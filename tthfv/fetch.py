# SPDX-License-Identifier: Apache-2.0
"""Fetch stage: clone a design's project repository at its pinned commit and resolve its sources.

A failed fetch is a result, not an exception: fetch() returns a FetchResult whose status says what
went wrong. Layout under WORK/src/<shuttle>/<macro>/:

    repo/        the checkout (shallow, exactly the pinned commit, submodules, patch applied)
    fetch.log    every git command and its output
    fetch.json   written last, by rename; its presence marks a finished, successful fetch
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tthfv import manifest, paths
from tthfv.manifest import Design

Status = Literal["ok", "fetch-fail", "patch-fail"]

_STAMP_VERSION = 1


@dataclass(frozen=True)
class FetchResult:
    status: Status
    src_dir: Path  # <checkout>/<subdir>: the project directory; its src/ holds the sources
    sources: tuple[Path, ...]  # absolute, existing; empty unless status is "ok"
    include_dirs: tuple[Path, ...]
    log: Path
    reason: str = ""


def _stamp(design: Design) -> dict:
    patch = hashlib.sha256(design.patch.read_bytes()).hexdigest() if design.patch else None
    return {
        "version": _STAMP_VERSION,
        "repo": design.repo,
        "commit": design.commit,
        "subdir": design.subdir,
        "patch": patch,
        "source_files": list(design.source_files),
    }


def _run(args: list[str], log: Path, cwd: Path, env: dict, timeout: int) -> tuple[bool, str]:
    """Run a command, append it and its output to the log. Returns (succeeded, output)."""
    try:
        done = subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
        )
        ok, out = done.returncode == 0, done.stdout + done.stderr
    except subprocess.TimeoutExpired as e:
        ok, out = False, f"timed out after {timeout}s\n{(e.stdout or b'')!r}{(e.stderr or b'')!r}"
    except OSError as e:  # no git binary, unusable cwd
        ok, out = False, f"{type(e).__name__}: {e}"
    with log.open("a") as f:
        f.write(f"$ {' '.join(args)}\n{out}")
        if not ok:
            f.write("(failed)\n")
    return ok, out


def _why(args: list[str], out: str) -> str:
    detail = " | ".join(out.strip().splitlines()[-3:]) or "no output"
    return f"git {args[1]} failed: {detail}"


def _use_https_submodules(repo: Path, log: Path, env: dict) -> None:
    """Submodule URLs are often SSH, which hangs or fails without a key: rewrite them to https."""
    listing = subprocess.run(
        ["git", "config", "-f", ".gitmodules", "--get-regexp", r"submodule\..*\.url"],
        cwd=repo, capture_output=True, text=True, env=env, check=False,
    )  # fmt: skip
    for line in listing.stdout.splitlines():
        key, _, url = line.partition(" ")
        url = url.strip()
        https = re.sub(r"^(git@github\.com:|ssh://git@github\.com/)", "https://github.com/", url)
        if https != url:
            _run(["git", "config", "-f", ".gitmodules", key, https], log, repo, env, 60)


def _clone(design: Design, repo: Path, log: Path, env: dict) -> tuple[Status, str]:
    """Check out design.commit into the empty directory repo and apply the patch."""
    steps = [
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", design.repo],
        ["git", "fetch", "-q", "--depth", "1", "origin", design.commit],
        ["git", "checkout", "-q", "FETCH_HEAD"],
    ]
    for args in steps:
        ok, out = _run(args, log, repo, env, 600)
        if not ok:
            return "fetch-fail", _why(args, out)
    if (repo / ".gitmodules").exists():
        _use_https_submodules(repo, log, env)
        for args in (
            ["git", "submodule", "sync", "-q", "--recursive"],
            ["git", "submodule", "update", "-q", "--init", "--depth", "1", "--recursive"],
        ):
            ok, out = _run(args, log, repo, env, 600)
            if not ok:
                return "fetch-fail", _why(args, out)
    if design.patch is not None:
        args = ["git", "apply", "--whitespace=nowarn", str(design.patch)]
        ok, out = _run(args, log, repo, env, 60)
        if not ok:
            return "patch-fail", f"patch {design.patch.name} did not apply: {out.strip()}"
    return "ok", ""


def _resolve(design: Design, repo: Path, src_dir: Path, log: Path) -> FetchResult:
    """Resolve source_files (relative to <src_dir>/src) to absolute existing paths inside repo.

    source_files come from external data, so every path is resolved (symlinks followed) and must
    stay inside the checkout; an escaping or absolute-outside entry is a failure naming the file.
    Entries may leave src/ (../lib/x.v) as long as they stay inside the checkout.
    """
    src = src_dir / "src"
    root = repo.resolve()
    resolved, missing, escaping = [], [], []
    for s in design.source_files:
        p = (src / s).resolve()  # an absolute s replaces src
        if not p.is_relative_to(root):
            escaping.append(s)
        elif not p.exists():
            missing.append(s)
        resolved.append(p)
    if escaping:
        reason = "sources outside the checkout: " + ", ".join(escaping)
        return FetchResult("fetch-fail", src_dir, (), (), log, reason)
    if missing:
        return FetchResult("fetch-fail", src_dir, (), (), log, "missing sources: " + ", ".join(missing))
    return FetchResult("ok", src_dir, tuple(resolved), (src,), log)


def fetch(design: Design, force: bool = False) -> FetchResult:
    """Fetch design.repo at design.commit into WORK/src/<id>/repo and resolve its sources.

    Without force, a finished fetch of the same repo, commit, patch and sources is reused with no
    network access. Failures (git, patch, missing source) are returned, not raised.
    """
    work = paths.design_dir("src", design.id).resolve()  # absolute, even if TTHFV_WORK is relative
    repo = work / "repo"
    log = work / "fetch.log"
    stamp_path = work / "fetch.json"
    src_dir = repo / design.subdir if design.subdir else repo

    try:
        if not force and stamp_path.exists() and repo.exists():
            try:
                fresh = json.loads(stamp_path.read_text()) == _stamp(design)
            except ValueError:
                fresh = False
            if fresh:
                result = _resolve(design, repo, src_dir, log)
                if result.status == "ok":
                    return result
        return _fetch_fresh(design, work, repo, src_dir, log)
    except (OSError, subprocess.TimeoutExpired) as e:
        reason = f"{type(e).__name__}: {e}"
        try:
            with log.open("a") as f:
                f.write(f"{reason}\n")
        except OSError:
            pass
        return FetchResult("fetch-fail", src_dir, (), (), log, reason)


def _fetch_fresh(design: Design, work: Path, repo: Path, src_dir: Path, log: Path) -> FetchResult:
    work.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    tmp = work / "repo.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
    status, reason = _clone(design, tmp, log, env)
    if status != "ok":
        return FetchResult(status, src_dir, (), (), log, reason)  # an earlier good repo is kept
    stamp_path = work / "fetch.json"
    stamp_path.unlink(missing_ok=True)
    if repo.exists():
        shutil.rmtree(repo)
    tmp.rename(repo)
    result = _resolve(design, repo, src_dir, log)
    if result.status == "ok":
        tmp_stamp = work / "fetch.json.tmp"
        tmp_stamp.write_text(json.dumps(_stamp(design), indent=2) + "\n")
        tmp_stamp.rename(stamp_path)
    return result


def _cmd_fetch(args: argparse.Namespace) -> int:
    designs = manifest.load(extras=manifest.EXTRAS_DEFAULT)
    chosen = [manifest.by_id(designs, i) for i in args.ids]
    failed = 0
    for d in chosen:
        r = fetch(d, force=args.force)
        print(f"{d.id} {r.status} {len(r.sources) if r.status == 'ok' else r.reason}")
        failed += r.status != "ok"
    return 1 if failed else 0


def add_parser(sub) -> None:
    p = sub.add_parser("fetch", help="clone designs' project repositories at their pinned commits")
    p.add_argument("ids", nargs="+", metavar="id", help="<shuttle>/<macro>")
    p.add_argument("--force", action="store_true", help="fetch again even if already fetched")
    p.set_defaults(func=_cmd_fetch)

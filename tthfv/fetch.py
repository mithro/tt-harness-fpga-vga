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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tthfv import manifest, paths
from tthfv.manifest import Design

Status = Literal["ok", "fetch-fail", "patch-fail"]

_STAMP_VERSION = 1


@dataclass(frozen=True)
class FetchResult:
    """What the later stages need to know about a design's sources. The build and the reference both
    work from these fields, so the two stages cannot disagree about what the design is."""

    status: Status
    src_dir: Path  # <checkout>/<subdir>: the project directory; its src/ holds the sources
    sources: tuple[Path, ...]  # absolute, existing, as listed; empty unless status is "ok"
    include_dirs: tuple[Path, ...]  # src/ first, then the directories of headers and other non-HDL files
    log: Path
    reason: str = ""
    synth_sources: tuple[Path, ...] = ()  # the .v/.sv files: the compilation units of both stages
    top_module_used: str = ""  # the module both stages use as the design's top
    top_module_fallback: bool = False  # True if the manifest's top_module was not declared
    sources_sha256: str = ""  # digest of everything under src/ (see sources_sha256())


HDL_SUFFIXES = (".v", ".sv")
VHDL_SUFFIXES = (".vhd", ".vhdl")
# `module name` or `macromodule name` not preceded by a word character (so not `endmodule`).
_MODULE = re.compile(r"(?<![\w$])(?:macromodule|module)\s+(?:automatic\s+|static\s+)?\\?([A-Za-z_][\w$]*)")
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


def sources_sha256(src: Path, listed: Iterable[Path] = (), base: Path | None = None) -> str:
    """sha256 over every regular file under src (names relative to it, contents, sorted; `.git` is
    skipped), then over each listed source outside src (name relative to base). Yosys and Verilator
    also read files by relative path ($readmemh) and by `include, so the whole tree counts.

    This is the one digest of a design's sources: the build's reuse check and the reference's key
    both use it."""
    h = hashlib.sha256()
    found: list[tuple[str, Path]] = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            p = Path(root) / name
            if p.is_file():
                found.append((p.relative_to(src).as_posix(), p))
    for rel, p in sorted(found):
        data = p.read_bytes()
        h.update(f"tree:{rel}\0{len(data)}\0".encode() + data)
    inside = {p for _rel, p in found}
    outside = sorted(
        (os.path.relpath(p, base if base is not None else src), p)
        for p in listed
        if p not in inside and p.is_file()
    )
    for rel, p in outside:
        data = p.read_bytes()
        h.update(f"listed:{rel}\0{len(data)}\0".encode() + data)
    return h.hexdigest()


def declared_modules(files: Iterable[Path]) -> set[str]:
    out: set[str] = set()
    for f in files:
        out.update(_MODULE.findall(_COMMENT.sub("", f.read_text(errors="replace"))))
    return out


def _pick_top(design: Design, units: tuple[Path, ...]) -> tuple[str, bool, str]:
    """(top module, whether the fallback was used, failure reason). The design's top_module if a
    module of that name is declared in the sources; else the one tt_um_ module they declare (re-hardened
    projects are sometimes renamed on the shuttle while the repo declares the original name); else
    failure."""
    declared = declared_modules(units)
    if design.top_module in declared:
        return design.top_module, False, ""
    tt = sorted(m for m in declared if m.startswith("tt_um_"))
    if len(tt) == 1:
        return tt[0], True, ""
    found = ", ".join(tt) if tt else "none"
    return "", False, (
        f"top module {design.top_module} is not declared in the sources, and they declare "
        f"{len(tt)} tt_um_ modules to fall back on ({found})"
    )


def _inputs(design: Design) -> dict:
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


def resolve(design: Design, repo: Path, src_dir: Path, log: Path) -> FetchResult:
    """Resolve source_files (relative to <src_dir>/src) to absolute existing paths inside repo, and
    decide once what the design is: its compilation units, include directories, top module and
    source digest.

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

    def fail(reason: str) -> FetchResult:
        return FetchResult("fetch-fail", src_dir, (), (), log, reason)

    if escaping:
        return fail("sources outside the checkout: " + ", ".join(escaping))
    if missing:
        return fail("missing sources: " + ", ".join(missing))
    if any(p.suffix.lower() in VHDL_SUFFIXES for p in resolved):
        return fail("VHDL sources are not supported")
    # Headers and other non-HDL files are not compilation units, but their directories are searched.
    units = tuple(p for p in resolved if p.suffix in HDL_SUFFIXES)
    if not units:
        return fail("no .v or .sv source files in the source list")
    includes = [src]
    for p in resolved:
        if p.suffix not in HDL_SUFFIXES and p.parent not in includes:
            includes.append(p.parent)
    top, fallback, why = _pick_top(design, units)
    if why:
        return fail(why)
    digest = sources_sha256(src, resolved, src_dir)
    return FetchResult(
        "ok", src_dir, tuple(resolved), tuple(includes), log, "", units, top, fallback, digest
    )


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
                stored = json.loads(stamp_path.read_text())
                fresh = {k: stored.get(k) for k in _inputs(design)} == _inputs(design)
            except ValueError:
                stored, fresh = {}, False
            if fresh:
                result = resolve(design, repo, src_dir, log)
                if result.status == "ok":
                    if _record(design, result) != stored:  # an edited checkout, or an older fetch.json
                        _write_stamp(work, design, result)
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
    result = resolve(design, repo, src_dir, log)
    if result.status == "ok":
        _write_stamp(work, design, result)
    return result


def _record(design: Design, r: FetchResult) -> dict:
    """The content of fetch.json: the inputs it was made from and what was resolved from them."""
    return {
        **_inputs(design),
        "synth_sources": [str(p.relative_to(r.src_dir)) if p.is_relative_to(r.src_dir) else str(p) for p in r.synth_sources],
        "top_module_used": r.top_module_used,
        "top_module_fallback": r.top_module_fallback,
        "sources_sha256": r.sources_sha256,
    }


def _write_stamp(work: Path, design: Design, r: FetchResult) -> None:
    tmp_stamp = work / "fetch.json.tmp"
    tmp_stamp.write_text(json.dumps(_record(design, r), indent=2) + "\n")
    tmp_stamp.rename(work / "fetch.json")


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

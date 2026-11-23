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
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tthfv import manifest, paths, proc
from tthfv.manifest import Design
from tthfv.text import strip_control

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
    checkout: Path | None = None  # the whole checkout (resolved): what a sandbox may read of the design


HDL_SUFFIXES = (".v", ".sv")
VHDL_SUFFIXES = (".vhd", ".vhdl")
# `module name` or `macromodule name` not preceded by a word character (so not `endmodule`).
_MODULE = re.compile(r"(?<![\w$])(?:macromodule|module)\s+(?:automatic\s+|static\s+)?\\?([A-Za-z_][\w$]*)")
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


MAX_SOURCE_READ = 64 << 20  # bytes of one source file the host reads into memory (to look for module names)
_HASH_CHUNK = 1 << 20


def _open_regular(path: Path) -> int | None:
    """A descriptor for path if it is a regular file (never following a symlink in the last component, never
    blocking on a FIFO), else None. The caller closes it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd


def _file_record(h, label: str, path: Path) -> None:
    """Add one file to the digest: "<label>\0<size>\0" and its bytes, read in chunks (size as fstat says,
    so a file that grows while it is read cannot make the record longer). Anything that is not a regular file
    is recorded by kind only, never read."""
    fd = _open_regular(path)
    if fd is None:
        h.update(f"{label}\0special\0".encode())
        return
    try:
        size = os.fstat(fd).st_size
        h.update(f"{label}\0{size}\0".encode())
        left = size
        while left > 0:
            chunk = os.read(fd, min(_HASH_CHUNK, left))
            if not chunk:
                break
            h.update(chunk)
            left -= len(chunk)
    finally:
        os.close(fd)


def sources_sha256(src: Path, listed: Iterable[Path] = (), base: Path | None = None) -> str:
    """sha256 over every file under src (names relative to it, contents, sorted; `.git` is skipped), then
    over each listed source outside src (name relative to base). Yosys and Verilator also read files by
    relative path ($readmemh) and by `include, so the whole tree counts.

    A symlink is never followed: it counts as its link text (os.readlink), whatever it points at, and a
    symlinked directory is not entered. So a repository cannot make the digest depend on, or the host read,
    a file outside the checkout.

    This is the one digest of a design's sources: the build's reuse check and the reference's key
    both use it."""
    h = hashlib.sha256()
    found: list[tuple[str, Path]] = []
    for root, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in [*files, *(d for d in dirs if (Path(root) / d).is_symlink())]:
            p = Path(root) / name
            found.append((p.relative_to(src).as_posix(), p))
    for rel, p in sorted(found):
        if p.is_symlink():
            h.update(f"link:{rel}\0{os.readlink(p)}\0".encode())
        else:
            _file_record(h, f"tree:{rel}", p)
    inside = {p for _rel, p in found}
    outside = sorted(
        (os.path.relpath(p, base if base is not None else src), p)
        for p in listed
        if p not in inside and p.is_file()
    )
    for rel, p in outside:
        _file_record(h, f"listed:{rel}", p)
    return h.hexdigest()


def read_source_text(path: Path) -> str:
    """The text of a source file for looking up module names: a regular file only (a symlink is not
    followed), at most MAX_SOURCE_READ bytes of it."""
    fd = _open_regular(path)
    if fd is None:
        return ""
    try:
        return os.read(fd, MAX_SOURCE_READ).decode(errors="replace")
    finally:
        os.close(fd)


def declared_modules(files: Iterable[Path]) -> set[str]:
    out: set[str] = set()
    for f in files:
        out.update(_MODULE.findall(_COMMENT.sub("", read_source_text(f))))
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
    return (
        "",
        False,
        (
            f"top module {design.top_module} is not declared in the sources, and they declare "
            f"{len(tt)} tt_um_ modules to fall back on ({found})"
        ),
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


# Git settings that stop git running a program because of a hook, a repository or a configuration: no
# hooks (git init copies them from the template directory), no `ext::` transport (it runs a command named
# in a submodule URL), no filesystem monitor. Given through GIT_CONFIG_COUNT, which reaches the git
# children too (submodule update), like `-c` does, and keeps `git <subcommand>` in the log and in errors.
_GIT_SAFE_CONFIG = (
    ("core.hooksPath", "/dev/null"),
    ("protocol.ext.allow", "never"),
    ("core.fsmonitor", "false"),
)


def git_env() -> dict[str, str]:
    """The environment for every git command of the fetch stage: the host's, with no prompting, a
    batch-mode ssh and _GIT_SAFE_CONFIG added after any GIT_CONFIG_* entries already there."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
    n = int(env.get("GIT_CONFIG_COUNT") or 0)
    for i, (key, value) in enumerate(_GIT_SAFE_CONFIG, start=n):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    env["GIT_CONFIG_COUNT"] = str(n + len(_GIT_SAFE_CONFIG))
    return env


def _run(args: list[str], log: Path, cwd: Path, env: dict) -> tuple[int, str]:
    """Run a git command with a timeout, append it and its output to the log. Returns (exit code, output);
    the code is proc.TIMEOUT_RC if it timed out (its process group is killed) and -1 if it could not run."""
    try:
        done = proc.capture(args, proc.GIT_TIMEOUT, cwd=cwd, env=env)
        rc, out = done.rc, done.text
        if rc == proc.TIMEOUT_RC:
            out += f"\ntimed out after {proc.GIT_TIMEOUT}s\n"
    except OSError as e:  # no git binary, unusable cwd
        rc, out = -1, f"{type(e).__name__}: {e}"
    with log.open("a") as f:
        f.write(f"$ {' '.join(args)}\n{out}")
        if rc != 0:
            f.write("(failed)\n")
    return rc, out


def _why(args: list[str], rc: int, out: str) -> str:
    if rc == proc.TIMEOUT_RC:
        return f"git {args[1]} timed out after {proc.GIT_TIMEOUT}s"
    detail = " | ".join(out.strip().splitlines()[-3:]) or "no output"
    return f"git {args[1]} failed: {detail}"


def _use_https_submodules(repo: Path, log: Path, env: dict) -> tuple[Status, str]:
    """Submodule URLs are often SSH, which hangs or fails without a key: rewrite them to https."""
    args = ["git", "config", "-f", ".gitmodules", "--get-regexp", r"submodule\..*\.url"]
    rc, out = _run(args, log, repo, env)
    if rc not in (0, 1):  # 1: no url entries
        return "fetch-fail", _why(args, rc, out)
    for line in out.splitlines():
        key, _, url = line.partition(" ")
        url = url.strip()
        https = re.sub(r"^(git@github\.com:|ssh://git@github\.com/)", "https://github.com/", url)
        if https != url:
            set_args = ["git", "config", "-f", ".gitmodules", key, https]
            rc, out = _run(set_args, log, repo, env)
            if rc != 0:
                return "fetch-fail", _why(set_args, rc, out)
    return "ok", ""


def _clone(design: Design, repo: Path, log: Path, env: dict) -> tuple[Status, str]:
    """Check out design.commit into the empty directory repo and apply the patch."""
    steps = [
        ["git", "init", "-q"],
        ["git", "remote", "add", "--", "origin", design.repo],
        ["git", "fetch", "-q", "--depth", "1", "--", "origin", design.commit],
        ["git", "checkout", "-q", "FETCH_HEAD"],
    ]
    for args in steps:
        rc, out = _run(args, log, repo, env)
        if rc != 0:
            return "fetch-fail", _why(args, rc, out)
    if (repo / ".gitmodules").exists():
        status, reason = _use_https_submodules(repo, log, env)
        if status != "ok":
            return status, reason
        for args in (
            ["git", "submodule", "sync", "-q", "--recursive"],
            ["git", "submodule", "update", "-q", "--init", "--depth", "1", "--recursive"],
        ):
            rc, out = _run(args, log, repo, env)
            if rc != 0:
                return "fetch-fail", _why(args, rc, out)
    if design.patch is not None:
        args = ["git", "apply", "--whitespace=nowarn", "--", str(design.patch)]
        rc, out = _run(args, log, repo, env)
        if rc == proc.TIMEOUT_RC:
            return "patch-fail", _why(args, rc, out)
        if rc != 0:
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
        "ok", src_dir, tuple(resolved), tuple(includes), log, "", units, top, fallback, digest, root
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
    except OSError as e:
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
    status, reason = _clone(design, tmp, log, git_env())
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
        "synth_sources": [
            str(p.relative_to(r.src_dir)) if p.is_relative_to(r.src_dir) else str(p) for p in r.synth_sources
        ],
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
        print(strip_control(f"{d.id} {r.status} {len(r.sources) if r.status == 'ok' else r.reason}"))
        failed += r.status != "ok"
    return 1 if failed else 0


def add_parser(sub) -> None:
    p = sub.add_parser("fetch", help="clone designs' project repositories at their pinned commits")
    p.add_argument("ids", nargs="+", metavar="id", help="<shuttle>/<macro>")
    p.add_argument("--force", action="store_true", help="fetch again even if already fetched")
    p.set_defaults(func=_cmd_fetch)

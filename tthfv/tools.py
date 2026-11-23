# SPDX-License-Identifier: Apache-2.0
"""Install and locate the pinned oss-cad-suite toolchain."""

import argparse
import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from tthfv import paths, pins, proc
from tthfv.errors import UserError

TOOL_NAMES = ("yosys", "nextpnr-ice40", "icepack", "verilator")
VERSION_TIMEOUT = 60  # seconds, for one `--version` query
_INSTALL_HINT = "run: uv run tthfv tools install"


class ToolMissing(UserError):
    pass


def tools_dir() -> Path:
    """Read at call time so TTHFV_TOOLS can change after import."""
    return Path(os.environ.get("TTHFV_TOOLS") or paths.REPO_ROOT / "tools")


def bin_dir() -> Path:
    return tools_dir() / "oss-cad-suite" / "bin"


def tool(name: str) -> Path:
    if name not in TOOL_NAMES:
        raise ValueError(f"unknown tool {name!r}; allowed: {', '.join(TOOL_NAMES)}")
    path = bin_dir() / name
    if not path.is_file():
        raise ToolMissing(f"{name} not found at {path}; {_INSTALL_HINT}")
    return path


def sv2v() -> Path | None:
    """The pinned sv2v, if the toolchain ships one; never one from the host PATH."""
    path = bin_dir() / "sv2v"
    return path if path.is_file() else None


_GUARDED = (*TOOL_NAMES, "sv2v")  # tools that may only come from the pinned toolchain
_farms: dict[tuple[str, str], str | None] = {}


def _without_guarded(host_dir: str) -> str | None:
    """host_dir if it holds none of the guarded tools; else a directory of symlinks to everything in it
    but those, so git, make and the compiler stay reachable. None if it cannot be made (then the
    directory is left off PATH)."""
    try:
        names = sorted(os.listdir(host_dir))
    except OSError:
        return None
    if not any((Path(host_dir) / n).is_file() for n in _GUARDED if n in names):
        return host_dir
    key = (str(tools_dir()), host_dir)
    if key in _farms:
        return _farms[key]
    keep = [n for n in names if n not in _GUARDED]
    tag = hashlib.sha256("\0".join([host_dir, *keep]).encode()).hexdigest()[:16]
    farm = tools_dir() / ".hostpath" / tag
    result: str | None = str(farm)
    if not farm.is_dir():
        try:
            farm.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(dir=farm.parent, prefix=".tmp-"))
            for n in keep:
                (tmp / n).symlink_to(Path(host_dir) / n)
            try:
                tmp.rename(farm)
            except OSError:  # another process made it first
                shutil.rmtree(tmp, ignore_errors=True)
        except OSError:
            result = None
    _farms[key] = result
    return result


def env() -> dict[str, str]:
    """The environment for running tools: the pinned bin directory first, then the host PATH with the
    guarded tools (yosys, nextpnr-ice40, icepack, verilator, sv2v) hidden, so none of them is ever
    taken from the host."""
    e = dict(os.environ)
    rest = [d for host in e.get("PATH", "").split(os.pathsep) if host and (d := _without_guarded(host))]
    e["PATH"] = os.pathsep.join([str(bin_dir()), *rest])
    return e


_version_cache: dict[tuple[str, int, int], str] = {}


def version_line(exe: Path) -> str:
    """The first line of `exe --version`, asked once per process for each executable (a changed file is
    asked again). A tool that hangs or fails is a UserError."""
    st = exe.stat()
    key = (str(exe), st.st_mtime_ns, st.st_size)
    if key not in _version_cache:
        done = proc.capture([str(exe), "--version"], VERSION_TIMEOUT, env=env())
        if done.rc == proc.TIMEOUT_RC:
            raise UserError(f"{exe.name} --version timed out after {VERSION_TIMEOUT}s")
        first = done.text.strip().splitlines()
        if done.rc != 0 or not first:
            raise UserError(f"{exe.name} --version failed: {done.text.strip() or 'no output'}")
        _version_cache[key] = first[0]
    return _version_cache[key]


def icepack_version() -> str:
    """icepack has no version flag; it ships in the pinned suite."""
    tool("icepack")
    return f"icepack (no --version flag; from oss-cad-suite {pins.OSS_CAD_SUITE_VERSION})"


def versions() -> dict[str, str]:
    out = {}
    for name in TOOL_NAMES:
        if name == "icepack":
            # icepack has no version flag; it ships in the pinned suite.
            out[name] = icepack_version()
            continue
        out[name] = version_line(tool(name))
    return out


def install() -> None:
    root = tools_dir()
    check = bin_dir() / "verilator"
    if check.exists():
        print(f"already installed: {check}")
        return
    root.mkdir(parents=True, exist_ok=True)
    tarball = root / pins.OSS_CAD_SUITE_URL.rsplit("/", 1)[1]
    part = tarball.with_name(tarball.name + ".part")
    tmp = root / ".oss-cad-suite.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"downloading {pins.OSS_CAD_SUITE_URL}", flush=True)
    sha = hashlib.sha256()
    try:
        with urllib.request.urlopen(pins.OSS_CAD_SUITE_URL, timeout=60) as resp, part.open("wb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
                sha.update(chunk)
        part.rename(tarball)
    finally:
        part.unlink(missing_ok=True)
    print(f"sha256 {sha.hexdigest()}  {tarball.name}", flush=True)
    print(f"extracting into {root}", flush=True)
    try:
        with tarfile.open(tarball) as tf:
            tf.extractall(tmp, filter="data")
        if not (tmp / "oss-cad-suite" / "bin" / "verilator").exists():
            raise ToolMissing(f"archive {tarball.name} has no oss-cad-suite/bin/verilator")
        shutil.rmtree(root / "oss-cad-suite", ignore_errors=True)  # stale partial tree
        (tmp / "oss-cad-suite").rename(root / "oss-cad-suite")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    (root / "oss-cad-suite.sha256").write_text(f"{sha.hexdigest()}  {tarball.name}\n")
    print(f"installed {pins.OSS_CAD_SUITE_VERSION}: {check}")


def _run_install(args: argparse.Namespace) -> int:
    install()
    return 0


def _run_versions(args: argparse.Namespace) -> int:
    for name, line in versions().items():
        print(f"{name}: {line}")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("tools", help="install and inspect the oss-cad-suite toolchain")
    tsub = p.add_subparsers(dest="tools_command", metavar="<action>")
    tsub.required = True
    tsub.add_parser("install", help="download and extract the pinned toolchain").set_defaults(
        func=_run_install
    )
    tsub.add_parser("versions", help="print tool versions").set_defaults(func=_run_versions)

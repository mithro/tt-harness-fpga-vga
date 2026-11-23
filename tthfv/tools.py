# SPDX-License-Identifier: Apache-2.0
"""Install and locate the pinned oss-cad-suite toolchain."""

import argparse
import hashlib
import os
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from tthfv import paths, pins

TOOL_NAMES = ("yosys", "nextpnr-ice40", "icepack", "verilator")
_INSTALL_HINT = "run: uv run tthfv tools install"


class ToolMissing(RuntimeError):
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


def env() -> dict[str, str]:
    e = dict(os.environ)
    e["PATH"] = f"{bin_dir()}{os.pathsep}{e.get('PATH', '')}"
    return e


def versions() -> dict[str, str]:
    out = {}
    for name in TOOL_NAMES:
        if name == "icepack":
            # icepack has no version flag; it ships in the pinned suite.
            tool(name)
            out[name] = f"icepack (no --version flag; from oss-cad-suite {pins.OSS_CAD_SUITE_VERSION})"
            continue
        r = subprocess.run(
            [str(tool(name)), "--version"], env=env(), capture_output=True, text=True, check=True
        )
        out[name] = (r.stdout.strip() or r.stderr.strip()).splitlines()[0]
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
    print(f"downloading {pins.OSS_CAD_SUITE_URL}", flush=True)
    sha = hashlib.sha256()
    with urllib.request.urlopen(pins.OSS_CAD_SUITE_URL) as resp, part.open("wb") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)
            sha.update(chunk)
    part.rename(tarball)
    (root / "oss-cad-suite.sha256").write_text(f"{sha.hexdigest()}  {tarball.name}\n")
    print(f"sha256 {sha.hexdigest()}  {tarball.name}", flush=True)
    print(f"extracting into {root}", flush=True)
    with tarfile.open(tarball) as tf:
        tf.extractall(root, filter="data")
    if not check.exists():
        raise ToolMissing(f"install finished but {check} is missing")
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

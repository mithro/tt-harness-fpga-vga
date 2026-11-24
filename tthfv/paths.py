# SPDX-License-Identifier: Apache-2.0
"""Filesystem layout shared by all stages."""

import os
import re
from pathlib import Path

from tthfv.errors import UserError

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK = Path(os.environ.get("TTHFV_WORK") or REPO_ROOT / "work")
TOOLS = Path(os.environ.get("TTHFV_TOOLS") or REPO_ROOT / "tools")

DESIGN_ID = re.compile(r"^[a-z0-9]+/[A-Za-z0-9_]+$")
_STAGE = re.compile(r"^[a-z0-9_-]+$")


def design_dir(stage: str, design_id: str) -> Path:
    """Return WORK/<stage>/<shuttle>/<macro> for a design id like "tt08/tt_um_x"."""
    if not _STAGE.match(stage):
        raise ValueError(f"invalid stage name: {stage!r}")
    if not DESIGN_ID.match(design_id):
        raise ValueError(f"invalid design id (want <shuttle>/<macro>): {design_id!r}")
    path = WORK / stage / design_id
    no_symlinks(path, WORK)
    return path


MIN_FREE_DEFAULT = 20 << 30  # bytes that must be free where WORK is, before a stage starts
RUN_RESERVE = 10 << 30  # bytes kept free on top of one scratch cap per job, when a run starts
_SIZE = re.compile(r"^([1-9]\d*)([KMGT]?)$")


def min_free() -> int:
    """TTHFV_MIN_FREE (like 20G; default 20G): the free space a stage insists on before it starts."""
    text = os.environ.get("TTHFV_MIN_FREE") or ""
    if not text:
        return MIN_FREE_DEFAULT
    m = _SIZE.match(text)
    if not m:
        raise UserError(f"TTHFV_MIN_FREE must look like 20G, not {text!r}")
    return int(m.group(1)) * {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}[m.group(2)]


def free_bytes(path: Path) -> int:
    """Free bytes (to an ordinary user) on the filesystem that holds path, or its nearest existing parent."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    st = os.statvfs(p)
    return st.f_bavail * st.f_frsize


def require_free(path: Path, need: int, why: str = "") -> None:
    """UserError if less than `need` bytes are free where path is."""
    free = free_bytes(path)
    if free < need:
        raise UserError(
            f"not enough free disk space for {path.name or path}: {free / (1 << 30):.1f} GiB free, "
            f"{need / (1 << 30):.1f} GiB needed{why}"
        )


def no_symlinks(path: Path, base: Path) -> None:
    """UserError if path or any directory between base (exclusive) and it is a symlink: written through, it
    would redirect every write. base itself may be one (the user may link their work directory)."""
    cur = base
    for part in path.relative_to(base).parts:
        cur = cur / part
        if cur.is_symlink():
            raise UserError(f"{cur} is a symbolic link; refusing to read or write through it")

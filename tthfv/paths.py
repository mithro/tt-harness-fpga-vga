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
    cur = WORK
    for part in path.relative_to(
        WORK
    ).parts:  # nothing below WORK may be a symlink: it would redirect every write
        cur = cur / part
        if cur.is_symlink():
            raise UserError(f"{cur} is a symbolic link; refusing to read or write through it")
    return path

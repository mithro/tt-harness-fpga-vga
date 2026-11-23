# SPDX-License-Identifier: Apache-2.0
"""Claim files: a courtesy lock between sessions on this host (this project's and other projects').

A claim is `<dir>/<board>-<usb serial>.json` holding who took it, why, and until when (at most 30 minutes
ahead). An expired claim is void. Other sessions name their files differently (for example `fpga-4-p35.json`),
so ANY live JSON file in the directory whose name starts with `<board>-` counts as a claim on that board: one
with a parseable `expires` is live until then, one without is live for 30 minutes after it was last written.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from tthfv import paths
from tthfv.board import ClaimHeld
from tthfv.errors import UserError

OWNER = "tt-harness-fpga-vga"
MAX_MINUTES = 30
UNDATED_LIFETIME_S = 30 * 60


@dataclass(frozen=True)
class Claim:
    board: str
    usb_serial: str | None
    owner: str | None
    purpose: str | None
    created: str | None
    expires: str | None
    path: Path


def require_shared_dir() -> Path:
    """The claims directory for a real session: it must be named explicitly, because it only works if every
    project using these boards on this host writes to the same one (the default under the work directory
    would hide their claims from us and ours from them)."""
    env = os.environ.get("TTHFV_BOARD_CLAIMS")
    if not env:
        raise UserError(
            "set TTHFV_BOARD_CLAIMS to the claims directory shared by every project that uses these boards "
            "on this host; a board session will not start without it"
        )
    return Path(env)


def _pause() -> None:
    """Nothing. A test replaces it to hold a process between deciding and acting (tests/claims_child.py)."""


def claims_dir() -> Path:
    env = os.environ.get("TTHFV_BOARD_CLAIMS")
    if env:
        return Path(env)
    work = os.environ.get("TTHFV_WORK")
    return (Path(work) if work else paths.REPO_ROOT / "work") / "board-claims"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value)  # accepts a trailing Z
    except ValueError:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=dt.UTC)


def _read(board: str, path: Path) -> Claim | None:
    """The claim in `path` if it is live, else None."""
    try:
        text = path.read_text()
        mtime = path.stat().st_mtime
    except OSError:
        return None  # gone meanwhile
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    data = data if isinstance(data, dict) else {}
    expires = _parse_time(data.get("expires"))
    if expires is not None:
        if expires <= _now():
            return None
    elif _now().timestamp() - mtime >= UNDATED_LIFETIME_S:
        return None

    def s(k):
        return data.get(k) if isinstance(data.get(k), str) else None

    return Claim(board, s("usb_serial"), s("owner"), s("purpose"), s("created"), s("expires"), path)


def _candidates(board: str) -> list[Path]:
    d = claims_dir()
    try:
        names = sorted(os.listdir(d))
    except FileNotFoundError:
        return []
    return [d / n for n in names if n.startswith(f"{board}-") and n.endswith(".json")]


def current(board: str) -> Claim | None:
    """A live claim on `board` by anyone, or None."""
    for p in _candidates(board):
        c = _read(board, p)
        if c is not None:
            return c
    return None


def _describe(c: Claim) -> str:
    return f"{c.path.name} (owner {c.owner}, purpose {c.purpose!r}, expires {c.expires})"


LOCK_NAME = ".claims.lock"  # not a .json file, so never mistaken for a claim


@contextmanager
def _locked(d: Path) -> Iterator[None]:
    """An exclusive flock on a lock file in the claims directory, around a whole check-and-create or release, so
    that two processes cannot both decide an expired claim is void and both replace it. (Other projects'
    sessions do not take this lock; for them the exclusive create and the re-check below remain the guard.)"""
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def take(board: str, usb_serial: str, purpose: str, minutes: float) -> Claim:
    if not 0 < minutes <= MAX_MINUTES:
        raise ValueError(f"a claim lasts more than 0 and at most {MAX_MINUTES} minutes, not {minutes}")
    d = claims_dir()
    with _locked(d):
        return _take(d, board, usb_serial, purpose, minutes)


def _take(d: Path, board: str, usb_serial: str, purpose: str, minutes: float) -> Claim:
    held = current(board)
    if held is not None:
        raise ClaimHeld(f"{board} is claimed: {_describe(held)}")
    path = d / f"{board}-{usb_serial}.json"
    expired = path.exists() and _read(board, path) is None  # an expired claim of ours: void, so replace it
    _pause()
    if expired:
        path.unlink(missing_ok=True)
    created = _now()
    data = {
        "board": board,
        "usb_serial": usb_serial,
        "owner": OWNER,
        "purpose": purpose,
        "created": _iso(created),
        "expires": _iso(created + dt.timedelta(minutes=minutes)),
    }
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)  # atomic: exactly one taker wins
    except FileExistsError as e:
        other = _read(board, path)
        raise ClaimHeld(f"{board} is claimed: {_describe(other) if other else path.name}") from e
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    claim = Claim(board, usb_serial, OWNER, purpose, data["created"], data["expires"], path)
    # Another project's session names its file differently, so the exclusive create cannot see it: look again
    # now that ours exists, and back off if someone else got in between.
    for p in _candidates(board):
        if p != path and _read(board, p) is not None:
            _release(claim)
            raise ClaimHeld(f"{board} is claimed: {_describe(_read(board, p) or claim)}")
    return claim


def release(claim: Claim) -> None:
    """Remove our claim file, but only if it is still ours (same owner, purpose and creation time)."""
    with _locked(claim.path.parent):
        _release(claim)


def _release(claim: Claim) -> None:
    try:
        data = json.loads(claim.path.read_text())
    except (OSError, ValueError):
        return
    if (
        isinstance(data, dict)
        and data.get("owner") == claim.owner
        and data.get("created") == claim.created
        and data.get("purpose") == claim.purpose
    ):
        claim.path.unlink(missing_ok=True)

# SPDX-License-Identifier: Apache-2.0
"""Read a board's public status and decide whether it may be used.

The daemon on each board's Raspberry Pi publishes `/health`; the site serves it at
`<base>/board/<slug>/status.json`. `clients` counts the serial WebSocket's subscribers (a visitor who merely
opens the board's page is one), and `idle_display.state` is "loaded" only when the daemon has put its idle design
back on a board that nobody is using ("waiting", "in use" and "left: ..." mean someone was or is there).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field

from tthfv.board import BoardError

DEFAULT_BASE_URL = "https://tinytapeout.fpgas.online"
USER_AGENT = "tthfv"
HTTP_TIMEOUT_S = 8.0  # below the session's 10 s blind limit, so one hung read cannot hide a visitor
# The daemon refreshes about every 5 s; reading more often only adds load to a public service. The limit is
# per process (all threads share it); separate processes each keep their own.
MIN_INTERVAL_S = 5.0

# A board is identified by its USB serial only (switch-port tables disagree between documents).
BOARDS = {
    "fpga-3": "8c46329b33590ecb",
    "fpga-4": "a2961e5cac65b25f",
    "tt-fd1a167bd863a198": "fd1a167bd863a198",
}
# Boards this project must never use: fpga-1 (4df39a7a6856f86f) belongs to another session.
FORBIDDEN = {"fpga-1": "4df39a7a6856f86f"}

_last_read: dict[str, float] = {}
_rate_lock = threading.Lock()


@dataclass(frozen=True)
class BoardStatus:
    slug: str
    usb_serial: str | None
    clients: int | None
    idle_state: str | None
    reachable: bool
    present: bool
    raw: dict = field(repr=False)


def base_url() -> str:
    return (os.environ.get("TTHFV_BOARD_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def status_url(slug: str) -> str:
    return f"{base_url()}/board/{slug}/status.json"


def serial_url(slug: str) -> str:
    ws = os.environ.get("TTHFV_BOARD_WS_URL")
    if not ws:
        b = base_url()
        ws = "wss://" + b[len("https://") :] if b.startswith("https://") else "ws://" + b.split("://", 1)[-1]
    return f"{ws.rstrip('/')}/ws/board/{slug}/serial"


def parse(slug: str, raw: dict) -> BoardStatus:
    board = raw.get("board") if isinstance(raw.get("board"), dict) else {}
    idle = raw.get("idle_display") if isinstance(raw.get("idle_display"), dict) else {}
    clients = raw.get("clients")
    return BoardStatus(
        slug=slug,
        usb_serial=board.get("usb_serial") if isinstance(board.get("usb_serial"), str) else None,
        clients=clients if isinstance(clients, int) and not isinstance(clients, bool) else None,
        idle_state=idle.get("state") if isinstance(idle.get("state"), str) else None,
        reachable=raw.get("reachable") is True,
        present=board.get("present") is True,
        raw=raw,
    )


def _wait_turn(slug: str) -> None:
    """At most one read of a board every MIN_INTERVAL_S, across every thread of this process."""
    with _rate_lock:
        last = _last_read.get(slug)
        now = time.monotonic()
        if last is not None and now - last < MIN_INTERVAL_S:
            time.sleep(MIN_INTERVAL_S - (now - last))
        _last_read[slug] = time.monotonic()


def read(slug: str) -> BoardStatus:
    _wait_turn(slug)
    req = urllib.request.Request(
        status_url(slug), headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            body = resp.read()
    except Exception as e:
        raise BoardError(f"cannot read the status of {slug}: {type(e).__name__}: {e}") from e
    try:
        raw = json.loads(body)
    except ValueError as e:
        raise BoardError(f"status of {slug} is not JSON: {body[:200]!r}") from e
    if not isinstance(raw, dict):
        raise BoardError(f"status of {slug} is not a JSON object")
    return parse(slug, raw)


def expected_serial(slug: str) -> str | None:
    return BOARDS.get(slug)


def usable(s: BoardStatus) -> tuple[bool, str]:
    """The rule: a known board, never a forbidden one, reachable and present, with the USB serial we expect,
    `clients` 0 and the idle display `loaded`. Anything else means someone was or is there, or it is not the
    board we think."""
    if s.slug in FORBIDDEN or s.usb_serial in FORBIDDEN.values():
        return False, f"{s.slug} (usb serial {s.usb_serial}) belongs to another session: never use it"
    want = BOARDS.get(s.slug)
    if want is None:
        return False, f"{s.slug} is an unknown board"
    if not s.reachable:
        return False, f"{s.slug} is not reachable"
    if not s.present:
        return False, f"{s.slug}: no board present on the Pi"
    if s.usb_serial != want:
        return False, f"{s.slug} reports usb serial {s.usb_serial}, expected {want}"
    if s.clients != 0:
        return False, f"{s.slug} has clients {s.clients}"
    if s.idle_state != "loaded":
        return False, f"{s.slug} idle display is {s.idle_state!r}, not 'loaded'"
    return True, "clients 0, idle display loaded"

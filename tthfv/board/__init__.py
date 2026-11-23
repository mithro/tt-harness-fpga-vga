# SPDX-License-Identifier: Apache-2.0
"""Host-side link to the public Tiny Tapeout FPGA boards behind tinytapeout.fpgas.online.

The boards are public and shared, with no lock: `status` decides whether a board may be used, `claims` keeps
this project's sessions (and other projects' sessions on this host) off each other, `link` speaks the serial
WebSocket and MicroPython's raw REPL, and `session.board_session` puts the etiquette rules into code.
"""

from tthfv.errors import UserError


class BoardError(UserError):
    """Anything that went wrong talking to a board."""


class BoardNotUsable(BoardError):
    """The status rule refused the board (somebody is or was there, or it is not the board we think)."""


class ClaimHeld(BoardError):
    """Another session holds a live claim on the board."""


class BoardTaken(BoardError):
    """Another client connected to the board while we had it."""


class SessionTimeLimit(BoardError):
    """The session reached its cap on connected time."""


class WatchLost(BoardError):
    """The session can no longer see the board's status (watcher dead or status unreadable), so it
    cannot tell whether a visitor has arrived: it stops."""


class BoardNotRestored(BoardError):
    """After the session the board's status did not return to clients 0 and idle display loaded."""


class ReplNoPrompt(BoardError):
    """The board did not show the raw REPL banner; `seen` holds every byte it sent meanwhile."""

    def __init__(self, message: str, seen: bytes = b""):
        super().__init__(f"{message}; saw {seen[-400:]!r}")
        self.seen = seen


class BoardScriptError(BoardError):
    """A script raised on the board; `traceback` is the board's stderr."""

    def __init__(self, traceback: str, stdout: bytes = b""):
        super().__init__(f"the board raised:\n{traceback.strip()}")
        self.traceback = traceback
        self.stdout = stdout

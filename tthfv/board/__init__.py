# SPDX-License-Identifier: Apache-2.0
"""Host-side link to the public Tiny Tapeout FPGA boards behind tinytapeout.fpgas.online.

The boards are public and shared, with no lock: `status` decides whether a board may be used, `claims` keeps
this project's sessions (and other projects' sessions on this host) off each other, `link` speaks the serial
WebSocket and MicroPython's raw REPL, and `session.board_session` puts the etiquette rules into code.
"""

from tthfv.errors import UserError


class SessionTerminated(BaseException):
    """SIGTERM or SIGHUP arrived during a board session. Like KeyboardInterrupt it is not an Exception, so
    nothing catches it by accident; the session cleans up and re-raises it."""

    def __init__(self, signum: int):
        super().__init__(f"terminated by signal {signum}")
        self.signum = signum


class BoardError(UserError):
    """Anything that went wrong talking to a board."""


class SessionThreadError(BoardError):
    """A board session was started off the main thread, where Python cannot install signal handlers: Ctrl-C
    or SIGTERM would then end the process without the clean-up."""


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


class LinkLost(BoardError):
    """The serial WebSocket closed under us."""


class BoardLeftDirty(BoardError):
    """The connection dropped after the session changed the board, and it could not be cleaned up safely."""


class BoardNotRestored(BoardError):
    """After the session the board's status did not return to clients 0 and idle display loaded."""


class ReplNoPrompt(BoardError):
    """The board did not show the raw REPL banner; `seen` holds every byte it sent meanwhile."""

    def __init__(self, message: str, seen: bytes = b""):
        super().__init__(f"{message}; saw {seen[-400:]!r}")
        self.seen = seen


class BoardFramingError(BoardError):
    """The raw REPL's framing did not hold (text output containing 0x04, say); the link resynchronised at the
    next prompt."""


class BoardScriptError(BoardError):
    """A script raised on the board; `traceback` is the board's stderr."""

    def __init__(self, traceback: str, stdout: bytes = b""):
        super().__init__(f"the board raised:\n{traceback.strip()}")
        self.traceback = traceback
        self.stdout = stdout

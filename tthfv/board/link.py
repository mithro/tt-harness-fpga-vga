# SPDX-License-Identifier: Apache-2.0
"""One serial connection to a board through the daemon's WebSocket, and MicroPython's raw REPL over it.

Framing of the WebSocket (fpgas.online-tt `server.py`): binary frames carry serial bytes both ways; text frames
from the server are JSON notices. The first notice, `{"event": "board", ...}`, is sent only once the daemon has
attached this client to the serial bridge (while its idle display is busy with the board a new client is held,
unattached), so nothing is written before it arrives. The bridge drops a client more than 256 KB behind.

Raw REPL framing: the board answers a script with `OK`, then stdout, 0x04, stderr, 0x04, `>`. stdout ends at
an unescaped 0x04, so a script that sends binary data must length-prefix it; `start_raw` / `read_exact` /
`finish_raw` exist for that, and `exec_raw` is those three for text output.

`abort(exc)` (called from another thread, for example a status poll that saw a visitor) makes every later read
raise `exc`; `cleanup` ignores it, because cleanup must run on every exit path.
"""

from __future__ import annotations

import datetime as dt
import json
import socket
import threading
import time
from contextlib import ExitStack
from typing import Self

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect

from tthfv.board import (
    BoardError,
    BoardFramingError,
    BoardScriptError,
    LinkLost,
    ReplNoPrompt,
    SessionTimeLimit,
    WatchLost,
    status,
)

CTRL_A, CTRL_B, CTRL_C, CTRL_D = b"\x01", b"\x02", b"\x03", b"\x04"
RAW_BANNER = b"raw REPL; CTRL-B to exit\r\n>"
FRIENDLY_PROMPT = b">>> "
SOFT_REBOOT = b"soft reboot"
BANNER_TIMEOUT_S = 3.0
BANNER_SCAN_CAP = 64 * 1024  # bytes discarded while looking for the banner before giving up
POLL_S = 0.2  # longest a read blocks before it looks at the abort flag again
TRANSCRIPT_CAP = 1 << 20
CLEANUP_TIMEOUT_S = 20.0  # from Ctrl-D to the SDK's prompt (about 2 s measured)
SEND_TIMEOUT_S = 60.0  # longest any one write may take (also cut by the session deadline)
CLEANUP_SEND_TIMEOUT_S = 5.0  # for the clean-up's few bytes


class _Done(Exception):
    pass


class Link:
    def __init__(self, slug: str, ws, stack: ExitStack, t0: float):
        self.slug = slug
        self._ws = ws
        self._stack = stack
        self._t0 = t0
        self._buf = b""
        self._write_lock = threading.Lock()
        self._abort: BaseException | None = None
        self.notices: list[dict] = []
        self.transcript: list[dict] = []
        self._transcript_bytes = 0
        self.connect_s: float = 0.0
        self.sent_any = False
        self.dirty = False  # we changed the board's state: leave with a soft reset
        self.changes: list[str] = []  # what made it dirty, for an error that has to say so
        self.lost = False  # the WebSocket closed under us (or we cut it at the cap)
        self.lost_utc: str | None = None
        self.killed = False  # cut by the session at the connected-time cap
        self.in_raw = False  # at the raw REPL's prompt or running a script in it, as far as we know
        # A script of ours may still be running: start_raw without a finish_raw that reached the prompt
        # (a timeout, an exception mid-exec). Only then does leaving need Ctrl-C.
        self.script_running = False
        self.no_prompt = False
        self.closed = False
        self.opened_at = t0  # when the WebSocket was asked for: the session's cap counts from here
        self.deadline: float | None = None  # monotonic time after which every operation raises
        self.watch: threading.Thread | None = None  # the session's status watcher; dead = blind

    # -- connection --
    @classmethod
    def open(cls, slug: str, *, attach_timeout_s: float = 30.0, open_timeout_s: float = 20.0) -> Link:
        t0 = time.monotonic()
        stack = ExitStack()
        try:
            ws = stack.enter_context(
                connect(
                    status.serial_url(slug),
                    user_agent_header=status.USER_AGENT,
                    open_timeout=open_timeout_s,
                    max_size=None,
                    compression=None,
                )
            )
            # From here until the link is handed back, ANY exception (KeyboardInterrupt, SystemExit, a bug)
            # closes the connection: a half-open link nobody holds would stay attached to the board.
            link = cls(slug, ws, stack, t0)
            link._wait_attached(attach_timeout_s)
            link.connect_s = time.monotonic() - t0
            return link
        except (OSError, WebSocketException, TimeoutError) as e:
            stack.close()
            raise BoardError(f"cannot connect to {slug}'s serial WebSocket: {e}") from e
        except BaseException:
            stack.close()
            raise

    def _wait_attached(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while not any(n.get("event") == "board" for n in self.notices):
            if time.monotonic() >= deadline:
                raise BoardError(f"{self.slug}: not attached to the serial bridge after {timeout_s:.1f} s")
            self._fill(deadline)
        board = next(n for n in self.notices if n.get("event") == "board")
        if board.get("present") is not True:
            raise BoardError(f"{self.slug}: the daemon says no board is present: {board}")

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._stack.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def kill(self, reason: str) -> None:
        """End the connection now, from any thread (the session's watcher at the cap): the socket is shut
        down, so the board's serial bridge drops us at once and the body's next operation raises. The link
        still has to be closed by its owner."""
        self.killed = True
        self._mark_lost()
        try:
            self._ws.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._log("notice", f"killed: {reason}")

    def _lost_error(self, e: BaseException) -> BaseException:
        """The error for an operation that found the connection closed: the reason we cut it ourselves (the cap's
        SessionTimeLimit) if we did, else LinkLost."""
        self._mark_lost()
        if self.killed and self._abort is not None:
            return self._abort
        return LinkLost(f"{self.slug}: connection lost: {e}")

    def _mark_lost(self) -> None:
        if not self.lost:
            self.lost = True
            self.lost_utc = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    # -- abort --
    def abort(self, exc: BaseException) -> None:
        if self._abort is None:
            self._abort = exc

    @property
    def aborted(self) -> BaseException | None:
        return self._abort

    def check(self) -> None:
        """Raise if the session must stop: aborted (a visitor, a blind watcher), past its deadline, or its
        status watcher is no longer running. Every send and read calls this; a session body doing long
        host-side work calls it too, at least once per status poll interval."""
        if self._abort is not None:
            raise self._abort
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.abort(SessionTimeLimit(f"{self.slug}: connected-time cap reached"))
            raise self._abort
        if self.watch is not None and not self.watch.is_alive():
            self.abort(WatchLost(f"{self.slug}: the status watcher is not running"))
            raise self._abort
        if self.watch is not None and time.monotonic() - self.watch.last_ok > self.watch.blind_s:
            # Judged here, on the clock, not by the watcher: a watcher stuck inside a status read never gets to.
            self.abort(WatchLost(f"{self.slug}: no status read has succeeded for {self.watch.blind_s:.0f} s"))
            raise self._abort

    # -- bytes --
    def _log(self, direction: str, data) -> None:
        size = len(data)
        if self._transcript_bytes + size > TRANSCRIPT_CAP:
            return
        self._transcript_bytes += size
        text = data if isinstance(data, str) else data.decode("latin-1")
        self.transcript.append({"t": round(time.monotonic() - self._t0, 3), "dir": direction, "data": text})

    def write(self, data: bytes) -> None:
        """Send bytes to a running board script (binary data, say). Marks the link dirty: what a script does
        with data cannot be known here, so leaving will soft-reset the board."""
        self.dirty = True
        self.changes.append(f"binary write of {len(data)} bytes")
        self._send(data)

    def _send(self, data: bytes, *, honour_abort: bool = True) -> None:
        if honour_abort:
            self.check()
        # A send can block for ever if the far end stops reading (TCP fills up), so each one runs in a helper
        # thread with a bound: the session's deadline for ordinary writes, CLEANUP_SEND_TIMEOUT_S for the
        # clean-up's few bytes. A send that does not complete in time cuts the connection.
        if honour_abort:
            timeout = SEND_TIMEOUT_S
            if self.deadline is not None:
                timeout = min(timeout, max(0.05, self.deadline - time.monotonic()))
        else:
            timeout = CLEANUP_SEND_TIMEOUT_S
        with self._write_lock:
            done = threading.Event()
            failed: list[BaseException] = []

            def run() -> None:
                try:
                    self._ws.send(bytes(data))
                except BaseException as e:  # noqa: BLE001 - handed to the caller
                    failed.append(e)
                finally:
                    done.set()

            threading.Thread(target=run, name="tthfv-send", daemon=True).start()
            try:
                finished = done.wait(timeout)
            except BaseException:
                # Interrupted (a signal) while the frame is in flight: let it finish first, or the clean-up's
                # bytes could overtake it on the wire.
                done.wait(CLEANUP_SEND_TIMEOUT_S)
                raise
            if not finished:
                self.kill(f"a write of {len(data)} bytes did not complete in {timeout:.1f} s")
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    self.abort(SessionTimeLimit(f"{self.slug}: connected-time cap reached during a write"))
                    raise self._abort
                raise LinkLost(
                    f"{self.slug}: a write of {len(data)} bytes did not complete in {timeout:.1f} s"
                )
            if failed:
                e = failed[0]
                if isinstance(e, ConnectionClosed):
                    raise self._lost_error(e) from e
                raise e
        self.sent_any = True
        self._log("tx", data)

    def _fill(self, deadline: float, *, honour_abort: bool = True) -> bool:
        """Wait (until `deadline`) for one frame; True if serial bytes were added to the buffer."""
        if honour_abort:
            self.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            msg = self._ws.recv(timeout=min(POLL_S, remaining))
        except TimeoutError:
            return False
        except ConnectionClosed as e:
            raise self._lost_error(e) from e
        if isinstance(msg, str):
            self._log("notice", msg)
            try:
                notice = json.loads(msg)
            except ValueError:
                notice = {"event": "unparsed", "text": msg}
            notice = notice if isinstance(notice, dict) else {"event": "unparsed", "text": msg}
            self.notices.append(notice)
            if notice.get("event") == "error":
                raise BoardError(f"{self.slug}: the daemon says: {notice}")
            return False
        self._log("rx", msg)
        self._buf += msg
        return True

    def read_exact(self, n: int, timeout_s: float) -> bytes:
        deadline = time.monotonic() + timeout_s
        while len(self._buf) < n:
            if time.monotonic() >= deadline:
                raise BoardError(f"{self.slug}: timed out waiting for {n} bytes; got {len(self._buf)}")
            self._fill(deadline)
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def read_until(self, marker: bytes, timeout_s: float, *, honour_abort: bool = True) -> bytes:
        """The bytes before `marker`; the marker is consumed."""
        deadline = time.monotonic() + timeout_s
        while marker not in self._buf:
            if time.monotonic() >= deadline:
                raise BoardError(
                    f"{self.slug}: timed out waiting for {marker!r}; buffered {self._buf[-200:]!r}"
                )
            self._fill(deadline, honour_abort=honour_abort)
        out, self._buf = self._buf.split(marker, 1)
        return out

    def _collect(self, seconds: float, *, until: bytes | None = None, honour_abort: bool = True) -> bytes:
        """Everything that arrives within `seconds` (or until `until` appears in it), taken off the buffer."""
        deadline = time.monotonic() + seconds
        while until is None or until not in self._buf:
            if time.monotonic() >= deadline:
                break
            self._fill(deadline, honour_abort=honour_abort)
        out, self._buf = self._buf, b""
        return out

    # -- REPL --
    def friendly_prompt(self, timeout_s: float = BANNER_TIMEOUT_S) -> bytes:
        """Send a bare CR and return what came back, up to the friendly prompt (or the timeout)."""
        self._send(b"\r")
        return self._collect(timeout_s, until=FRIENDLY_PROMPT)

    def enter_raw(self, banner_timeout_s: float = BANNER_TIMEOUT_S) -> None:
        """Ctrl-C twice, Ctrl-A, and wait for the raw REPL banner. Without it, leave (Ctrl-B) and raise
        ReplNoPrompt with what was seen: no further escalation."""
        self._send(CTRL_C + CTRL_C)
        seen = self._collect(0.1)
        self._send(CTRL_A)
        deadline = time.monotonic() + banner_timeout_s
        while RAW_BANNER not in self._buf:
            if time.monotonic() >= deadline or len(self._buf) > BANNER_SCAN_CAP:
                seen += self._buf
                self._buf = b""
                self.no_prompt = True
                self._send(CTRL_B, honour_abort=False)
                raise ReplNoPrompt(f"{self.slug}: no raw REPL banner within {banner_timeout_s:.1f} s", seen)
            self._fill(deadline)
        _, self._buf = self._buf.split(RAW_BANNER, 1)
        self.in_raw = True

    def start_raw(
        self, source: str, timeout_s: float, *, changes_state: bool, label: str | None = None
    ) -> None:
        """Send a script and wait for its `OK`. Its stdout follows; read it, then call finish_raw.

        `label` names a state-changing script briefly, for an error that has to list what was done.
        `changes_state` has no default on purpose: every caller says whether the script changes the board (a pin,
        a PWM, a PIO program, the FPGA's configuration, a file...). If it does, leaving soft-resets the board."""
        if not self.in_raw:
            self.enter_raw()
        self.script_running = True  # from here until finish_raw has read the prompt
        if changes_state:
            self.dirty = True
            first = source.strip().splitlines()[0][:60] if source.strip() else ""
            self.changes.append(label or f"script {first!r} ({len(source)} bytes)")
        self._send(source.encode("utf-8") + CTRL_D)
        deadline = time.monotonic() + timeout_s
        while len(self._buf) < 2:
            if time.monotonic() >= deadline:
                raise BoardError(f"{self.slug}: no OK after the script; buffered {self._buf!r}")
            self._fill(deadline)
        if not self._buf.startswith(b"OK"):
            raise BoardError(f"{self.slug}: raw REPL protocol mismatch, expected OK: {self._buf[:200]!r}")
        self._buf = self._buf[2:]

    def finish_raw(self, timeout_s: float) -> bytes:
        """The rest of stdout, then stderr and the prompt, all within `timeout_s`. Raises BoardScriptError if
        stderr is not empty."""
        end = time.monotonic() + timeout_s
        stdout = self.read_until(CTRL_D, max(0.0, end - time.monotonic()))
        stderr = self.read_until(CTRL_D, max(0.0, end - time.monotonic()))
        prompt = self.read_exact(1, max(0.0, end - time.monotonic()))
        if prompt == b">":
            self.script_running = False  # finished, with or without a traceback
        else:
            # stdout held a 0x04, so what we took for stderr was more stdout. The script's real end is the next
            # 0x04 followed by the prompt: read to it, so the link is in step for the next script, and say so.
            self._buf = prompt + self._buf
            try:
                self.read_until(CTRL_D + b">", max(1.0, end - time.monotonic()))
                self.script_running = False
            except BoardError:
                self._buf = b""
            raise BoardFramingError(
                f"{self.slug}: the script's text output contained 0x04 (binary output needs a length-prefixed "
                "read_exact); resynchronised at the next prompt"
            )
        if stderr:
            raise BoardScriptError(stderr.decode("utf-8", "replace"), stdout)
        return stdout

    def exec_raw(
        self, source: str, timeout_s: float, *, changes_state: bool, label: str | None = None
    ) -> bytes:
        """Run a script with text output. `timeout_s` bounds the whole call (from sending the script to the
        prompt), and the session's deadline cuts it shorter still (SessionTimeLimit)."""
        end = time.monotonic() + timeout_s
        self.start_raw(source, timeout_s, changes_state=changes_state, label=label)
        return self.finish_raw(max(0.0, end - time.monotonic()))

    # -- leaving --
    def cleanup(self, *, full: bool, interrupt: bool = True, timeout_s: float | None = None) -> dict:
        """Ctrl-C twice if `interrupt` (stop a script of ours that may still run), Ctrl-B (leave the raw REPL
        for the friendly REPL), and, if `full`, Ctrl-D from the friendly REPL, which soft-resets the board and runs the SDK's main.py (a Ctrl-D
        sent from the raw REPL would leave the board without the SDK). Never raises for a board that does not
        answer; says so in the report instead."""
        timeout_s = CLEANUP_TIMEOUT_S if timeout_s is None else timeout_s
        t0 = time.monotonic()
        report: dict = {"full": full, "sent": [], "friendly_prompt": False}
        if full:
            report.update({"soft_reboot": False, "prompt_after": False})
        received = b""
        try:
            if interrupt or full:
                self._send(CTRL_C + CTRL_C, honour_abort=False)
                report["sent"].append("03 03")
                received += self._buf + self._collect(0.3, honour_abort=False)
                self.script_running = False
            self._send(CTRL_B, honour_abort=False)
            report["sent"].append("02")
            got = self._collect(3.0, until=FRIENDLY_PROMPT, honour_abort=False)
            received += got
            report["friendly_prompt"] = FRIENDLY_PROMPT in got
            self.in_raw = False
            if not full:
                raise _Done
            self._send(CTRL_D, honour_abort=False)
            report["sent"].append("04")
            report["reset_sent_s"] = round(time.monotonic() - t0, 3)
            deadline = time.monotonic() + timeout_s
            after = b""
            while time.monotonic() < deadline:
                self._fill(deadline, honour_abort=False)
                after += self._buf
                self._buf = b""
                tail = after.rpartition(SOFT_REBOOT)[2] if SOFT_REBOOT in after else None
                if tail is not None and b"\n" + FRIENDLY_PROMPT in tail.replace(b"\r", b""):
                    report["prompt_after"] = True
                    break
            received += after
            report["soft_reboot"] = SOFT_REBOOT in after
        except _Done:
            pass
        except BoardError as e:
            report["error"] = str(e)
        report["seconds"] = round(time.monotonic() - t0, 3)
        report["received"] = received.decode("utf-8", "replace")
        return report

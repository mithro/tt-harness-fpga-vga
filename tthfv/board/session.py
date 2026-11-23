# SPDX-License-Identifier: Apache-2.0
"""`board_session`: the etiquette for a public, shared, unlocked board, in code.

1. Refuse unless the status rule says the board is usable (`status.usable`).
2. Take the claim.
3. Connect.
4. Confirm with the status that we are the only client (CONFIRM_AFTER_S explains the lag this allows for)
   and the USB serial is the expected one, else disconnect at once (nothing sent) and raise.
5. While the body runs, a background poll reads the status at most every `poll_s` (the daemon refreshes about
   every 5 s); if `clients` exceeds 1 the link is aborted and the body's next read raises `BoardTaken`.
6. A hard cap on connected time (`max_connected_s`) aborts the session with `SessionTimeLimit`.
7. On EVERY exit path (normal, exception, KeyboardInterrupt, BoardTaken): the clean-up sequence, close,
   release the claim. A session that changed the board (`link.dirty`: a script run with `changes_state=True`,
   or a binary `write`) gets Ctrl-C twice, Ctrl-B, then Ctrl-D from the friendly REPL, so the SDK's main.py
   runs. A read-only session gets only Ctrl-C twice and Ctrl-B: the board is left at the friendly REPL with
   what it had, and the daemon's idle display restores itself from there (it asks the board what is enabled
   once the board has been quiet 60 s; `idle_display` means `loaded`, and the SDK's start state means it
   reloads), so a reset would only disturb a visitor who arrived meanwhile.
8. Then poll the status for up to `restore_timeout_s` until `clients` is 0 and the idle display `loaded`;
   raise `BoardNotRestored` if that never happens.

Rules for session bodies (the code inside `with board_session(...) as link:`):

- Touch the link (any send or read, or `link.check()`) at least once per status poll interval (5 s): that
  is where an abort (a visitor, the time cap, a blind watcher) takes effect. A body that does not touch the
  link is only stopped when it returns.
- Keep long host-side work (building, comparing, writing files) outside the session; connected time is
  capped and every second of it keeps the board from the public.
- Say truthfully whether each script changes the board (`changes_state=`); anything that touches a pin, a
  PWM, a PIO program or the FPGA does.
- In a board script, never disable the keyboard interrupt (`micropython.kbd_intr(-1)`) and never read
  stdin: the clean-up's Ctrl-C must always be able to stop it, and a visitor's bytes must not be taken as
  data.

Signals: for the life of the session SIGTERM and SIGHUP raise SessionTerminated, which the session handles
like KeyboardInterrupt (clean-up, close, release) and re-raises; the previous handlers are restored on exit.
Python can only install signal handlers in the main thread: a session run in another thread keeps the
process's handlers as they are, so a SIGTERM there ends the process without clean-up. SIGKILL can never
be handled: it leaves the claim file until it expires (at most 30 minutes) and the board in whatever state
the session had put it (the raw REPL, and anything a dirty session changed) until the daemon or a visitor
resets it.

A board that never showed the raw REPL banner is left with Ctrl-B alone (no reset storms), and a connection over
which nothing was sent gets no clean-up bytes at all.
"""

from __future__ import annotations

import datetime as dt
import math
import signal
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Self

from tthfv.board import (
    BoardError,
    BoardLeftDirty,
    BoardNotRestored,
    BoardNotUsable,
    BoardTaken,
    SessionTerminated,
    SessionTimeLimit,
    WatchLost,
    claims,
    status,
)
from tthfv.board.link import Link

# The cap on connected time, counted from the moment the WebSocket was opened and enforced by the link on
# every operation. 540 s leaves a minute for the clean-up inside the plan's ten-minute bursts.
MAX_CONNECTED_S = 540.0
# The public status lags a new connection: on fpga-4 on 2026-11-23 a read 5 s after our WebSocket opened
# still showed clients 0, and the next, 10 s after, showed 1. So the first lone `1` may be a visitor who
# connected shortly before us, with us not yet counted. Before the first byte is sent the session wants a
# read taken at least CONFIRM_AFTER_S after its connection opened (half again the worst lag seen) that
# shows exactly 1, and no read since the connection showing more than 1. Without one by
# CONFIRM_TIMEOUT_S it leaves without having sent anything.
CONFIRM_AFTER_S = 15.0
CONFIRM_TIMEOUT_S = 40.0
RESTORE_TIMEOUT_S = 120.0
# Without a successful status read for this long the session cannot see a visitor arrive, so it stops.
STATUS_BLIND_S = 10.0


def _utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class SessionReport:
    slug: str = ""
    purpose: str = ""
    started_utc: str = ""
    status_before: dict = field(default_factory=dict)
    claim_file: str = ""
    connected_utc: str = ""
    connect_s: float | None = None
    verified_clients: int | None = None
    verify_s: float | None = None
    confirm_s: float | None = None  # from the WebSocket opening to the confirming read
    polls: list[dict] = field(default_factory=list)
    cleanup: dict = field(default_factory=dict)
    disconnected_utc: str = ""
    disconnected_at: float | None = field(default=None, repr=False)  # monotonic
    connected_s: float | None = None
    claim_released_utc: str = ""
    restore: list[dict] = field(default_factory=list)
    time_to_loaded_s: float | None = None
    status_after: dict = field(default_factory=dict)
    outcome: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _snap(t0: float, s: status.BoardStatus) -> dict:
    return {"t": round(time.monotonic() - t0, 2), "utc": _utc(), "clients": s.clients, "state": s.idle_state}


def _verify(
    slug: str,
    want: str,
    link: Link,
    report: SessionReport,
    t0: float,
    *,
    after_s: float,
    timeout_s: float,
    poll_s: float,
) -> None:
    """Confirm that the status counts exactly us (see CONFIRM_AFTER_S), on the right board, before anything is
    sent. Any read since our connection opened that shows more than 1 client means someone else is there."""
    while True:
        asked = time.monotonic()
        s = status.read(slug)
        since_open = asked - link.opened_at
        report.polls.append({**_snap(t0, s), "phase": "verify", "after_open_s": round(since_open, 2)})
        if s.usb_serial != want:
            raise BoardError(f"{slug} now reports usb serial {s.usb_serial}, expected {want}")
        if s.clients is not None and s.clients > 1:
            raise BoardTaken(f"{slug} shows clients {s.clients} after we connected: someone else is there")
        if s.clients == 1 and since_open >= after_s:
            report.verified_clients = 1
            report.verify_s = round(time.monotonic() - t0, 2)
            report.confirm_s = round(since_open, 2)
            return
        if time.monotonic() - link.opened_at >= timeout_s:
            raise BoardError(
                f"{slug}: could not confirm we are the only client within {timeout_s:.0f} s "
                f"(last read: clients {s.clients}); left without sending anything"
            )
        # Normally one poll interval; but if the confirming moment falls inside the next interval, read right
        # after it instead of a whole interval later (the status rate limit still applies).
        now = time.monotonic()
        target = link.opened_at + after_s + 0.05
        time.sleep(target - now if now + poll_s > target > now else poll_s)


class _Watch(threading.Thread):
    """Background status poll while the body runs. Any failure to read counts as a failed read; after
    STATUS_BLIND_S without a good one the link is aborted. If this thread dies anyway, the link notices
    (`Link.check`) and refuses to go on. The connected-time cap is enforced by the link's own deadline; the
    watcher only aborts on it too, so that a body which does not touch the link still finds the abort."""

    def __init__(self, slug: str, want: str, link: Link, poll_s: float, report, t0: float):
        super().__init__(name=f"tthfv-watch-{slug}", daemon=True)
        self.slug, self.want, self.link, self.poll_s = slug, want, link, poll_s
        self.report, self.t0 = report, t0
        self.stopped = threading.Event()
        self.last_ok = time.monotonic()  # the last successful status read; Link.check judges blindness
        self.blind_s = STATUS_BLIND_S

    def run(self) -> None:
        while not self.stopped.wait(self.poll_s):
            if self.link.deadline is not None and time.monotonic() >= self.link.deadline:
                self.link.abort(SessionTimeLimit(f"{self.slug}: connected-time cap reached"))
                return
            try:
                s = status.read(self.slug)
            except Exception as e:  # noqa: BLE001 - whatever went wrong, we did not see the status
                self.report.polls.append(
                    {
                        "t": round(time.monotonic() - self.t0, 2),
                        "utc": _utc(),
                        "error": f"{type(e).__name__}: {e}",
                    }
                )
                if time.monotonic() - self.last_ok >= self.blind_s:
                    self.link.abort(
                        WatchLost(f"{self.slug}: status unreadable for {STATUS_BLIND_S:.0f} s (last: {e})")
                    )
                    return
                continue
            self.last_ok = time.monotonic()
            if self.stopped.is_set():
                return
            self.report.polls.append({**_snap(self.t0, s), "phase": "watch"})
            if s.clients is not None and s.clients > 1:
                self.link.abort(
                    BoardTaken(f"{self.slug} shows clients {s.clients}: another client connected")
                )
                return
            if s.usb_serial != self.want:
                self.link.abort(BoardError(f"{self.slug} now reports usb serial {s.usb_serial}"))
                return

    def stop(self) -> None:
        self.stopped.set()


def _wait_restored(slug: str, timeout_s: float, report: SessionReport, t_disc: float, t0: float) -> str:
    """Poll after leaving. Returns "loaded"; "visitor" (a read taken CONFIRM_AFTER_S after we left, so past the
    status lag, shows someone else in use of the board); "left" (the daemon settled on `left: ...` or
    `failed: ...`); or "timeout"."""
    deadline = t_disc + timeout_s
    while True:
        asked = time.monotonic()
        try:
            s = status.read(slug)
        except BoardError as e:
            report.restore.append({"t": round(time.monotonic() - t0, 2), "utc": _utc(), "error": str(e)})
        else:
            report.restore.append(_snap(t0, s))
            report.status_after = s.raw
            if s.clients == 0 and s.idle_state == "loaded":
                report.time_to_loaded_s = round(time.monotonic() - t_disc, 1)
                return "loaded"
            if s.idle_state and s.idle_state.startswith(("left:", "failed:")):
                return "left"
            if (s.clients or 0) >= 1 and s.idle_state == "in use" and asked - t_disc >= CONFIRM_AFTER_S:
                return "visitor"
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _clean_up_ok(cleanup: dict) -> bool:
    """Did our clean-up do what it set out to do?"""
    if "error" in cleanup:
        return False
    if cleanup.get("skipped") == "nothing was sent":
        return True
    if "skipped" in cleanup:  # no raw REPL banner: we never knew where the board was
        return False
    if cleanup.get("full"):
        return bool(cleanup.get("soft_reboot") and cleanup.get("prompt_after"))
    return bool(cleanup.get("friendly_prompt"))


def _summary(cleanup: dict) -> dict:
    out = {k: v for k, v in cleanup.items() if k != "received"}
    if cleanup.get("received"):
        out["received_tail"] = cleanup["received"][-200:]
    return out


def _after_leaving(slug: str, link: Link | None, timeout_s: float, report: SessionReport, t0: float) -> None:
    """Step 8: wait for the board to come back, and say truthfully what is known. Raises BoardNotRestored only
    when the evidence points at us: the daemon settled on `left: ...`/`failed: ...`, our clean-up did not
    complete, or the board stayed `waiting` (or unreadable) past the limit."""

    def note(text: str) -> None:
        report.outcome = (report.outcome + "; " if report.outcome else "") + text

    if link is None:
        return
    if not link.sent_any:  # nothing of ours is on the board: do not wait for whoever is there to leave
        note("nothing sent, restore not awaited")
        return
    ok = _clean_up_ok(report.cleanup)
    result = _wait_restored(slug, timeout_s, report, report.disconnected_at, t0)
    if result == "loaded":
        if not ok:
            note("restored despite an incomplete clean-up")
        return
    if result == "visitor" and ok:
        note("restored-unverified: board in use by someone else")
        return
    last = report.restore[-1] if report.restore else {}
    why = {
        "left": f"the daemon reports {last.get('state')!r}",
        "visitor": "someone else is on the board and our clean-up did not complete",
        "timeout": f"not back to clients 0 and 'loaded' within {timeout_s:.0f} s",
    }[result]
    note("NOT RESTORED")
    raise BoardNotRestored(
        f"{slug}: {why} after we left (last read: clients {last.get('clients')}, state {last.get('state')!r}"
        f"{', error ' + last['error'] if 'error' in last else ''}); clean-up: {_summary(report.cleanup)}; "
        "stop all board work and report"
    )


def _reconnect_and_clean(
    slug: str, want: str, old: Link, report: SessionReport, t0: float, timing: dict
) -> dict:
    """The connection dropped after we changed the board. Wait (past the status lag) for the status to show
    nobody; then reconnect once, confirm as before, and send the full clean-up. If anyone else is there, touch
    nothing and raise BoardLeftDirty."""
    done = "; ".join(old.changes) or "state-changing work"

    def give_up(why: str) -> BoardLeftDirty:
        return BoardLeftDirty(
            f"{slug}: the connection dropped after this session changed the board ({done}); {why}; nothing more "
            "was sent. The board may be dirty: stop board work and report"
        )

    dropped = time.monotonic()
    while True:
        asked = time.monotonic()
        try:
            s = status.read(slug)
        except BoardError as e:
            report.polls.append({"t": round(time.monotonic() - t0, 2), "utc": _utc(), "error": str(e)})
        else:
            report.polls.append({**_snap(t0, s), "phase": "after-drop"})
            if s.clients == 0 and asked - dropped >= timing["after_s"]:
                break
        if time.monotonic() - dropped >= timing["timeout_s"]:
            return_why = (
                "someone else is connected"
                if report.polls[-1].get("clients")
                else "the status never showed the board empty"
            )
            raise give_up(return_why)
        time.sleep(timing["poll_s"])
    try:
        new = Link.open(slug, attach_timeout_s=timing["attach_timeout_s"])
    except BoardError as e:
        raise give_up(f"the reconnect failed: {e}") from e
    try:
        try:
            _verify(slug, want, new, report, t0, after_s=timing["after_s"], timeout_s=timing["timeout_s"],
                    poll_s=timing["poll_s"])  # fmt: skip
        except BoardError as e:
            raise give_up(f"after reconnecting: {e}") from e
        cleanup = new.cleanup(full=True)
        cleanup["reconnected"] = True
        return cleanup
    finally:
        new.close()
        report.disconnected_at = time.monotonic()
        report.disconnected_utc = _utc()


def _leave(
    link: Link | None, claim: claims.Claim, report: SessionReport, t_open: float, reconnect=None
) -> None:
    """The clean-up the link's state calls for, then close and release, whatever happens in between. A dropped
    connection is cleaned through `reconnect` (holding the claim) only if we had changed the board."""
    try:
        if link is not None:
            if link.lost:
                report.outcome = (report.outcome + "; " if report.outcome else "") + "connection lost"
                if link.dirty:
                    report.outcome += " after changing the board: reconnecting to clean up"
                    report.cleanup = reconnect(link)
                else:
                    report.cleanup = {
                        "skipped": "connection lost; nothing had been changed, the board is left as is"
                    }
            elif link.dirty:  # our pins or programs could fight whatever comes next: soft-reset
                report.cleanup = link.cleanup(full=True)
                if link.lost and not _clean_up_ok(report.cleanup):  # it dropped during the clean-up itself
                    report.outcome = (report.outcome + "; " if report.outcome else "") + "connection lost"
                    report.cleanup = reconnect(link)
            elif link.no_prompt:
                report.cleanup = {"skipped": "no raw REPL banner: left with Ctrl-B only"}
            elif link.sent_any:  # read-only: leave the raw REPL, no reset (the daemon restores anyway)
                report.cleanup = link.cleanup(full=False)
            else:
                report.cleanup = {"skipped": "nothing was sent"}
            if "error" in report.cleanup and not link.lost:
                report.outcome = (report.outcome + "; " if report.outcome else "") + (
                    f"clean-up incomplete: {report.cleanup['error']} (sent {report.cleanup.get('sent')})"
                )
    finally:
        if link is not None and not link.closed:
            link.close()
            report.disconnected_at = time.monotonic()
            report.disconnected_utc = _utc()
            report.connected_s = round(report.disconnected_at - t_open, 1)
        claims.release(claim)
        report.claim_released_utc = _utc()


class _Deferred:
    """While active (main thread only), SIGINT, SIGTERM and SIGHUP are recorded instead of acted on; the first
    one is re-raised by the caller once the protected work is done."""

    SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def __init__(self) -> None:
        self.pending: int | None = None
        self._old: dict | None = None

    def _record(self, signum, _frame) -> None:
        if self.pending is None:
            self.pending = signum

    def __enter__(self) -> Self:
        if threading.current_thread() is threading.main_thread():
            self._old = {s: signal.signal(s, self._record) for s in self.SIGNALS}
        return self

    def __exit__(self, *exc) -> None:
        if self._old is not None:
            for s, h in self._old.items():
                signal.signal(s, h)

    def exception(self) -> BaseException:
        return KeyboardInterrupt() if self.pending == signal.SIGINT else SessionTerminated(self.pending)


TERM_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


@contextmanager
def _terminate_as_exception() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield  # see the module docstring: handlers can only be installed in the main thread
        return

    def handler(signum, _frame):
        raise SessionTerminated(signum)

    old = {s: signal.signal(s, handler) for s in TERM_SIGNALS}
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)


@contextmanager
def board_session(slug: str, purpose: str, **kwargs) -> Iterator[Link]:
    """See the module docstring. Keyword arguments: max_connected_s, poll_s, restore_timeout_s,
    attach_timeout_s, confirm_after_s, confirm_timeout_s, report."""
    with _terminate_as_exception(), _board_session(slug, purpose, **kwargs) as link:
        yield link


@contextmanager
def _board_session(
    slug: str,
    purpose: str,
    *,
    max_connected_s: float = MAX_CONNECTED_S,
    poll_s: float = status.MIN_INTERVAL_S,
    restore_timeout_s: float = RESTORE_TIMEOUT_S,
    attach_timeout_s: float = 30.0,
    confirm_after_s: float | None = None,
    confirm_timeout_s: float | None = None,
    report: SessionReport | None = None,
) -> Iterator[Link]:
    report = report if report is not None else SessionReport()
    report.slug, report.purpose, report.started_utc = slug, purpose, _utc()
    t0 = time.monotonic()
    claims.require_shared_dir()
    if slug in status.FORBIDDEN:
        raise BoardNotUsable(f"{slug} belongs to another session: never use it")
    want = status.expected_serial(slug)
    if want is None:
        raise BoardNotUsable(f"{slug} is an unknown board")
    before = status.read(slug)
    report.status_before = before.raw
    ok, why = status.usable(before)
    if not ok:
        report.outcome = f"not usable: {why}"
        raise BoardNotUsable(why)
    minutes = min(claims.MAX_MINUTES, math.ceil((max_connected_s + restore_timeout_s + 60) / 60))
    claim = claims.take(slug, want, purpose, minutes)
    report.claim_file = claim.path.name
    link: Link | None = None
    primary: BaseException | None = None
    t_open = time.monotonic()
    try:
        link = Link.open(slug, attach_timeout_s=attach_timeout_s)
        report.connected_utc = _utc()
        report.connect_s = round(link.connect_s, 3)
        _verify(
            slug,
            want,
            link,
            report,
            t0,
            after_s=CONFIRM_AFTER_S if confirm_after_s is None else confirm_after_s,
            timeout_s=CONFIRM_TIMEOUT_S if confirm_timeout_s is None else confirm_timeout_s,
            poll_s=poll_s,
        )
        link.deadline = link.opened_at + max_connected_s
        watch = _Watch(slug, want, link, poll_s, report, t0)
        link.watch = watch
        watch.start()
        try:
            yield link
        finally:
            watch.stop()
            link.watch = None
            watch.join(status.HTTP_TIMEOUT_S + 2.0)  # it may be inside one status read when told to stop
        if link.aborted is not None:  # the body finished, but a visitor came or the cap passed meanwhile
            raise link.aborted
        report.outcome = "ok"
    except BaseException as e:
        primary = link.aborted if link is not None and link.aborted is not None else e
        report.outcome = (
            f"board taken: {e}"
            if isinstance(primary, BoardTaken)
            else f"{type(e).__name__}: {e}".rstrip(": ")
        )
        if primary is not e and isinstance(primary, BoardTaken):
            raise primary from e
        raise
    finally:
        deferred = _Deferred()
        timing = {
            "after_s": CONFIRM_AFTER_S if confirm_after_s is None else confirm_after_s,
            "timeout_s": CONFIRM_TIMEOUT_S if confirm_timeout_s is None else confirm_timeout_s,
            "poll_s": poll_s,
            "attach_timeout_s": attach_timeout_s,
        }
        with deferred:  # a Ctrl-C or SIGTERM now waits until the board is cleaned and the claim released
            try:
                _leave(
                    link,
                    claim,
                    report,
                    t_open,
                    lambda old: _reconnect_and_clean(slug, want, old, report, t0, timing),
                )
            except BoardLeftDirty:
                report.outcome += "; BOARD MAY BE DIRTY"
                raise
        if deferred.pending is not None:
            name = signal.Signals(deferred.pending).name
            report.outcome = (report.outcome + "; " if report.outcome else "") + (
                f"{name} during clean-up deferred until the clean-up and claim release were done; "
                "restore not checked"
            )
            raise deferred.exception() from primary
        if isinstance(primary, SessionTerminated):
            report.outcome += "; restore not checked (terminated)"
        else:
            try:
                _after_leaving(slug, link, restore_timeout_s, report, t0)
            except BoardNotRestored as e:
                raise e from primary

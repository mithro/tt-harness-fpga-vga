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
   runs. A read-only session sends the least that undoes what it did: nothing if it never entered the raw
   REPL; Ctrl-B alone at an idle raw prompt; Ctrl-C twice and Ctrl-B only if a script of ours may still be
   running (Ctrl-C would raise KeyboardInterrupt in whatever a just-arrived visitor started). The board is
   left at the friendly REPL with
   what it had, and the daemon's idle display restores itself from there (it asks the board what is enabled
   once the board has been quiet 60 s; `idle_display` means `loaded`, and the SDK's start state means it
   reloads), so a reset would only disturb a visitor who arrived meanwhile.
8. Then poll the status for up to `restore_timeout_s` until `clients` is 0 and the idle display `loaded`;
   raise `BoardNotRestored` if that never happens.

Residual risks. What the protocol cannot prevent (there is no lock): `clients` lags a connection by 5-10 s and
is polled every 5 s, so a visitor who arrives during our session can receive 10-15 s of our traffic before we
notice, including, for a session that changed the board's state, our clean-up's soft reset of the board. A
visitor who arrives in the last ~10 s before our confirming read is not seen by it either. After a dropped
connection a state-changing script of ours keeps running on the board for about 35 s while we wait past the lag
before reconnecting to clean up (or until someone acts, if a visitor is there and we do not reconnect). SIGKILL,
power loss or a crash of the host running the harness leave the board as it was and the claim file in place
until it expires (at most 30 minutes).

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

Signals: one handler (`_Signals`) serves SIGINT, SIGTERM and SIGHUP for the whole session. Outside the clean-up
SIGINT raises KeyboardInterrupt and SIGTERM/SIGHUP raise SessionTerminated, which the session handles like
KeyboardInterrupt (clean-up, close, release) and re-raises; the previous handlers are restored on exit. During
the clean-up and claim release a signal is deferred and re-raised once they are done, chained to any error the
clean-up itself raised, and the outcome says so; a signal during the restore check ends it and the outcome says
the return to `loaded` was not confirmed. Worst-case deferral: the ordinary clean-up takes at most about 25 s
(a fresh status read, up to 13 s, then 0.3 s + 3 s of Ctrl-C/Ctrl-B replies and up to 20 s for the SDK's
prompt after Ctrl-D); the reconnect after a dropped connection can take about 135 s (CONFIRM_TIMEOUT_S waiting
for the board to be empty, the attach, CONFIRM_TIMEOUT_S again to confirm, the clean-up), and a SECOND signal
during it ends the reconnect at once: the board is reported possibly dirty, the claim released, and the first
signal re-raised. Python can only install signal handlers in the main thread: a session run in another thread keeps the
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
# Status reads of a board are at least MIN_INTERVAL_S (5 s) apart, so they fall on a grid set by the read
# before connecting. Connecting CONNECT_LEAD_S before the next grid point makes a grid point fall just
# after CONFIRM_AFTER_S, so the confirming read is taken at about 15.3 s; connecting at once would put it
# at about 20 s. The wait (up to 5 s) happens before connecting, when it costs the board nothing.
CONNECT_LEAD_S = 0.3
# After we leave, `in use` counts as a visitor only in a read taken this long after our disconnect: before
# that the status may still be counting our own connection (the same 5-10 s lag).
RESTORE_VISITOR_AFTER_S = 15.0
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
    confirm_s: float | None = None  # from the WebSocket opening to the confirming read's ANSWER
    pre_connect_wait_s: float | None = None  # waiting to align with the status grid, before connecting
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
    check=None,
) -> None:
    """Confirm that the status counts exactly us (see CONFIRM_AFTER_S), on the right board, before anything is
    sent. Any read since our connection opened that shows more than 1 client means someone else is there."""
    while True:
        if check is not None:
            check()
        s = status.read(slug)
        since_open = (
            s.asked_at - link.opened_at
        )  # when the request really went out, after the rate-limit wait
        report.polls.append(
            {**_snap(t0, s), "phase": "verify", "after_open_s": round(since_open, 2),
             "taken_after_open_s": round(since_open, 2), "answered_after_open_s": round(s.answered_at - link.opened_at, 2)}
        )  # fmt: skip
        if s.usb_serial != want:
            raise BoardError(f"{slug} now reports usb serial {s.usb_serial}, expected {want}")
        if s.clients is not None and s.clients > 1:
            raise BoardTaken(f"{slug} shows clients {s.clients} after we connected: someone else is there")
        if s.clients == 1 and since_open >= after_s:
            report.verified_clients = 1
            report.verify_s = round(time.monotonic() - t0, 2)
            report.confirm_s = round(s.answered_at - link.opened_at, 2)
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
        _sleep(target - now if now + poll_s > target > now else poll_s, check)


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
        self.last_status: status.BoardStatus | None = None

    def run(self) -> None:
        """Poll until the session stops us, whatever aborts happen meanwhile: after an abort the session still
        needs to know who is there when it leaves. At the cap, cut the connection, so it really ends then even
        if the body never touches the link."""
        while True:
            wait = self.poll_s
            if self.link.deadline is not None and not self.link.killed:
                wait = max(0.0, min(wait, self.link.deadline - time.monotonic()))
            if self.stopped.wait(wait):
                return
            if (
                self.link.deadline is not None
                and time.monotonic() >= self.link.deadline
                and not self.link.killed
            ):
                self.link.abort(SessionTimeLimit(f"{self.slug}: connected-time cap reached"))
                self.link.kill("connected-time cap")
                self.report.polls.append({"t": round(time.monotonic() - self.t0, 2), "utc": _utc(),
                                          "event": "cap: connection cut"})  # fmt: skip
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
                        WatchLost(f"{self.slug}: status unreadable for {self.blind_s:.0f} s (last: {e})")
                    )
                continue
            self.last_ok = time.monotonic()
            self.last_status = s
            if self.stopped.is_set():
                return
            self.report.polls.append({**_snap(self.t0, s), "phase": "watch"})
            if s.clients is not None and s.clients > 1:
                self.link.abort(
                    BoardTaken(f"{self.slug} shows clients {s.clients}: another client connected")
                )
            elif s.usb_serial != self.want:
                self.link.abort(BoardError(f"{self.slug} now reports usb serial {s.usb_serial}"))

    def stop(self) -> None:
        self.stopped.set()


def _wait_restored(
    slug: str, timeout_s: float, report: SessionReport, t_disc: float, t0: float, stop=None
) -> str:
    """Poll after leaving. Returns "loaded"; "visitor" (a read taken RESTORE_VISITOR_AFTER_S after we left, past the
    status lag, shows someone else in use of the board); "left" (the daemon settled on `left: ...` or
    `failed: ...`); or "timeout"."""
    deadline = t_disc + timeout_s
    while True:
        if stop is not None:
            stop()
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
            if (
                (s.clients or 0) >= 1
                and s.idle_state == "in use"
                and asked - t_disc >= RESTORE_VISITOR_AFTER_S
            ):
                return "visitor"
        if time.monotonic() >= deadline:
            return "timeout"
        _sleep(min(0.05, max(0.0, deadline - time.monotonic())), stop)


def _clean_up_ok(cleanup: dict) -> bool:
    """Did our clean-up do what it set out to do?"""
    if "error" in cleanup:
        return False
    if cleanup.get("skipped") == "nothing was sent":
        return True
    if cleanup.get("state_unchanged"):
        return True
    if "skipped" in cleanup:
        return False
    if cleanup.get("full"):
        return bool(cleanup.get("soft_reboot") and cleanup.get("prompt_after"))
    return bool(cleanup.get("friendly_prompt"))


def _summary(cleanup: dict) -> dict:
    out = {k: v for k, v in cleanup.items() if k != "received"}
    if cleanup.get("received"):
        out["received_tail"] = cleanup["received"][-200:]
    return out


def _after_leaving(
    slug: str, link: Link | None, timeout_s: float, report: SessionReport, t0: float, stop=None
) -> None:
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
    result = _wait_restored(slug, timeout_s, report, report.disconnected_at, t0, stop)
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
    running = "a script of ours was mid-run" if old.script_running else "no script of ours was running"

    def give_up(why: str) -> BoardLeftDirty:
        return BoardLeftDirty(
            f"{slug}: the connection dropped at {old.lost_utc} after this session changed the board; {running}; "
            f"state-changing scripts: [{done}]; {why}; nothing more was sent. The board may be dirty: stop board "
            "work and report"
        )

    signals: _Signals = timing["signals"]
    signals.in_reconnect = True
    try:
        return _reconnect_inner(slug, want, report, t0, timing, give_up)
    except _SecondSignal as e:
        _note(report, f"a second signal ({signal.Signals(e.signum).name}) ended the reconnect")
        raise give_up(f"a second signal ({signal.Signals(e.signum).name}) ended the reconnect") from None
    finally:
        signals.in_reconnect = False


def _reconnect_inner(slug: str, want: str, report: SessionReport, t0: float, timing: dict, give_up) -> dict:
    dropped = time.monotonic()
    while True:
        timing["signals"].check_second()
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
        _sleep(timing["poll_s"], timing["signals"].check_second)
    try:
        new = Link.open(slug, attach_timeout_s=timing["attach_timeout_s"])
    except BoardError as e:
        raise give_up(f"the reconnect failed: {e}") from e
    timing["signals"].link = new
    try:
        try:
            _verify(slug, want, new, report, t0, after_s=timing["after_s"], timeout_s=timing["timeout_s"],
                    poll_s=timing["poll_s"], check=timing["signals"].check_second)  # fmt: skip
        except BoardError as e:
            raise give_up(f"after reconnecting: {e}") from e
        cleanup = new.cleanup(full=True)
        cleanup["reconnected"] = True
        return cleanup
    finally:
        timing["signals"].link = None
        new.close()
        report.disconnected_at = time.monotonic()
        report.disconnected_utc = _utc()


def _note(report: SessionReport, text: str) -> None:
    report.outcome = (report.outcome + "; " if report.outcome else "") + text


def _someone_else_there(link: Link, report: SessionReport) -> bool:
    """A fresh status read before anything is sent after an abort. We are still connected, so more than one
    client means someone else; an unreadable status counts as someone (send nothing we need not)."""
    try:
        s = status.read(link.slug)
    except BoardError as e:
        report.polls.append({"utc": _utc(), "phase": "leave", "error": str(e)})
        others = True
    else:
        report.polls.append({"utc": _utc(), "phase": "leave", "clients": s.clients, "state": s.idle_state})
        others = s.clients is None or s.clients > 1
    report.cleanup = {"others_present": others}
    return others


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
                    # Nothing of ours changed the board, so nothing is worth a reconnect. If we were in the raw
                    # REPL it stays there; that is harmless: the daemon's own task enters and leaves the raw REPL
                    # itself (Ctrl-C, Ctrl-A ... Ctrl-B) when it next asks the board, and a visitor's Commander
                    # sends Ctrl-B on attaching.
                    where = "left in the raw REPL" if link.in_raw else "left at the friendly REPL"
                    report.cleanup = {
                        "skipped": f"{where} after a dropped connection; no state was changed",
                        "state_unchanged": True,
                    }
                    _note(report, report.cleanup["skipped"])
            elif link.aborted is not None and _someone_else_there(link, report) and not link.dirty:
                report.cleanup = {"skipped": "someone else is connected: left without sending anything",
                                  "state_unchanged": True}  # fmt: skip
                _note(report, "someone else is connected: left without sending anything")
            elif link.dirty:  # our pins or programs could fight whatever comes next: soft-reset
                if link.aborted is not None and report.cleanup.get("others_present"):
                    _note(report, "full clean-up sent with someone else connected (the lesser harm: our changes "
                                  "could fight whatever they run)")  # fmt: skip
                report.cleanup = link.cleanup(full=True)
                if link.lost and not _clean_up_ok(report.cleanup):  # it dropped during the clean-up itself
                    report.outcome = (report.outcome + "; " if report.outcome else "") + "connection lost"
                    report.cleanup = reconnect(link)
            elif link.in_raw and link.script_running:  # read-only, but a script of ours may still run
                report.cleanup = link.cleanup(full=False, interrupt=True)
            elif link.in_raw:  # read-only and idle at the raw prompt: Ctrl-B alone, no Ctrl-C, no reset
                report.cleanup = link.cleanup(full=False, interrupt=False)
            elif link.no_prompt:
                report.cleanup = {
                    "skipped": "no raw REPL banner: left with Ctrl-B only",
                    "state_unchanged": True,
                }
            elif link.sent_any:
                report.cleanup = {
                    "skipped": "never in the raw REPL: nothing to undo",
                    "state_unchanged": True,
                }
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


class _SecondSignal(BaseException):
    """A second signal arrived while the session was reconnecting to clean up: give the reconnect up."""

    def __init__(self, signum: int):
        super().__init__(f"second signal {signal.Signals(signum).name}")
        self.signum = signum


class _Signals:
    """ONE handler for SIGINT, SIGTERM and SIGHUP for the whole session (main thread only), and it NEVER raises.

    A handler that raises can do so anywhere in the main thread: inside `threading.Event.wait`, a lock's
    acquire or release, `Thread.start`, the websockets library. An exception there can leave a lock held or
    released twice; under signal stress that hung the process with its claim held and the board in the raw REPL,
    or ended it with "release unlocked lock". So the handler only records the signal (and hands its exception to
    the open link, which raises it at its next check); the main thread raises it at its own safe points:
    `Link.check` (every send and read of the body), the polling waits of the session (pre-connect wait,
    confirmation, restore check), and after the clean-up. During the clean-up (`in_cleanup`) nothing is handed
    to the link: the signal waits until the board is left and the claim released. During the reconnect after a
    dropped connection (`in_reconnect`), a SECOND signal makes the reconnect's own waits give up.

    The handlers are installed and restored with the three signals blocked, so none slips past between calls."""

    SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def __init__(self) -> None:
        self.in_cleanup = False
        self.in_reconnect = False
        self.pending: int | None = None  # the first signal received
        self.exc: BaseException | None = None  # its exception, made once
        self.raised = False  # already raised at a safe point by the session
        self.count = 0
        self.last: int | None = None
        self.second: int | None = None
        self.link = None  # the session's current link, while one is open
        self._old: dict | None = None

    @staticmethod
    def _exception_for(signum: int) -> BaseException:
        return KeyboardInterrupt() if signum == signal.SIGINT else SessionTerminated(signum)

    def handler(self, signum, _frame) -> None:
        # Records only: see the class docstring. Never raise from here.
        self.count += 1
        self.last = signum
        if self.pending is None:
            self.pending = signum
            self.exc = self._exception_for(signum)
            if not self.in_cleanup and self.link is not None:
                self.link.interrupt(self.exc)
        elif self.in_reconnect:
            self.second = signum

    def check(self) -> None:
        """A safe point outside the clean-up: raise the recorded signal, once."""
        if self.exc is not None and not self.raised and not self.in_cleanup:
            self.raised = True
            raise self.exc

    def check_second(self) -> None:
        """A safe point in the reconnect: a second signal gives it up."""
        if self.in_reconnect and self.second is not None:
            raise _SecondSignal(self.second)

    def check_new(self, since: int) -> None:
        """A safe point in the restore check: a signal received after `since` ends it."""
        if self.count > since and self.last is not None:
            raise self._exception_for(self.last)

    def exception(self) -> BaseException:
        return self.exc if self.exc is not None else self._exception_for(self.pending)

    def __enter__(self) -> Self:
        if threading.current_thread() is threading.main_thread():
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, self.SIGNALS)
            try:
                self._old = {sig: signal.signal(sig, self.handler) for sig in self.SIGNALS}
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        return self

    def __exit__(self, *exc) -> None:
        if self._old is not None:
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, self.SIGNALS)
            try:
                for sig, h in self._old.items():
                    signal.signal(sig, h)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def _sleep(seconds: float, check=None) -> None:
    """Sleep in short steps, calling `check` (a safe point that may raise) before each."""
    end = time.monotonic() + seconds
    while True:
        if check is not None:
            check()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.01, remaining))


@contextmanager
def board_session(slug: str, purpose: str, **kwargs) -> Iterator[Link]:
    """See the module docstring. Keyword arguments: max_connected_s, poll_s, restore_timeout_s,
    attach_timeout_s, confirm_after_s, confirm_timeout_s, report."""
    signals = _Signals()
    with signals, _board_session(slug, purpose, signals=signals, **kwargs) as link:
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
    signals: _Signals | None = None,
) -> Iterator[Link]:
    signals = signals if signals is not None else _Signals()  # not installed: a flag holder only
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
    signals.check()  # nothing to clean up yet
    claim = claims.take(slug, want, purpose, minutes)
    # From the moment the claim exists, everything is inside the try whose finally releases it: a signal in the
    # pre-connect wait (up to 5 s) must not leave the claim behind until it expires.
    link: Link | None = None
    primary: BaseException | None = None
    t_open: float | None = None
    try:
        report.claim_file = claim.path.name
        after = CONFIRM_AFTER_S if confirm_after_s is None else confirm_after_s
        wait = status.next_turn(slug) - CONNECT_LEAD_S - time.monotonic()
        report.pre_connect_wait_s = round(max(0.0, wait), 2) if after > 0 else 0.0
        if after > 0 and wait > 0:
            _sleep(wait, signals.check)
        signals.check()
        t_open = time.monotonic()
        link = Link.open(slug, attach_timeout_s=attach_timeout_s)
        signals.link = link
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
            check=signals.check,
        )
        link.deadline = link.opened_at + max_connected_s
        watch = _Watch(slug, want, link, poll_s, report, t0)
        link.watch = watch
        watch.start()
        try:
            yield link
        finally:
            signals.in_cleanup = True  # from here on a signal waits until the board is left and released
            watch.stop()
            link.watch = None
            end = time.monotonic() + status.HTTP_TIMEOUT_S + 2.0  # it may be inside one status read
            while watch.is_alive() and time.monotonic() < end:
                time.sleep(0.01)
        if link.aborted is not None:  # the body finished, but a visitor came or the cap passed meanwhile
            raise link.aborted
        report.outcome = "ok"
    except BaseException as e:
        signals.in_cleanup = True
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
        signals.in_cleanup = True
        timing = {
            "after_s": CONFIRM_AFTER_S if confirm_after_s is None else confirm_after_s,
            "timeout_s": CONFIRM_TIMEOUT_S if confirm_timeout_s is None else confirm_timeout_s,
            "poll_s": poll_s,
            "attach_timeout_s": attach_timeout_s,
            "signals": signals,
        }
        leave_error: BaseException | None = None
        try:
            _leave(
                link,
                claim,
                report,
                t_open,
                lambda old: _reconnect_and_clean(slug, want, old, report, t0, timing),
            )
        except BaseException as e:  # noqa: BLE001 - re-raised below, after any deferred signal is accounted for
            leave_error = e
            if isinstance(e, BoardLeftDirty):
                _note(report, "BOARD MAY BE DIRTY")
        signals.in_cleanup = False
        sig = signals.exc
        if sig is not None and primary is not sig:
            # Received but not yet raised (during the clean-up, or while the body was not touching the link):
            # it is the session's final exception, with whatever else happened chained to it.
            name = signal.Signals(signals.pending).name
            confirmed = "confirmed" if _clean_up_ok(report.cleanup) else "not confirmed"
            _note(report, f"{name} received; deferred until the clean-up and claim release were done; "
                          f"clean-up {confirmed}; restore not checked")  # fmt: skip
            signals.raised = True
            raise sig from (leave_error or primary)
        if leave_error is not None:
            raise leave_error
        if isinstance(primary, SessionTerminated):
            _note(report, "restore not checked (terminated)")
        else:
            since = signals.count
            try:
                _after_leaving(slug, link, restore_timeout_s, report, t0, lambda: signals.check_new(since))
            except BoardNotRestored as e:
                raise e from primary
            except (KeyboardInterrupt, SessionTerminated) as e:
                name = "SIGINT" if isinstance(e, KeyboardInterrupt) else signal.Signals(e.signum).name
                _note(report, f"{name} during the restore check: return to 'loaded' not confirmed")
                raise

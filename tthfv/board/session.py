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

A board that never showed the raw REPL banner is left with Ctrl-B alone (no reset storms), and a connection over
which nothing was sent gets no clean-up bytes at all.
"""

from __future__ import annotations

import datetime as dt
import math
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

from tthfv.board import (
    BoardError,
    BoardNotRestored,
    BoardNotUsable,
    BoardTaken,
    SessionTimeLimit,
    WatchLost,
    claims,
    status,
)
from tthfv.board.link import Link

MAX_CONNECTED_S = 600.0
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
WATCH_JOIN_S = status.HTTP_TIMEOUT_S + 2.0  # the watcher may be inside one status read when told to stop


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
        time.sleep(poll_s)


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

    def run(self) -> None:
        last_ok = time.monotonic()
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
                if time.monotonic() - last_ok >= STATUS_BLIND_S:
                    self.link.abort(
                        WatchLost(f"{self.slug}: status unreadable for {STATUS_BLIND_S:.0f} s (last: {e})")
                    )
                    return
                continue
            last_ok = time.monotonic()
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


def _wait_restored(slug: str, timeout_s: float, report: SessionReport, t_disc: float, t0: float) -> bool:
    deadline = t_disc + timeout_s
    while True:
        try:
            s = status.read(slug)
        except BoardError as e:
            report.restore.append({"t": round(time.monotonic() - t0, 2), "utc": _utc(), "error": str(e)})
        else:
            report.restore.append(_snap(t0, s))
            report.status_after = s.raw
            if s.clients == 0 and s.idle_state == "loaded":
                report.time_to_loaded_s = round(time.monotonic() - t_disc, 1)
                return True
        if time.monotonic() >= deadline:
            return False


@contextmanager
def board_session(
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
    t_disc: float | None = None
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
            watch.join(WATCH_JOIN_S)
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
        try:
            if link is not None:
                if link.dirty:  # our pins or programs could fight whatever comes next: soft-reset
                    report.cleanup = link.cleanup(full=True)
                elif link.no_prompt:
                    report.cleanup = {"skipped": "no raw REPL banner: left with Ctrl-B only"}
                elif link.sent_any:  # read-only: leave the raw REPL, no reset (the daemon restores anyway)
                    report.cleanup = link.cleanup(full=False)
                else:
                    report.cleanup = {"skipped": "nothing was sent"}
        finally:
            if link is not None:
                link.close()
                t_disc = time.monotonic()
                report.disconnected_utc = _utc()
                report.connected_s = round(t_disc - t_open, 1)
            claims.release(claim)
            report.claim_released_utc = _utc()
        if t_disc is not None and not _wait_restored(slug, restore_timeout_s, report, t_disc, t0):
            last = report.restore[-1] if report.restore else {}
            msg = (
                f"{slug} did not return to clients 0 and 'loaded' within {restore_timeout_s:.0f} s "
                f"after we left (last: clients {last.get('clients')}, state {last.get('state')!r}"
                f"{', error ' + last['error'] if 'error' in last else ''}); stop all board work and report"
            )
            report.outcome = (report.outcome + "; " if report.outcome else "") + "NOT RESTORED"
            raise BoardNotRestored(msg) from primary

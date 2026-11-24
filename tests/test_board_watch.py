# SPDX-License-Identifier: Apache-2.0
"""The watcher and the session deadline (review fix C1): a blind watcher must stop the session."""

import threading
import time

import pytest
from board_fakes import FakeBoard

from tthfv.board import BoardError, SessionTimeLimit, WatchLost, session, status
from tthfv.board.session import SessionReport, board_session

FAST = {
    "poll_s": 0.05,
    "restore_timeout_s": 3.0,
    "confirm_after_s": 0.0,
    "confirm_timeout_s": 2.0,
    "attach_timeout_s": 2.0,
}


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(session, "CONFIRM_AFTER_S", 0.0)
    d = tmp_path / "claims"
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(d))
    return d


@pytest.fixture
def fake(cdir, monkeypatch):
    with FakeBoard() as f:
        f.env(monkeypatch)
        yield f


def _claims(cdir):
    return sorted(p.name for p in cdir.glob("*.json")) if cdir.exists() else []


@pytest.mark.parametrize("mode", ["badstatus", "incomplete"])
def test_status_read_wraps_a_malformed_response(fake, cdir, mode):
    fake.status_garbage = mode
    with pytest.raises(BoardError, match="fpga-4"):
        status.read("fpga-4")


@pytest.mark.parametrize("mode", ["badstatus", "incomplete"])
def test_malformed_status_mid_session_aborts_and_cleans_up(fake, cdir, monkeypatch, mode):
    monkeypatch.setattr(session, "STATUS_BLIND_S", 0.4)
    report = SessionReport()
    sent = 0
    with (
        pytest.raises(BoardError, match="status"),
        board_session("fpga-4", "t", report=report, **FAST) as link,
    ):
        link.exec_raw("print(1)", 5, changes_state=True)
        fake.status_garbage = mode
        threading.Timer(1.5, lambda: setattr(fake, "status_garbage", None)).start()
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < 3:
                link.exec_raw("print(2)", 5, changes_state=False)
                sent += 1
                time.sleep(0.02)
        finally:
            stopped_after = time.monotonic() - t0
    assert stopped_after < 1.0  # blind limit 0.4 s plus a poll, well before the loop's own end
    fake.settled_clients()  # let the fake finish handling our frames and close
    assert fake.repl.resets == 1  # full clean-up (the session was dirty)
    assert _claims(cdir) == []
    assert report.outcome != "ok"


def test_dead_watcher_stops_the_next_link_operation(fake, cdir, monkeypatch):
    monkeypatch.setattr(session._Watch, "run", lambda self: None)
    with pytest.raises(WatchLost), board_session("fpga-4", "t", **FAST) as link:
        time.sleep(0.1)
        link.exec_raw("print(1)", 5, changes_state=False)
    fake.settled_clients()  # let the fake finish handling our frames and close
    assert fake.repl.scripts == []
    assert _claims(cdir) == []


def test_link_enforces_the_cap_without_the_watcher(fake, cdir, monkeypatch):
    def idle(self):
        self.stopped.wait()

    monkeypatch.setattr(session._Watch, "run", idle)
    t0 = time.monotonic()
    with (
        pytest.raises(SessionTimeLimit),
        board_session("fpga-4", "t", max_connected_s=0.6, **FAST) as link,
    ):
        while True:
            link.exec_raw("print(1)", 5, changes_state=False)
    assert time.monotonic() - t0 < 5
    assert _claims(cdir) == []


def test_watcher_thread_is_joined_on_exit(fake, cdir):
    with board_session("fpga-4", "t", **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=False)
    assert not [t for t in threading.enumerate() if t.name.startswith("tthfv-watch")]


def test_check_raises_once_aborted(fake, cdir):
    from tthfv.board import BoardTaken

    with pytest.raises(BoardTaken), board_session("fpga-4", "t", **FAST) as link:
        link.check()
        link.abort(BoardTaken("visitor"))
        link.check()

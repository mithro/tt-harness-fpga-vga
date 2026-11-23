# SPDX-License-Identifier: Apache-2.0
"""Review round 2, item 1: a status read that blocks (a trickling or silent server) must not blind the session.
Constants at a fifth of the real ones: blind limit 2 s (10), HTTP total 1.6 s (8), polls 1 s (5)."""

import threading
import time
import types

import pytest
from board_fakes import FakeBoard

from tthfv.board import BoardError, WatchLost, session, status
from tthfv.board.link import Link
from tthfv.board.session import SessionReport, board_session

BLIND, HTTP, POLL = 2.0, 1.6, 1.0
FAST = {"poll_s": POLL, "restore_timeout_s": 4.0, "confirm_after_s": 0.0, "confirm_timeout_s": 3.0,
        "attach_timeout_s": 2.0}  # fmt: skip


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(status, "HTTP_TIMEOUT_S", HTTP)
    monkeypatch.setattr(session, "STATUS_BLIND_S", BLIND)
    monkeypatch.setattr(session, "CONFIRM_AFTER_S", 0.0)
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(tmp_path / "claims"))
    with FakeBoard() as f:
        f.env(monkeypatch)
        f.trickle_s = 0.2  # well inside the per-operation socket timeout
        yield f


@pytest.mark.parametrize("mode", ["trickle", "silent"])
def test_status_read_has_a_total_deadline(fake, mode):
    fake.status_garbage = mode
    t0 = time.monotonic()
    with pytest.raises(BoardError, match="fpga-4"):
        status.read("fpga-4")
    assert time.monotonic() - t0 < HTTP + 0.5


@pytest.mark.parametrize("mode", ["trickle", "silent"])
def test_blocked_status_reads_abort_the_session(fake, mode):
    report = SessionReport()
    with pytest.raises(WatchLost), board_session("fpga-4", "t", report=report, **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=False)
        fake.status_garbage = mode
        threading.Timer(BLIND + POLL + 1.5, lambda: setattr(fake, "status_garbage", None)).start()
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < 10:
                link.exec_raw("print(2)", 5, changes_state=False)
                time.sleep(0.05)
        finally:
            stopped_after = time.monotonic() - t0
    assert stopped_after < BLIND + POLL + 0.5


def test_check_goes_blind_on_the_clock_even_if_the_watcher_is_stuck(fake):
    with Link.open("fpga-4") as link:
        link.watch = types.SimpleNamespace(is_alive=lambda: True, last_ok=time.monotonic() - BLIND - 0.1,
                                           blind_s=BLIND)  # fmt: skip
        with pytest.raises(WatchLost, match="no status"):
            link.check()

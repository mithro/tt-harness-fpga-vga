# SPDX-License-Identifier: Apache-2.0
"""Review fix I5: one deadline from the moment the WebSocket opened; exec_raw's timeout is a total."""

import time

import pytest
from board_fakes import FakeBoard, FakeRepl

from tthfv.board import BoardError, SessionTimeLimit, session, status
from tthfv.board.link import Link
from tthfv.board.session import SessionReport, board_session

FAST = {"poll_s": 0.05, "restore_timeout_s": 2.0, "confirm_timeout_s": 3.0, "attach_timeout_s": 2.0}


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    d = tmp_path / "claims"
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(d))
    return d


def test_default_cap_leaves_room_for_the_clean_up_inside_ten_minutes():
    assert session.MAX_CONNECTED_S == 540.0


def test_cap_counts_from_the_websocket_opening_not_from_verification(cdir, monkeypatch):
    report = SessionReport()
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        with (
            pytest.raises(SessionTimeLimit),
            board_session(
                "fpga-4", "t", max_connected_s=1.0, confirm_after_s=0.6, report=report, **FAST
            ) as link,
        ):
            opened = link.opened_at
            assert link.deadline == pytest.approx(opened + 1.0)
            try:
                while True:
                    link.exec_raw("print(1)", 5, changes_state=False)
            finally:
                ended = time.monotonic() - opened
    assert 1.0 <= ended < 1.4


def _slow_board():
    """OK after 0.2 s, then 20 bytes of output 12 ms apart: each phase is short, the whole call is not."""
    return FakeBoard(repl=FakeRepl(run=lambda src: (b"x" * 20, b"")), chunk=1)


def test_exec_raw_timeout_is_a_total_for_the_call(monkeypatch):
    with _slow_board() as fake:
        fake.env(monkeypatch)
        with Link.open("fpga-4") as link:
            link.enter_raw()
            fake.delay_before_reply = {0x04: 0.2}
            fake.chunk_delay = 0.012
            t0 = time.monotonic()
            with pytest.raises(BoardError, match="timed out"):
                link.exec_raw("slow()", 0.3, changes_state=False)
            assert time.monotonic() - t0 < 0.5


def test_exec_raw_is_clipped_to_the_session_deadline(monkeypatch):
    with _slow_board() as fake:
        fake.env(monkeypatch)
        with Link.open("fpga-4") as link:
            link.enter_raw()
            fake.delay_before_reply = {0x04: 1.0}
            link.deadline = time.monotonic() + 0.3
            t0 = time.monotonic()
            with pytest.raises(SessionTimeLimit):
                link.exec_raw("slow()", 30, changes_state=False)
            assert time.monotonic() - t0 < 0.6

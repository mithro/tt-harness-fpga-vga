# SPDX-License-Identifier: Apache-2.0
"""Review fix I1: after leaving, tell our failure apart from a visitor arriving."""

import time

import pytest
from board_fakes import FakeBoard

from tthfv.board import BoardNotRestored, BoardTaken, session, status
from tthfv.board import link as link_mod
from tthfv.board.session import SessionReport, board_session

FAST = {"poll_s": 0.05, "restore_timeout_s": 2.0, "confirm_after_s": 0.0, "confirm_timeout_s": 2.0,
        "attach_timeout_s": 2.0}  # fmt: skip


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(session, "CONFIRM_AFTER_S", 0.0)
    monkeypatch.setattr(link_mod, "CLEANUP_TIMEOUT_S", 1.0)
    d = tmp_path / "claims"
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(d))
    return d


@pytest.fixture
def fake(cdir, monkeypatch):
    with FakeBoard() as f:
        f.env(monkeypatch)
        yield f


def _visitor_after_we_leave(board):
    if board.ws_connections and board.ws_clients == 0 and not board.visitors:
        board.visitors = 1


def test_nothing_sent_returns_promptly_even_with_a_visitor(fake, cdir):
    def hook(board):
        if board.ws_clients and not board.visitors:
            board.visitors = 1  # arrives as we connect, and stays

    fake.hook = hook
    report = SessionReport()
    t0 = time.monotonic()
    with pytest.raises(BoardTaken), board_session("fpga-4", "t", report=report, **FAST):
        pytest.fail("the body must not run")
    assert time.monotonic() - t0 < FAST["restore_timeout_s"]
    assert fake.repl.received == b""
    assert "restore not awaited" in report.outcome


def test_visitor_after_a_clean_exit_is_restored_unverified_not_an_error(fake, cdir):
    report = SessionReport()
    with board_session("fpga-4", "t", report=report, **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=True)
        fake.hook = _visitor_after_we_leave
    assert "restored-unverified: board in use by someone else" in report.outcome
    assert report.time_to_loaded_s is None


def test_left_state_is_not_restored_and_names_the_clean_up(fake, cdir):
    with (
        pytest.raises(BoardNotRestored, match="left: the SDK is not running") as e,
        board_session("fpga-4", "t", **FAST) as link,
    ):
        link.exec_raw("print(1)", 5, changes_state=True)
        fake.state_override = "left: the SDK is not running"
    assert "03 03" in str(e.value) and "soft_reboot" in str(e.value)


def test_persistent_waiting_is_not_restored(fake, cdir):
    with pytest.raises(BoardNotRestored, match="waiting"), board_session("fpga-4", "t", **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=False)
        fake.state_override = "waiting"


def test_failed_clean_up_and_a_visitor_is_not_restored(fake, cdir):
    with pytest.raises(BoardNotRestored, match="clean-up"), board_session("fpga-4", "t", **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=True)
        fake.repl.silent = True  # the board stops answering: no prompt after the clean-up
        fake.hook = _visitor_after_we_leave

# SPDX-License-Identifier: Apache-2.0
import json
import threading
import time

import pytest
from board_fakes import FakeBoard, FakeRepl

from tthfv.board import (
    BoardNotRestored,
    BoardNotUsable,
    BoardTaken,
    ClaimHeld,
    ReplNoPrompt,
    SessionTimeLimit,
    status,
)
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


def test_happy_path_runs_cleanup_releases_and_waits_for_loaded(fake, cdir):
    report = SessionReport()
    with board_session("fpga-4", "test", report=report, **FAST) as link:
        assert _claims(cdir) == ["fpga-4-a2961e5cac65b25f.json"]
        claim = json.loads((cdir / "fpga-4-a2961e5cac65b25f.json").read_text())
        assert claim["purpose"] == "test"
        assert link.exec_raw("print(1)", 5, changes_state=True) == b"ran\r\n"
    assert _claims(cdir) == []
    assert fake.ws_clients == 0
    assert fake.repl.resets == 1 and fake.repl.raw_resets == 0 and fake.repl.mode == "friendly"
    assert report.outcome == "ok"
    assert report.verified_clients == 1
    assert report.cleanup["soft_reboot"]
    assert report.time_to_loaded_s is not None
    assert report.restore[-1]["state"] == "loaded"


def test_refuses_when_not_usable(fake, cdir):
    fake.state_override = "in use"
    with pytest.raises(BoardNotUsable, match="in use"), board_session("fpga-4", "test", **FAST):
        pytest.fail("body must not run")
    assert fake.ws_connections == 0
    assert _claims(cdir) == []


def test_refuses_with_a_client_present(fake, cdir):
    fake.visitors = 1
    with pytest.raises(BoardNotUsable, match="clients 1"), board_session("fpga-4", "test", **FAST):
        pytest.fail("body must not run")
    assert fake.ws_connections == 0


def test_refuses_a_forbidden_board_without_asking(fake, cdir):
    with pytest.raises(BoardNotUsable, match="fpga-1"), board_session("fpga-1", "test", **FAST):
        pytest.fail("body must not run")
    assert fake.status_reads == 0


def test_refuses_when_claimed(fake, cdir):
    cdir.mkdir()
    (cdir / "fpga-4-p35.json").write_text("{}")
    with pytest.raises(ClaimHeld), board_session("fpga-4", "test", **FAST):
        pytest.fail("body must not run")
    assert fake.ws_connections == 0
    assert _claims(cdir) == ["fpga-4-p35.json"]


def test_body_raising_still_cleans_up_and_releases(fake, cdir):
    with pytest.raises(RuntimeError, match="body failed"), board_session("fpga-4", "test", **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=True)
        raise RuntimeError("body failed")
    assert fake.repl.resets == 1 and fake.repl.mode == "friendly"
    assert _claims(cdir) == []
    assert fake.ws_clients == 0


def test_interrupt_still_cleans_up_and_releases(fake, cdir):
    with pytest.raises(KeyboardInterrupt), board_session("fpga-4", "test", **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=True)
        raise KeyboardInterrupt
    assert fake.repl.resets == 1 and fake.repl.mode == "friendly"
    assert _claims(cdir) == []


def test_board_script_error_cleans_up(fake, cdir):
    from tthfv.board import BoardScriptError

    with pytest.raises(BoardScriptError, match="boom"), board_session("fpga-4", "test", **FAST) as link:
        link.exec_raw("raise ValueError('boom')", 5, changes_state=True)
    assert fake.repl.resets == 1
    assert _claims(cdir) == []


def test_visitor_joining_mid_session_aborts(fake, cdir):
    report = SessionReport()
    with pytest.raises(BoardTaken), board_session("fpga-4", "test", report=report, **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=True)
        fake.visitors = 1
        t0 = time.monotonic()
        threading.Timer(1.0, lambda: setattr(fake, "visitors", 0)).start()
        try:
            while time.monotonic() - t0 < 5:
                link.exec_raw("print(2)", 5, changes_state=True)
        finally:
            aborted_after = time.monotonic() - t0
    assert aborted_after < 1.0  # within a couple of polls
    assert report.outcome.startswith("board taken")
    assert fake.repl.resets == 1  # cleanup ran
    assert _claims(cdir) == []


def test_visitor_while_body_is_not_reading_still_raises(fake, cdir):
    with pytest.raises(BoardTaken), board_session("fpga-4", "test", **FAST):
        fake.visitors = 1
        threading.Timer(0.5, lambda: setattr(fake, "visitors", 0)).start()
        time.sleep(0.4)
    assert _claims(cdir) == []


def test_second_client_at_connect_disconnects_at_once_without_sending(fake, cdir):
    def hook(board):
        if board.ws_clients and board.visitors == 0 and not getattr(board, "_hooked", False):
            board._hooked = True
            board.visitors = 1
            threading.Timer(0.5, lambda: setattr(board, "visitors", 0)).start()

    fake.hook = hook
    with pytest.raises(BoardTaken, match="clients 2"), board_session("fpga-4", "test", **FAST):
        pytest.fail("body must not run")
    assert fake.repl.received == b""
    assert fake.ws_clients == 0
    assert _claims(cdir) == []


def test_time_cap_aborts(fake, cdir):
    with (
        pytest.raises(SessionTimeLimit),
        board_session("fpga-4", "test", max_connected_s=0.5, **FAST) as link,
    ):
        while True:
            link.exec_raw("print(1)", 5, changes_state=True)
    assert fake.repl.resets == 1
    assert _claims(cdir) == []


def test_not_restored_raises_after_releasing(fake, cdir):
    quick = {**FAST, "restore_timeout_s": 0.5}
    with (
        pytest.raises(BoardNotRestored, match="failed: stuck"),
        board_session("fpga-4", "test", **quick) as link,
    ):
        link.exec_raw("print(1)", 5, changes_state=True)
        fake.state_override = "failed: stuck"
    assert _claims(cdir) == []


def test_no_prompt_leaves_with_ctrl_b_only(cdir, monkeypatch):
    with FakeBoard(repl=FakeRepl(silent=True)) as f:
        f.env(monkeypatch)
        with pytest.raises(ReplNoPrompt), board_session("fpga-4", "test", **FAST) as link:
            link.enter_raw(banner_timeout_s=0.3)
        assert bytes(f.repl.received) == b"\x03\x03\x01\x02"
    assert _claims(cdir) == []

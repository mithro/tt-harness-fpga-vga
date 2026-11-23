# SPDX-License-Identifier: Apache-2.0
"""Review fix I4: the WebSocket drops mid-session."""

import pytest
from board_fakes import FakeBoard

from tthfv.board import BoardLeftDirty, LinkLost, session, status
from tthfv.board import link as link_mod
from tthfv.board.session import SessionReport, board_session

FAST = {"poll_s": 0.05, "restore_timeout_s": 2.0, "confirm_after_s": 0.0, "confirm_timeout_s": 2.0,
        "attach_timeout_s": 2.0}  # fmt: skip


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(session, "CONFIRM_AFTER_S", 0.0)
    monkeypatch.setattr(link_mod, "CLEANUP_TIMEOUT_S", 2.0)
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


def test_dirty_drop_reconnects_once_and_cleans_up_holding_the_claim(fake, cdir):
    held_during_reconnect = []

    def hook(board):
        if board.ws_connections == 1 and board.ws_clients == 0:  # after the drop, before reconnecting
            held_during_reconnect.append(bool(_claims(cdir)))

    report = SessionReport()
    with pytest.raises(LinkLost), board_session("fpga-4", "t", report=report, **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=True)
        fake.hook = hook
        fake.drop_all()
        link.exec_raw("print(2)", 5, changes_state=False)
    assert fake.ws_connections == 2
    assert fake.repl.resets == 1 and fake.repl.mode == "friendly"
    assert held_during_reconnect and all(held_during_reconnect)
    assert _claims(cdir) == []
    assert report.cleanup["soft_reboot"] and report.cleanup.get("reconnected") is True
    assert "connection lost" in report.outcome


def test_dirty_drop_with_someone_there_touches_nothing(fake, cdir):
    report = SessionReport()
    with (
        pytest.raises(BoardLeftDirty, match="print\\(1\\)"),
        board_session("fpga-4", "t", report=report, **FAST) as link,
    ):
        link.exec_raw("print(1)", 5, changes_state=True)
        fake.visitors = 1
        fake.drop_all()
        link.exec_raw("print(2)", 5, changes_state=False)
    assert fake.ws_connections == 1
    assert fake.repl.resets == 0
    assert _claims(cdir) == []


def test_clean_drop_does_not_reconnect(fake, cdir):
    with pytest.raises(LinkLost), board_session("fpga-4", "t", **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=False)
        fake.drop_all()
        link.exec_raw("print(2)", 5, changes_state=False)
    assert fake.ws_connections == 1
    assert fake.repl.resets == 0
    assert _claims(cdir) == []

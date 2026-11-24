# SPDX-License-Identifier: Apache-2.0
"""Review round 2, item 2: the watcher keeps watching after an abort, the cap really ends the connection, and
leaving looks at the status before sending anything after an abort."""

import time

import pytest
from board_fakes import FakeBoard

from tthfv.board import BoardTaken, SessionTimeLimit, session, status
from tthfv.board.session import SessionReport, board_session

POLL = 0.2
FAST = {"poll_s": POLL, "restore_timeout_s": 3.0, "confirm_after_s": 0.0, "confirm_timeout_s": 2.0,
        "attach_timeout_s": 2.0}  # fmt: skip


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(session, "CONFIRM_AFTER_S", 0.0)
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(tmp_path / "claims"))
    with FakeBoard() as f:
        f.env(monkeypatch)
        yield f


def _after(fake, marker: bytes) -> bytes:
    fake.settled_clients()
    return bytes(fake.repl.received).rsplit(marker, 1)[1]


def test_cap_ends_the_connection_even_if_the_body_never_touches_the_link(fake):
    with pytest.raises(SessionTimeLimit), board_session("fpga-4", "t", max_connected_s=1.0, **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=False)
        time.sleep(2.5)  # host-side work that never looks at the link
        link.check()
    assert fake.closed_at[0] - fake.opened_at[0] < 1.0 + POLL + 0.5


def test_watcher_keeps_polling_after_an_abort(fake):
    report = SessionReport()
    with pytest.raises(BoardTaken), board_session("fpga-4", "t", report=report, **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=False)
        fake.visitors = 1
        time.sleep(1.5)
        aborted_polls = [p for p in report.polls if p.get("phase") == "watch" and p.get("clients") == 2]
    assert len(aborted_polls) >= 3


def test_visitor_and_clean_link_sends_nothing(fake):
    report = SessionReport()
    with pytest.raises(BoardTaken), board_session("fpga-4", "t", report=report, **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=False)
        fake.visitors = 1
        time.sleep(0.8)  # the body does not notice; it just returns
    assert _after(fake, b"print(1)\x04") == b""
    assert "someone else" in report.outcome


def test_visitor_and_dirty_link_gets_the_full_clean_up_and_says_so(fake):
    report = SessionReport()
    with pytest.raises(BoardTaken), board_session("fpga-4", "t", report=report, **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=True)
        fake.visitors = 1
        time.sleep(0.8)
    assert _after(fake, b"print(1)\x04") == b"\x03\x03\x02\x04"
    assert "lesser harm" in report.outcome


def test_visitor_after_the_cap_closed_a_clean_link_gets_nothing(fake):
    with pytest.raises(SessionTimeLimit), board_session("fpga-4", "t", max_connected_s=0.8, **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=False)
        time.sleep(1.5)
        fake.visitors = 1
    assert _after(fake, b"print(1)\x04") == b""
    assert fake.ws_connections == 1

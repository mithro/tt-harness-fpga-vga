# SPDX-License-Identifier: Apache-2.0
"""Review fix C3: the status lags a connection, so the `1` seen first may be a visitor, not us. The session
confirms with a read taken well after its own connection opened, before it sends anything."""

import threading
import time

import pytest
from board_fakes import FakeBoard

from tthfv.board import BoardError, BoardTaken, session, status
from tthfv.board.session import SessionReport, board_session

# Scaled down from the real values (lag 5-10 s, confirm after 15 s, give up at 40 s).
LAG = 0.8
TIMING = {"poll_s": 0.05, "restore_timeout_s": 4.0, "attach_timeout_s": 2.0,
          "confirm_after_s": 1.5, "confirm_timeout_s": 4.0}  # fmt: skip


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    d = tmp_path / "claims"
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(d))
    return d


def test_normal_case_confirms_after_the_lag(cdir, monkeypatch):
    report = SessionReport()
    with FakeBoard(lag_s=LAG) as fake:
        fake.env(monkeypatch)
        with board_session("fpga-4", "t", report=report, **TIMING) as link:
            assert link.exec_raw("print(1)", 5, changes_state=False) == b"ran\r\n"
    assert report.verified_clients == 1
    assert report.confirm_s >= 1.5
    early = [p for p in report.polls if p["phase"] == "verify" and p["after_open_s"] < LAG]
    assert early and all(p["clients"] == 0 for p in early)  # the lag is real in the fake


def test_visitor_just_before_us_is_caught_and_nothing_is_sent(cdir, monkeypatch):
    with FakeBoard(lag_s=LAG) as fake:
        fake.env(monkeypatch)
        fake.visitors = 1  # connects 0.3 s before us; the status will not show them until 0.8 s later
        time.sleep(0.3)
        threading.Timer(2.5, lambda: setattr(fake, "visitors", 0)).start()
        report = SessionReport()
        with pytest.raises(BoardTaken), board_session("fpga-4", "t", report=report, **TIMING):
            pytest.fail("the body must not run")
        fake.settled_clients()  # let the fake finish handling our frames and close
        assert fake.repl.received == b""
        assert fake.settled_clients() == 0
    ones = [p for p in report.polls if p.get("clients") == 1]
    assert ones  # we did see a lone "1" first: it was the visitor
    assert report.cleanup.get("skipped") == "nothing was sent"


def test_confirming_read_lands_just_after_the_wait_not_a_poll_later(cdir, monkeypatch):
    """At a tenth of real time: polls and the status rate limit 0.5 s apart, confirm after 1.5 s. The read that
    confirms must be TAKEN at least 1.5 s after the connection opened, and its ANSWER (what confirm_s records)
    arrives just after that, not one rate-limit interval later (2.0 s)."""
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.5)
    monkeypatch.setattr(session, "CONNECT_LEAD_S", 0.03)
    report = SessionReport()
    timing = {**TIMING, "poll_s": 0.5}
    with FakeBoard(lag_s=LAG) as fake:
        fake.env(monkeypatch)
        with board_session("fpga-4", "t", report=report, **timing) as link:
            answered = time.monotonic() - link.opened_at
            link.check()
    confirming = [p for p in report.polls if p.get("phase") == "verify"][-1]
    assert confirming["taken_after_open_s"] >= 1.5
    assert 1.5 <= report.confirm_s < 1.65
    assert report.confirm_s <= answered + 0.005  # confirm_s is rounded to 0.01 s
    assert (
        report.pre_connect_wait_s < 0.5
    )  # the connection waited at most one interval to align with the grid


def test_never_confirmed_leaves_without_sending(cdir, monkeypatch):
    with FakeBoard(lag_s=30) as fake:  # the status never catches up
        fake.env(monkeypatch)
        with pytest.raises(BoardError, match="confirm"), board_session("fpga-4", "t", **TIMING):
            pytest.fail("the body must not run")
        fake.settled_clients()  # let the fake finish handling our frames and close
        assert fake.repl.received == b""

# SPDX-License-Identifier: Apache-2.0
"""Review fix C3: the status lags a connection, so the `1` seen first may be a visitor, not us. The session
confirms with a read taken well after its own connection opened, before it sends anything."""

import threading
import time

import pytest
from board_fakes import FakeBoard

from tthfv.board import BoardError, BoardTaken, status
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
        assert fake.repl.received == b""
        assert fake.ws_clients == 0
    ones = [p for p in report.polls if p.get("clients") == 1]
    assert ones  # we did see a lone "1" first: it was the visitor
    assert report.cleanup.get("skipped") == "nothing was sent"


def test_confirming_read_lands_just_after_the_wait_not_a_poll_later(cdir, monkeypatch):
    """At a tenth of real time: polls 0.5 s apart (rate limit too), confirm after 1.5 s: about 1.5 s, not 2.0."""
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.5)
    report = SessionReport()
    timing = {**TIMING, "poll_s": 0.5}
    with FakeBoard(lag_s=LAG) as fake:
        fake.env(monkeypatch)
        with board_session("fpga-4", "t", report=report, **timing) as link:
            link.check()
    assert 1.5 <= report.confirm_s < 1.75


def test_never_confirmed_leaves_without_sending(cdir, monkeypatch):
    with FakeBoard(lag_s=30) as fake:  # the status never catches up
        fake.env(monkeypatch)
        with pytest.raises(BoardError, match="confirm"), board_session("fpga-4", "t", **TIMING):
            pytest.fail("the body must not run")
        assert fake.repl.received == b""

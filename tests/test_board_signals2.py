# SPDX-License-Identifier: Apache-2.0
"""Review round 2, item 6: one signal handler for the whole session; deferred signals are never dropped; a
second signal ends a long reconnect; a signal during the restore check shows in the outcome."""

import os
import signal
import threading
import time

import pytest
from board_fakes import FakeBoard

from tthfv.board import BoardLeftDirty, SessionTerminated, session, status
from tthfv.board import link as link_mod
from tthfv.board.session import SessionReport, board_session

FAST = {"poll_s": 0.05, "restore_timeout_s": 3.0, "confirm_after_s": 0.0, "confirm_timeout_s": 2.0,
        "attach_timeout_s": 2.0}  # fmt: skip


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(session, "CONFIRM_AFTER_S", 0.0)
    monkeypatch.setattr(link_mod, "CLEANUP_TIMEOUT_S", 2.0)
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(tmp_path / "claims"))
    with FakeBoard() as f:
        f.env(monkeypatch)
        f.claims_dir = tmp_path / "claims"
        yield f


def _sigint_at(*delays):
    for d in delays:
        threading.Timer(d, os.kill, (os.getpid(), signal.SIGINT)).start()


def test_handler_records_and_safe_points_raise():
    """Round 3: the handler never raises; the session's safe points do."""
    s = session._Signals()
    s.handler(signal.SIGTERM, None)
    assert s.pending == signal.SIGTERM
    with pytest.raises(SessionTerminated):
        s.check()
    s.in_cleanup = True
    s.handler(signal.SIGINT, None)  # a second one during the ordinary clean-up: only counted
    s.check_second()  # not in a reconnect: nothing
    s.in_reconnect = True
    s.handler(signal.SIGHUP, None)
    with pytest.raises(session._SecondSignal):
        s.check_second()
    with pytest.raises(SessionTerminated):
        s.check_new(1)  # a signal after the first one


def test_handler_hands_the_first_signal_to_the_link_outside_the_clean_up():
    import types

    s = session._Signals()
    got = []
    s.link = types.SimpleNamespace(interrupt=got.append)
    s.handler(signal.SIGINT, None)  # no raise here
    assert len(got) == 1 and isinstance(got[0], KeyboardInterrupt)
    s2 = session._Signals()
    s2.link, s2.in_cleanup = types.SimpleNamespace(interrupt=got.append), True
    s2.handler(signal.SIGINT, None)  # during the clean-up nothing is handed over: it is deferred
    assert len(got) == 1


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_signal_inside_a_receive_still_allows_the_clean_up(fake, sig):
    """A signal raised inside the websockets library's recv would leave it believing a recv is still running, and
    the clean-up's own reads would then fail. The signal must take effect at the link's next check instead."""
    exc = KeyboardInterrupt if sig == signal.SIGINT else SessionTerminated
    stray = []
    # Outside the session a stray SIGTERM must not kill the test run: record it instead (the session installs its
    # own handler for its lifetime and restores this one after).
    previous = signal.signal(sig, lambda signum, frame: stray.append(signum))
    timer = threading.Timer(0.1, os.kill, (os.getpid(), sig))  # in the middle of a 0.2 s receive
    try:
        with pytest.raises(exc), board_session("fpga-4", "t", **FAST) as link:
            link.exec_raw("x = 1", 5, changes_state=True)
            timer.start()
            link.read_exact(1, 5)  # nothing will come
    finally:
        timer.cancel()
        signal.signal(sig, previous)
    assert stray == []  # it landed inside the session
    fake.settled_clients()
    assert bytes(fake.repl.received).endswith(b"\x03\x03\x02\x04")
    assert fake.repl.resets == 1


def test_pending_signal_survives_a_left_dirty_error(fake):
    report = SessionReport()
    with pytest.raises(KeyboardInterrupt) as e, board_session("fpga-4", "t", report=report, **FAST) as link:
        link.exec_raw("x = 1", 5, changes_state=True)
        fake.visitors = 1
        fake.drop_all()
        _sigint_at(0.5)  # lands in the reconnect wait, which then gives up (someone is there)
        link.exec_raw("print(2)", 5, changes_state=False)
    assert isinstance(e.value.__cause__, BoardLeftDirty)
    assert "SIGINT" in report.outcome and "BOARD MAY BE DIRTY" in report.outcome
    assert not list(fake.claims_dir.glob("*.json"))


def test_a_second_signal_ends_the_reconnect_wait(fake):
    report = SessionReport()
    t_drop = []
    with (
        pytest.raises(KeyboardInterrupt),
        board_session("fpga-4", "t", report=report, **{**FAST, "confirm_timeout_s": 20.0}) as link,
    ):
        link.exec_raw("x = 1", 5, changes_state=True)
        fake.visitors = 1
        fake.drop_all()
        t_drop.append(time.monotonic())
        _sigint_at(0.3, 0.8)
        link.exec_raw("print(2)", 5, changes_state=False)
    # Behaviour, not a tight wall-clock figure: the reconnect was abandoned because of the second signal (it says
    # so), long before its own 20 s wait would have ended (half of it is a bound with real margin under load).
    assert "second signal" in report.outcome and "BOARD MAY BE DIRTY" in report.outcome
    assert time.monotonic() - t_drop[0] < 10.0
    assert fake.ws_connections == 1  # it never reconnected
    assert not list(fake.claims_dir.glob("*.json"))


def test_signal_during_the_restore_check_shows_in_the_outcome(fake):
    report = SessionReport()
    with (
        pytest.raises(KeyboardInterrupt),
        board_session("fpga-4", "t", report=report, **{**FAST, "restore_timeout_s": 10.0}) as link,
    ):
        link.exec_raw("print(1)", 5, changes_state=False)
        fake.state_override = "waiting"
        _sigint_at(0.8)  # after the clean-up, during the restore poll
    assert report.outcome != "ok"
    assert "restore" in report.outcome and "SIGINT" in report.outcome

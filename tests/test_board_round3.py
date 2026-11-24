# SPDX-License-Identifier: Apache-2.0
"""Review round 3: the signal handler never raises at arbitrary points; the claim is released whatever lands in
the pre-connect wait; a stashed interrupt never cuts the clean-up; and the minor fixes."""

import fcntl
import os
import signal
import threading
import time

import pytest
from board_fakes import FakeBoard

from tthfv.board import BoardTaken, ClaimHeld, LinkLost, SessionTerminated, claims, session, status
from tthfv.board import link as link_mod
from tthfv.board.link import Link
from tthfv.board.session import SessionReport, board_session

FAST = {"poll_s": 0.05, "restore_timeout_s": 3.0, "confirm_after_s": 0.0, "confirm_timeout_s": 2.0,
        "attach_timeout_s": 2.0}  # fmt: skip
EXC = {signal.SIGINT: KeyboardInterrupt, signal.SIGTERM: SessionTerminated, signal.SIGHUP: SessionTerminated}


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


class _Guard:
    """Keep a signal sent to ourselves from killing the test run if it lands outside the session."""

    def __init__(self, sig):
        self.sig, self.stray = sig, []

    def __enter__(self):
        self.previous = signal.signal(self.sig, lambda s, f: self.stray.append(s))
        return self

    def __exit__(self, *exc):
        signal.signal(self.sig, self.previous)


# -- Critical 1: the handler only records --


def test_handler_never_raises_it_only_records():
    s = session._Signals()
    s.handler(signal.SIGTERM, None)  # no raise, wherever the main thread happens to be
    assert s.pending == signal.SIGTERM
    with pytest.raises(SessionTerminated):
        s.check()
    s.check()  # raised once, not again


def test_handler_hands_the_exception_to_the_link():
    import types

    got = []
    s = session._Signals()
    s.link = types.SimpleNamespace(interrupt=got.append)
    s.handler(signal.SIGINT, None)
    assert len(got) == 1 and isinstance(got[0], KeyboardInterrupt)


# -- Important 2: a signal in the pre-connect wait --


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_signal_in_the_pre_connect_wait_releases_the_claim(fake, cdir, monkeypatch, sig):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 1.5)  # the wait before connecting is about 1.2 s
    monkeypatch.setattr(status, "_last_read", {})  # no rate-limit wait left over from an earlier test
    monkeypatch.setattr(session, "CONNECT_LEAD_S", 0.3)
    timer = threading.Timer(0.4, os.kill, (os.getpid(), sig))
    with _Guard(sig) as guard:
        try:
            timer.start()
            with pytest.raises(EXC[sig]), board_session("fpga-4", "t", **{**FAST, "confirm_after_s": 0.5}):
                pytest.fail("the body must not run")
        finally:
            timer.cancel()
    assert guard.stray == []
    assert _claims(cdir) == []
    assert fake.ws_connections == 0


# -- Important 3: a stashed interrupt never fires inside the clean-up --


def test_stashed_interrupt_does_not_cut_the_clean_up(fake):
    with Link.open("fpga-4") as link:
        link.exec_raw("x = 1", 5, changes_state=True)
        link.interrupt(KeyboardInterrupt())
        report = link.cleanup(full=True)
    assert report["sent"] == ["03 03", "02", "04"]
    assert report["soft_reboot"] and report["prompt_after"]


def test_signal_in_the_last_reply_still_gets_a_complete_clean_up_and_is_named(fake):
    """The reviewer's stash2 scenario at a few offsets: a SIGINT lands while the body waits for its last reply."""
    for off in (0.1, 0.25, 0.4, 0.45):
        fake.repl.received.clear()
        fake.delay_before_reply = {}
        report = SessionReport()
        timer = threading.Timer(off, os.kill, (os.getpid(), signal.SIGINT))
        raised = None
        try:
            with board_session("fpga-4", "t", report=report, **FAST) as link:
                link.exec_raw("print(0)", 5, changes_state=True)
                fake.delay_before_reply = {0x04: 0.5}
                timer.start()
                link.exec_raw("print(1)", 5, changes_state=True)
        except KeyboardInterrupt as e:
            raised = e
        finally:
            timer.cancel()
        fake.delay_before_reply = {}
        fake.settled_clients(4.0)
        assert bytes(fake.repl.received).endswith(b"\x03\x03\x02\x04"), off
        assert raised is not None, off  # a signal was received: the session ends with it
        assert report.outcome != "ok" and "SIGINT" in report.outcome, (off, report.outcome)


# -- minor 4: close and release are independent; a stuck lock still releases our claim --


def test_close_raising_still_releases_the_claim(fake, cdir, monkeypatch):
    def bad_close(self):
        raise RuntimeError("close failed")

    with pytest.raises(RuntimeError, match="close failed"), board_session("fpga-4", "t", **FAST) as link:
        monkeypatch.setattr(link_mod.Link, "close", bad_close)
        link.exec_raw("print(1)", 5, changes_state=False)
    monkeypatch.undo()
    assert _claims(cdir) == []


def test_release_with_a_stuck_lock_still_removes_our_own_claim(cdir, monkeypatch):
    monkeypatch.setattr(claims, "LOCK_WAIT_S", 0.3)
    c = claims.take("fpga-4", "a2961e5cac65b25f", "t", 5)
    fd = os.open(cdir / claims.LOCK_NAME, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        claims.release(c)
        assert not c.path.exists()
        with pytest.raises(ClaimHeld):
            claims.take("fpga-4", "a2961e5cac65b25f", "t", 5)  # taking still needs the lock
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# -- minor 5: a signal received at any point ends the session with the signal --


def test_signal_with_a_visitor_abort_ends_with_the_signal(fake):
    report = SessionReport()
    with (
        _Guard(signal.SIGTERM) as guard,
        pytest.raises(SessionTerminated) as e,
        board_session("fpga-4", "t", report=report, **FAST) as link,
    ):
        link.exec_raw("x = 1", 5, changes_state=True)
        fake.visitors = 1
        time.sleep(0.4)  # the watcher aborts: board taken
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.2)  # the body returns without touching the link
    assert guard.stray == []
    assert isinstance(e.value.__cause__, BoardTaken)
    assert "board taken" in report.outcome and "SIGTERM" in report.outcome


# -- minor 6: script_running only once the script was sent; truthful words for a read-only script left running --


def test_abort_before_sending_is_not_a_running_script(fake):
    report = SessionReport()
    seen = {}
    with pytest.raises(BoardTaken), board_session("fpga-4", "t", report=report, **FAST) as link:
        link.enter_raw()
        link.abort(BoardTaken("visitor"))
        try:
            link.exec_raw("print(1)", 5, changes_state=False)
        finally:
            seen["running"] = link.script_running  # asserted below: an assert here would be masked
    assert seen["running"] is False
    assert report.cleanup.get("sent") == ["02"]  # no needless Ctrl-C
    fake.settled_clients()
    assert b"print(1)" not in bytes(fake.repl.received)


def test_read_only_script_left_running_after_a_drop_is_named(fake):
    report = SessionReport()
    with pytest.raises(LinkLost), board_session("fpga-4", "t", report=report, **FAST) as link:
        link.enter_raw()
        fake.delay_before_reply = {0x04: 1.0}
        threading.Timer(0.3, fake.drop_all).start()
        link.start_raw("long_read()", 5, changes_state=False)
    assert "a read-only script of ours may still be running" in report.outcome


# -- minor 7: a deferred signal says whether the clean-up was confirmed --


def test_deferred_signal_says_the_clean_up_was_not_confirmed(fake):
    report = SessionReport()
    with pytest.raises(KeyboardInterrupt), board_session("fpga-4", "t", report=report, **FAST) as link:
        link.exec_raw("x = 1", 5, changes_state=True)
        fake.repl.silent = True  # the board stops answering: the clean-up cannot be confirmed
        threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT)).start()  # lands in the clean-up
    assert "clean-up not confirmed" in report.outcome

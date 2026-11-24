# SPDX-License-Identifier: Apache-2.0
"""Review round 4: a signal received at any time inside board_session ends it with that signal, never a normal
return, and the outcome names it (it is not lost in the restore check)."""

import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from board_fakes import FakeBoard

from tthfv.board import BoardError, SessionTerminated, session, status
from tthfv.board.session import SessionReport, board_session

CHILD = Path(__file__).with_name("board_child_restore.py")
FAST = {"poll_s": 0.05, "restore_timeout_s": 5.0, "confirm_after_s": 0.0, "confirm_timeout_s": 2.0,
        "attach_timeout_s": 2.0}  # fmt: skip
EXC = {signal.SIGINT: KeyboardInterrupt, signal.SIGTERM: SessionTerminated, signal.SIGHUP: SessionTerminated}


def _one_child(sig, offset, tmp: Path) -> tuple[float, int, str]:
    with FakeBoard(quiet_reads=10) as fake:  # about 1 s of restore check after the body
        env = {**os.environ, "PYTHONPATH": str(CHILD.parent), "TTHFV_BOARD_CLAIMS": str(tmp / f"c-{sig}-{offset}"),
               "TTHFV_BOARD_BASE_URL": fake.base_url, "TTHFV_BOARD_WS_URL": fake.ws_url}  # fmt: skip
        child = subprocess.Popen([sys.executable, str(CHILD)], stdout=subprocess.PIPE, text=True, env=env)
        try:
            line = child.stdout.readline().strip()
            assert line == "BODY", line
            time.sleep(offset)
            child.send_signal(sig)
            out, _ = child.communicate(timeout=30)
        finally:
            if child.poll() is None:
                child.kill()
        return offset, child.returncode, out.strip()


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_a_signal_across_the_restore_check_is_never_lost(tmp_path, sig):
    offsets = [round(0.05 * i, 2) for i in range(27)]  # 0 to 1.3 s after the body: the whole restore check
    with ThreadPoolExecutor(9) as pool:
        results = list(pool.map(lambda o: _one_child(sig, o, tmp_path), offsets))
    lost = [(o, rc, out) for o, rc, out in results if rc == 0]
    assert lost == [], lost  # exit 0 means the session returned normally although it had been signalled
    name = signal.Signals(sig).name
    for o, rc, out in results:
        if out.startswith("SIGNALLED"):
            assert rc == 128 + sig, (o, rc, out)
            assert name in out and not out.split(" ", 2)[2].startswith("ok"), (o, out)
    assert any(out.startswith("SIGNALLED") for _, _, out in results)  # the offsets did reach into the session


# -- the same invariant at each boundary, injected in-process --


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(session, "CONFIRM_AFTER_S", 0.0)
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(tmp_path / "claims"))
    with FakeBoard() as f:
        f.env(monkeypatch)
        yield f


def _signal_self(sig):
    os.kill(os.getpid(), sig)
    time.sleep(0.01)  # let the handler run here


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("boundary", ["after_last_read", "after_after_leaving", "after_clearing_in_cleanup"])
def test_signal_at_each_boundary_ends_the_session_with_it(fake, monkeypatch, sig, boundary):
    if boundary == "after_last_read":
        real = session._wait_restored

        def wrapped(*a, **k):
            result = real(*a, **k)  # the last status read has returned 'loaded'
            _signal_self(sig)
            return result

        monkeypatch.setattr(session, "_wait_restored", wrapped)
    elif boundary == "after_after_leaving":
        real_al = session._after_leaving

        def wrapped_al(*a, **k):
            real_al(*a, **k)
            _signal_self(sig)

        monkeypatch.setattr(session, "_after_leaving", wrapped_al)
    else:

        class Hooked(session._Signals):
            _armed = False

            @property
            def in_cleanup(self):
                return self.__dict__.get("_ic", False)

            @in_cleanup.setter
            def in_cleanup(self, value):
                was = self.__dict__.get("_ic", False)
                self.__dict__["_ic"] = value
                if was and not value:
                    _signal_self(sig)

        monkeypatch.setattr(session, "_Signals", Hooked)
    report = SessionReport()
    with pytest.raises(EXC[sig]), board_session("fpga-4", "t", report=report, **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=False)
    name = signal.Signals(sig).name
    assert report.outcome.startswith(name), report.outcome
    assert not report.outcome.startswith("ok")


def test_outcome_begins_with_the_signal_when_deferred(fake):
    report = SessionReport()
    with pytest.raises(KeyboardInterrupt), board_session("fpga-4", "t", report=report, **FAST) as link:
        link.exec_raw("x = 1", 5, changes_state=True)
        fake.delay_before_reply = {0x02: 0.6}
        threading.Timer(0.4, os.kill, (os.getpid(), signal.SIGINT)).start()  # lands in the clean-up
    assert report.outcome.startswith("SIGINT received; clean-up confirmed"), report.outcome


def test_a_session_off_the_main_thread_is_refused(fake):
    caught = {}

    def run():
        try:
            with board_session("fpga-4", "t", **FAST):
                caught["ran"] = True
        except BaseException as e:  # noqa: BLE001
            caught["error"] = e

    t = threading.Thread(target=run)
    t.start()
    t.join(20)
    assert "ran" not in caught
    assert isinstance(caught.get("error"), BoardError) and "main thread" in str(caught["error"])
    assert fake.status_reads == 0 and fake.ws_connections == 0

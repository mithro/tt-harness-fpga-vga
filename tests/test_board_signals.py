# SPDX-License-Identifier: Apache-2.0
"""Review fixes C2 and C4: termination signals and a second interrupt still get the clean-up."""

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from board_fakes import FakeBoard

from tthfv.board import session, status
from tthfv.board.session import SessionReport, board_session

CHILD = Path(__file__).with_name("board_child.py")
FAST = {"poll_s": 0.05, "restore_timeout_s": 3.0, "confirm_after_s": 0.0, "confirm_timeout_s": 2.0,
        "attach_timeout_s": 2.0}  # fmt: skip


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    d = tmp_path / "claims"
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(d))
    return d


def _claims(cdir):
    return sorted(p.name for p in cdir.glob("*.json")) if cdir.exists() else []


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_termination_signal_runs_the_clean_up(cdir, monkeypatch, sig):
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        env = {**os.environ, "PYTHONPATH": str(CHILD.parent)}
        child = subprocess.Popen([sys.executable, str(CHILD)], stdout=subprocess.PIPE, text=True, env=env)
        try:
            assert child.stdout.readline().strip() == "BODY"
            assert _claims(cdir)  # the claim is held while the body runs
            child.send_signal(sig)
            out, _ = child.communicate(timeout=20)
        finally:
            if child.poll() is None:
                child.kill()
        assert child.returncode == 128 + sig
        assert f"TERMINATED {int(sig)}" in out
        assert bytes(fake.repl.received).endswith(b"\x03\x03\x02\x04")
        assert fake.repl.resets == 1 and fake.repl.mode == "friendly"
        assert fake.settled_clients() == 0
    assert _claims(cdir) == []


def test_handlers_are_restored_after_the_session(cdir, monkeypatch):
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        with board_session("fpga-4", "t", **FAST) as link:
            assert signal.getsignal(signal.SIGTERM) is not before[signal.SIGTERM]
            link.exec_raw("print(1)", 5, changes_state=False)
    assert {s: signal.getsignal(s) for s in before} == before


def test_session_outside_the_main_thread_still_works(cdir, monkeypatch):
    result = {}
    with FakeBoard() as fake:
        fake.env(monkeypatch)

        def run():
            with board_session("fpga-4", "t", **FAST) as link:
                result["out"] = link.exec_raw("print(1)", 5, changes_state=False)

        t = threading.Thread(target=run)
        t.start()
        t.join(20)
    assert result["out"] == b"ran\r\n"


def test_interrupt_in_the_body_still_waits_for_loaded(cdir, monkeypatch):
    report = SessionReport()
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        with pytest.raises(KeyboardInterrupt), board_session("fpga-4", "t", report=report, **FAST) as link:
            link.exec_raw("print(1)", 5, changes_state=True)
            raise KeyboardInterrupt
    assert report.time_to_loaded_s is not None
    assert session.SessionTerminated  # exported for callers

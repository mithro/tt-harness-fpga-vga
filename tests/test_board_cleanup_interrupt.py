# SPDX-License-Identifier: Apache-2.0
"""Review fix C4: an interrupt during the clean-up is deferred until the clean-up is done."""

import os
import signal
import threading
from pathlib import Path

import pytest
from board_fakes import FakeBoard

from tthfv.board import status
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


def test_interrupt_during_clean_up_is_deferred_until_done(cdir, monkeypatch):
    report = SessionReport()
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        fake.delay_before_reply = {0x02: 0.8}  # the board is slow to answer Ctrl-B
        with pytest.raises(KeyboardInterrupt), board_session("fpga-4", "t", report=report, **FAST) as link:
            link.exec_raw("print(1)", 5, changes_state=True)
            threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT)).start()  # lands inside the clean-up
        fake.settled_clients()  # let the fake finish handling our frames and close
        assert bytes(fake.repl.received).endswith(b"\x03\x03\x02\x04")
        assert fake.repl.resets == 1
    assert _claims(cdir) == []
    assert report.cleanup["soft_reboot"] and report.cleanup["prompt_after"]
    assert "deferred" in report.outcome and "restore not checked" in report.outcome
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler

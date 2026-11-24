# SPDX-License-Identifier: Apache-2.0
"""Review round 2, item 3: leave with the least that undoes what we did. 03 03 would raise KeyboardInterrupt in
whatever a visitor had just started, so it is sent only when a script of ours may still be running."""

import pytest
from board_fakes import FakeBoard, FakeRepl

from tthfv.board import BoardError, BoardScriptError, session, status
from tthfv.board.session import SessionReport, board_session

FAST = {"poll_s": 0.05, "restore_timeout_s": 3.0, "confirm_after_s": 0.0, "confirm_timeout_s": 2.0,
        "attach_timeout_s": 2.0}  # fmt: skip


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(session, "CONFIRM_AFTER_S", 0.0)
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(tmp_path / "claims"))


def _after(fake, marker: bytes) -> bytes:
    fake.settled_clients()
    return bytes(fake.repl.received).rsplit(marker, 1)[1]


def test_never_in_the_raw_repl_sends_nothing_more(cdir, monkeypatch):
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        report = SessionReport()
        with board_session("fpga-4", "t", report=report, **FAST) as link:
            link.friendly_prompt()
        fake.settled_clients()
        assert bytes(fake.repl.received) == b"\r"
    assert report.cleanup.get("skipped")


def test_idle_raw_repl_read_only_leaves_with_ctrl_b_only(cdir, monkeypatch):
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        report = SessionReport()
        with board_session("fpga-4", "t", report=report, **FAST) as link:
            link.exec_raw("print(1)", 5, changes_state=False)
        assert _after(fake, b"print(1)\x04") == b"\x02"
        assert fake.repl.mode == "friendly" and fake.repl.resets == 0
    assert report.cleanup["sent"] == ["02"]


def test_script_error_is_finished_so_ctrl_b_only(cdir, monkeypatch):
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        with pytest.raises(BoardScriptError), board_session("fpga-4", "t", **FAST) as link:
            link.exec_raw("raise ValueError('boom')", 5, changes_state=False)
        assert _after(fake, b"boom')\x04") == b"\x02"


def test_script_possibly_running_gets_interrupted(cdir, monkeypatch):
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        report = SessionReport()
        with board_session("fpga-4", "t", report=report, **FAST) as link:
            link.start_raw("print(1)", 5, changes_state=False)  # never finished
            assert link.script_running
        assert _after(fake, b"print(1)\x04") == b"\x03\x03\x02"
        assert fake.repl.resets == 0
    assert report.cleanup["sent"] == ["03 03", "02"]


def test_timed_out_script_counts_as_running(cdir, monkeypatch):
    with FakeBoard(repl=FakeRepl(run=lambda src: (b"no end", b""))) as fake:
        fake.env(monkeypatch)
        fake.delay_before_reply = {0x04: 0.0}
        with pytest.raises(BoardError), board_session("fpga-4", "t", **FAST) as link:
            link.enter_raw()
            fake.repl.silent = True  # the script "runs" and never answers
            link.exec_raw("hang()", 0.3, changes_state=False)
        fake.repl.silent = False
        assert _after(fake, b"hang()\x04") == b"\x03\x03\x02"


def test_dirty_gets_the_full_sequence(cdir, monkeypatch):
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        with board_session("fpga-4", "t", **FAST) as link:
            link.exec_raw("print(1)", 5, changes_state=True)
        assert _after(fake, b"print(1)\x04") == b"\x03\x03\x02\x04"
        assert fake.repl.resets == 1

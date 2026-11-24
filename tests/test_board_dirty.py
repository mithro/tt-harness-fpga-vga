# SPDX-License-Identifier: Apache-2.0
"""Review fix I3: soft-reset the board only when the session changed its state."""

import threading
import time

import pytest
from board_fakes import FakeBoard

from tthfv.board import BoardTaken, ReplNoPrompt, session, status
from tthfv.board.link import Link
from tthfv.board.session import SessionReport, board_session

FAST = {
    "poll_s": 0.05,
    "restore_timeout_s": 3.0,
    "confirm_after_s": 0.0,
    "confirm_timeout_s": 2.0,
    "attach_timeout_s": 2.0,
}


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(session, "CONFIRM_AFTER_S", 0.0)
    d = tmp_path / "claims"
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(d))
    return d


@pytest.fixture
def fake(cdir, monkeypatch):
    with FakeBoard() as f:
        f.env(monkeypatch)
        yield f


def test_exec_raw_requires_changes_state(fake):
    with Link.open("fpga-4") as link, pytest.raises(TypeError):
        link.exec_raw("print(1)", 5)


def test_read_only_session_leaves_the_raw_repl_without_a_reset(fake, cdir):
    report = SessionReport()
    with board_session("fpga-4", "t", report=report, **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=False)
        assert not link.dirty
    fake.settled_clients()
    assert fake.repl.resets == 0 and fake.repl.raw_resets == 0
    assert fake.repl.mode == "friendly"
    assert bytes(fake.repl.received).endswith(b"print(1)\x04\x02")
    assert b"\x04" not in bytes(fake.repl.received).split(b"print(1)\x04", 1)[1]
    assert report.cleanup["sent"] == ["02"]
    assert report.cleanup["friendly_prompt"] is True
    assert report.outcome == "ok"


def test_state_changing_session_soft_resets(fake, cdir):
    with board_session("fpga-4", "t", **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=True)
        assert link.dirty
    fake.settled_clients()  # let the fake finish handling our frames and close
    assert fake.repl.resets == 1
    assert bytes(fake.repl.received).endswith(b"\x03\x03\x02\x04")


def test_binary_write_makes_the_link_dirty(fake, cdir):
    with board_session("fpga-4", "t", **FAST) as link:
        link.enter_raw()
        link.write(b"\x00\x01")
        assert link.dirty
    fake.settled_clients()  # let the fake finish handling our frames and close
    assert fake.repl.resets == 1


@pytest.mark.parametrize("dirty", [False, True])
def test_board_taken_resets_only_when_dirty(fake, cdir, dirty):
    with pytest.raises(BoardTaken), board_session("fpga-4", "t", **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=dirty)
        fake.visitors = 1
        threading.Timer(0.6, lambda: setattr(fake, "visitors", 0)).start()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3:
            link.exec_raw("print(2)", 5, changes_state=False)
    fake.settled_clients()
    assert fake.repl.resets == (1 if dirty else 0)
    # dirty: the full clean-up (the lesser harm); clean: nothing is sent while the visitor is there
    assert fake.repl.mode == ("friendly" if dirty else "raw")


def test_dirty_takes_priority_over_no_prompt(fake, cdir):
    with pytest.raises(ReplNoPrompt), board_session("fpga-4", "t", **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=True)
        fake.repl.silent = True
        link.in_raw = False
        link.enter_raw(banner_timeout_s=0.2)
    tail = bytes(fake.repl.received).rsplit(b"print(1)\x04", 1)[1]
    assert tail == b"\x03\x03\x01\x02" + b"\x03\x03\x02\x04"  # the failed entry, then the full clean-up


def test_no_prompt_and_clean_sends_nothing_more(cdir, monkeypatch):
    from board_fakes import FakeRepl

    with FakeBoard(repl=FakeRepl(silent=True)) as f:
        f.env(monkeypatch)
        with pytest.raises(ReplNoPrompt), board_session("fpga-4", "t", **FAST) as link:
            link.enter_raw(banner_timeout_s=0.2)
        f.settled_clients()  # let the fake finish handling our frames and close
        assert bytes(f.repl.received) == b"\x03\x03\x01\x02"

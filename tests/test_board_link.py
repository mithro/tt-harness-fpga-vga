# SPDX-License-Identifier: Apache-2.0
import time

import pytest
from board_fakes import FakeBoard, FakeRepl

from tthfv.board import BoardError, BoardScriptError, BoardTaken, ReplNoPrompt
from tthfv.board.link import Link


@pytest.fixture
def fake(monkeypatch):
    with FakeBoard() as f:
        f.env(monkeypatch)
        yield f


def test_open_waits_for_the_board_notice_and_sends_a_generic_user_agent(fake):
    link = Link.open("fpga-4")
    try:
        assert fake.ws_clients == 1
        assert link.notices and link.notices[0]["event"] == "board"
        assert fake.ws_user_agents == ["tthfv"]
        assert link.connect_s >= 0
        assert not link.sent_any
    finally:
        link.close()


def test_open_waits_while_the_daemon_holds_the_board(monkeypatch):
    with FakeBoard(attach_delay=0.5) as f:
        f.env(monkeypatch)
        link = Link.open("fpga-4", attach_timeout_s=5)
        assert link.connect_s >= 0.5
        link.close()


def test_open_gives_up_when_never_attached(monkeypatch):
    with FakeBoard(attach_delay=1.5) as f:
        f.env(monkeypatch)
        with pytest.raises(BoardError, match="not attached"):
            Link.open("fpga-4", attach_timeout_s=0.3)
        assert f.repl.received == b""


def test_friendly_prompt(fake):
    with Link.open("fpga-4") as link:
        seen = link.friendly_prompt()
    assert b">>> " in seen
    assert fake.repl.received == b"\r"


def test_exec_raw_returns_stdout(fake):
    with Link.open("fpga-4") as link:
        link.enter_raw()
        assert link.exec_raw("print('x')", 5) == b"ran\r\n"
    assert fake.repl.scripts == ["print('x')"]


def test_exec_raw_survives_fragmented_output(monkeypatch):
    with FakeBoard(chunk=1) as f:
        f.env(monkeypatch)
        with Link.open("fpga-4") as link:
            link.enter_raw()
            assert link.exec_raw("print('x')", 5) == b"ran\r\n"
            assert link.exec_raw("print('y')", 5) == b"ran\r\n"


def test_exec_raw_enters_the_raw_repl_itself(fake):
    with Link.open("fpga-4") as link:
        assert link.exec_raw("print('x')", 5) == b"ran\r\n"
        assert link.in_raw


def test_exec_raw_raises_with_the_boards_traceback_and_stays_in_step(fake):
    with Link.open("fpga-4") as link:
        link.enter_raw()
        with pytest.raises(BoardScriptError, match="ValueError: boom") as e:
            link.exec_raw("raise ValueError('boom')", 5)
        assert "Traceback" in e.value.traceback
        assert link.exec_raw("print('after')", 5) == b"ran\r\n"


def test_read_exact_carries_binary_including_0x04(monkeypatch):
    payload = b"\x04\x00\xff\x04"

    def run(source):
        return payload + b"tail", b""

    with FakeBoard(repl=FakeRepl(run=run)) as f:
        f.env(monkeypatch)
        with Link.open("fpga-4") as link:
            link.enter_raw()
            link.start_raw("send()", 5)
            assert link.read_exact(4, 5) == payload
            assert link.finish_raw(5) == b"tail"
            link.start_raw("again()", 5)
            assert link.read_exact(4, 5) == payload
            assert link.finish_raw(5) == b"tail"


def test_silent_board_no_banner_leaves_without_escalating(monkeypatch):
    with FakeBoard(repl=FakeRepl(silent=True)) as f:
        f.env(monkeypatch)
        with Link.open("fpga-4") as link:
            t0 = time.monotonic()
            with pytest.raises(ReplNoPrompt):
                link.enter_raw(banner_timeout_s=0.5)
            assert time.monotonic() - t0 < 2
            assert link.no_prompt
        received = bytes(f.repl.received)
    assert received == b"\x03\x03\x01\x02"  # interrupt, raw REPL, leave it: no Ctrl-D, nothing else


def test_banner_found_amid_noise(monkeypatch):
    with FakeBoard(spew=True) as f:
        f.env(monkeypatch)
        with Link.open("fpga-4") as link:
            link.enter_raw()
            assert link.in_raw


def test_cleanup_leaves_raw_and_soft_resets_from_the_friendly_repl(fake):
    with Link.open("fpga-4") as link:
        link.enter_raw()
        link.exec_raw("print('x')", 5)
        report = link.cleanup()
    assert fake.repl.mode == "friendly"
    assert fake.repl.resets == 1
    assert fake.repl.raw_resets == 0
    assert fake.repl.sdk
    assert bytes(fake.repl.received).endswith(b"\x03\x03\x02\x04")
    assert report["soft_reboot"] and report["prompt_after"]
    assert report["sent"] == ["03 03", "02", "04"]


def test_abort_stops_reads_but_not_cleanup(fake):
    with Link.open("fpga-4") as link:
        link.enter_raw()
        link.abort(BoardTaken("visitor"))
        with pytest.raises(BoardTaken):
            link.exec_raw("print('x')", 5)
        report = link.cleanup()
    assert report["soft_reboot"]
    assert fake.repl.resets == 1


def test_read_timeout(monkeypatch):
    def run(source):
        return b"", b""

    with FakeBoard(repl=FakeRepl(run=run)) as f:
        f.env(monkeypatch)
        with Link.open("fpga-4") as link:
            link.enter_raw()
            link.start_raw("x()", 5)
            link.finish_raw(5)
            with pytest.raises(BoardError, match="timed out"):
                link.read_exact(1, 0.3)


def test_transcript_records_both_directions(fake):
    with Link.open("fpga-4") as link:
        link.enter_raw()
        link.exec_raw("print('x')", 5)
    dirs = {e["dir"] for e in link.transcript}
    assert {"tx", "rx", "notice"} <= dirs

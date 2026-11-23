# SPDX-License-Identifier: Apache-2.0
"""Review minor: text output that contains 0x04 must not leave the link out of step."""

import pytest
from board_fakes import FakeBoard, FakeRepl

from tthfv.board import BoardFramingError
from tthfv.board.link import Link


def test_0x04_in_text_output_raises_and_leaves_the_buffer_consistent(monkeypatch):
    outputs = iter([(b"ab\x04cd", b""), (b"fine\r\n", b"")])
    with FakeBoard(repl=FakeRepl(run=lambda src: next(outputs))) as fake:
        fake.env(monkeypatch)
        with Link.open("fpga-4") as link:
            link.enter_raw()
            with pytest.raises(BoardFramingError, match="0x04"):
                link.exec_raw("binary()", 5, changes_state=False)
            assert link.exec_raw("text()", 5, changes_state=False) == b"fine\r\n"

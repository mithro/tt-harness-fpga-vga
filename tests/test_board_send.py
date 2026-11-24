# SPDX-License-Identifier: Apache-2.0
"""Review round 2, item 12: a write to a board that stops reading is bounded by the session deadline."""

import threading
import time

from board_fakes import FakeBoard

from tthfv.board import BoardError, SessionTimeLimit
from tthfv.board.link import Link


def test_write_to_a_stalled_board_is_bounded_by_the_deadline(monkeypatch):
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        link = Link.open("fpga-4")
        try:
            link.enter_raw()
            fake.stall_reads = True
            link.deadline = time.monotonic() + 1.0
            result = {}

            def push():
                try:
                    for _ in range(5000):  # 320 MB: far more than any socket buffer
                        link.write(b"x" * 65536)
                    result["done"] = True
                except BaseException as e:  # noqa: BLE001
                    result["error"] = e

            t0 = time.monotonic()
            t = threading.Thread(target=push, daemon=True)
            t.start()
            t.join(6)
            assert not t.is_alive(), "the write blocked past the deadline"
            assert isinstance(result.get("error"), BoardError)
            assert isinstance(result["error"], SessionTimeLimit) or link.lost
            assert time.monotonic() - t0 < 2.0
        finally:
            fake.stall_reads = False
            link.close()

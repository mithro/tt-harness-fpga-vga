# SPDX-License-Identifier: Apache-2.0
"""Review round 2, item 9: Link.open closes the connection on any exception raised inside it."""

import pytest
from board_fakes import FakeBoard

from tthfv.board import link as link_mod
from tthfv.board.link import Link


@pytest.mark.parametrize("exc", [KeyboardInterrupt, RuntimeError, SystemExit])
def test_open_closes_the_connection_on_any_exception(monkeypatch, exc):
    def boom(self, *args, **kwargs):
        raise exc("injected")

    with FakeBoard() as fake:
        fake.env(monkeypatch)
        monkeypatch.setattr(link_mod.Link, "__init__", boom)
        with pytest.raises(exc):
            Link.open("fpga-4")
        assert fake.seen_connections(1) == 1
        assert fake.settled_clients() == 0

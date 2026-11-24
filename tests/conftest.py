# SPDX-License-Identifier: Apache-2.0
import pytest


@pytest.fixture(autouse=True)
def _board_restore_visitor_lag(monkeypatch):
    """The fake board's status has no lag (unless a test asks for one), but its server needs a moment to handle
    our close; after leaving, count `in use` as a visitor only half a second on, not the real 15 s."""
    from tthfv.board import session

    monkeypatch.setattr(session, "RESTORE_VISITOR_AFTER_S", 0.5)

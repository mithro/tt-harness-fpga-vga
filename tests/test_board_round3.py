# SPDX-License-Identifier: Apache-2.0
"""Review round 3: the signal handler never raises at arbitrary points; the claim is released whatever lands in
the pre-connect wait; a stashed interrupt never cuts the clean-up; and the minor fixes."""

import signal

import pytest
from board_fakes import FakeBoard

from tthfv.board import SessionTerminated, session, status
from tthfv.board import link as link_mod

FAST = {"poll_s": 0.05, "restore_timeout_s": 3.0, "confirm_after_s": 0.0, "confirm_timeout_s": 2.0,
        "attach_timeout_s": 2.0}  # fmt: skip
EXC = {signal.SIGINT: KeyboardInterrupt, signal.SIGTERM: SessionTerminated, signal.SIGHUP: SessionTerminated}


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(session, "CONFIRM_AFTER_S", 0.0)
    monkeypatch.setattr(link_mod, "CLEANUP_TIMEOUT_S", 2.0)
    d = tmp_path / "claims"
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(d))
    return d


@pytest.fixture
def fake(cdir, monkeypatch):
    with FakeBoard() as f:
        f.env(monkeypatch)
        yield f


def _claims(cdir):
    return sorted(p.name for p in cdir.glob("*.json")) if cdir.exists() else []


class _Guard:
    """Keep a signal sent to ourselves from killing the test run if it lands outside the session."""

    def __init__(self, sig):
        self.sig, self.stray = sig, []

    def __enter__(self):
        self.previous = signal.signal(self.sig, lambda s, f: self.stray.append(s))
        return self

    def __exit__(self, *exc):
        signal.signal(self.sig, self.previous)


# -- Critical 1: the handler only records --


def test_handler_never_raises_it_only_records():
    s = session._Signals()
    s.handler(signal.SIGTERM, None)  # no raise, wherever the main thread happens to be
    assert s.pending == signal.SIGTERM
    with pytest.raises(SessionTerminated):
        s.check()
    s.check()  # raised once, not again


def test_handler_hands_the_exception_to_the_link():
    import types

    got = []
    s = session._Signals()
    s.link = types.SimpleNamespace(interrupt=got.append)
    s.handler(signal.SIGINT, None)
    assert len(got) == 1 and isinstance(got[0], KeyboardInterrupt)

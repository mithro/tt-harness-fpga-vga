# SPDX-License-Identifier: Apache-2.0
"""Review round 2, item 8: the claims lock needs no write access and never waits for ever."""

import fcntl
import os
import time

import pytest

from tthfv.board import ClaimHeld, claims

SERIAL = "a2961e5cac65b25f"


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    d = tmp_path / "claims"
    d.mkdir()
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(d))
    return d


def test_a_read_only_lock_file_still_works(cdir):
    lock = cdir / claims.LOCK_NAME
    lock.touch()
    lock.chmod(0o444)  # as if another user had created it
    c = claims.take("fpga-4", SERIAL, "probe", 5)
    claims.release(c)
    assert not c.path.exists()


def test_a_held_lock_is_waited_for_a_bounded_time(cdir, monkeypatch):
    monkeypatch.setattr(claims, "LOCK_WAIT_S", 0.5)
    fd = os.open(cdir / claims.LOCK_NAME, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # another process (another open file) holds it
        t0 = time.monotonic()
        with pytest.raises(ClaimHeld, match="lock"):
            claims.take("fpga-4", SERIAL, "probe", 5)
        assert 0.5 <= time.monotonic() - t0 < 1.5
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    claims.take("fpga-4", SERIAL, "probe", 5)  # free again

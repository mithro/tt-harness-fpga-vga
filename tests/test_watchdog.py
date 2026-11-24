# SPDX-License-Identifier: Apache-2.0
"""The disk watchdog: a slow walk does not make it blind, a walk stops the moment a cap is passed, the free-space
rule runs whatever the walk does, a tree that changes under the walk is tolerated, and a walk that keeps failing is
itself a kill."""

import itertools
import os
import threading
import time

import pytest

from tthfv import proc, safefile


def fast(monkeypatch, **kw):
    """A watchdog that ticks quickly, for tests."""
    values = {"TICK": 0.05, "INTERVAL": 0.1, "SLOW": 0.05, "MAX_INTERVAL": 0.4, "MAX_BLIND": 3, **kw}
    for name, value in values.items():
        monkeypatch.setattr(proc._Watchdog, name, value)


def test_the_interval_between_walks_has_a_ceiling(tmp_path, monkeypatch):
    fast(monkeypatch)
    starts: list[float] = []

    def slow_check() -> str:
        starts.append(time.monotonic())
        time.sleep(
            0.5
        )  # four times this is 2 s: without a ceiling the next walk would start 2 s after it ended
        return ""

    proc.run_logged(["sleep", "4"], 30, tmp_path / "x.log", cwd=tmp_path, scratch_check=slow_check)
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert len(gaps) >= 3
    assert all(0.85 < g < 1.2 for g in gaps), (
        gaps
    )  # the 0.5 s walk plus at most the 0.4 s ceiling (and a tick)


def test_a_walk_stops_when_the_entry_cap_is_passed(tmp_path, monkeypatch):
    for i in range(3000):
        (tmp_path / f"f{i}").write_text("")
    monkeypatch.setattr(safefile, "MAX_ENTRIES", 100)
    seen: list[list[int]] = []
    real = safefile._walk_at

    def spy(parent_fd, name, remove, entries=None, limit=None, incomplete=None):
        if entries is not None:
            seen.append(entries)
        return real(parent_fd, name, remove, entries, limit, incomplete)

    monkeypatch.setattr(safefile, "_walk_at", spy)
    assert safefile.scratch_exceeded(tmp_path) == "scratch limit (100 entries) exceeded during the run"
    assert seen and seen[0][0] <= 101, "the walk went on counting after the cap was passed"


def test_a_flood_of_empty_directories_trips_the_entry_cap(tmp_path, monkeypatch):
    for i in range(400):
        (tmp_path / f"d{i}").mkdir()
    monkeypatch.setattr(safefile, "MAX_ENTRIES", 100)
    assert "entries" in safefile.scratch_exceeded(tmp_path)


def test_the_byte_cap_stops_the_walk_too(tmp_path, monkeypatch):
    (tmp_path / "big").write_bytes(b"x" * (1 << 20))
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 1 << 19)
    assert safefile.scratch_exceeded(tmp_path) == "scratch limit (512 KiB) exceeded during the run"
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 1 << 30)
    assert safefile.scratch_exceeded(tmp_path) == ""


# ---- the free-space rule


def test_the_free_space_rule_fires_while_the_walk_is_still_running(tmp_path, monkeypatch):
    """Data the walk cannot see (an unlinked file) is caught by the fall in free space, even if the walk takes
    for ever."""
    fast(monkeypatch)
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 100 << 20)
    monkeypatch.setattr(proc._Watchdog, "FREE_MARGIN", 50 << 20)
    free = [10 << 30]
    monkeypatch.setattr(proc, "free_bytes", lambda path: free[0])
    gate = threading.Event()

    def stuck_walk() -> str:
        gate.wait(20)  # a walk that never ends
        return ""

    def fall() -> None:
        time.sleep(0.4)
        free[0] -= 200 << 20  # more than cap + margin

    threading.Thread(target=fall, daemon=True).start()
    started = time.monotonic()
    r = proc.run_logged(
        ["sleep", "30"], 60, tmp_path / "x.log", cwd=tmp_path, scratch_check=stuck_walk, scratch_dir=tmp_path
    )
    gate.set()
    assert r.limited == "disk-free" and "fell by 200 MiB" in r.detail and "invisible data" in r.detail
    assert time.monotonic() - started < 10 and not r.timed_out


def test_a_fall_inside_cap_plus_margin_is_not_a_kill(tmp_path, monkeypatch):
    fast(monkeypatch)
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 100 << 20)
    monkeypatch.setattr(proc._Watchdog, "FREE_MARGIN", 100 << 20)
    free = [100 << 30]
    monkeypatch.setattr(proc, "free_bytes", lambda path: free[0])
    free[0] -= 150 << 20
    r = proc.run_logged(
        ["sleep", "1"], 30, tmp_path / "x.log", cwd=tmp_path, scratch_check=lambda: "", scratch_dir=tmp_path
    )
    assert r.limited is None and r.rc == 0


def test_free_space_below_half_the_minimum_is_a_kill(tmp_path, monkeypatch):
    fast(monkeypatch)
    monkeypatch.setenv("TTHFV_MIN_FREE", "8G")
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 100 << 30)  # so that the fall alone is not what fires
    free = [10 << 30]
    monkeypatch.setattr(proc, "free_bytes", lambda path: free[0])
    threading.Timer(0.4, lambda: free.__setitem__(0, 3 << 30)).start()  # 3 GiB < 8 GiB / 2
    r = proc.run_logged(
        ["sleep", "30"], 60, tmp_path / "x.log", cwd=tmp_path, scratch_check=lambda: "", scratch_dir=tmp_path
    )
    assert r.limited == "disk-free" and "below the floor of 4096 MiB (TTHFV_MIN_FREE / 2)" in r.detail


# ---- a tree that changes, and a walk that cannot finish


def test_a_directory_that_vanishes_mid_walk_is_skipped_not_fatal(tmp_path, monkeypatch):
    for i in range(5):
        (tmp_path / f"d{i}").mkdir()
        (tmp_path / f"d{i}" / "f").write_bytes(b"x" * 8192)
    real = safefile._open_subdir

    def flaky(parent_fd, name):
        if name == "d2":
            raise FileNotFoundError(name)
        return real(parent_fd, name)

    monkeypatch.setattr(safefile, "_open_subdir", flaky)
    incomplete = [False]
    fd = os.open(tmp_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        total = safefile._walk_at(fd, tmp_path.name, False, [0], None, incomplete)
    finally:
        os.close(fd)
    assert incomplete[0] is True and total >= 4 * 8192  # the other four directories were counted
    with pytest.raises(safefile.WalkIncomplete):
        safefile.scratch_exceeded(tmp_path)


def test_going_back_up_after_the_tree_was_replaced_reopens_by_name(tmp_path, monkeypatch):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "f").write_bytes(b"x" * 8192)
    (tmp_path / "a" / "g").write_bytes(b"x" * 8192)
    (tmp_path / "z").mkdir()
    (tmp_path / "z" / "f").write_bytes(b"x" * 8192)
    real_open = os.open
    state = {"fired": False}

    def failing_dotdot(path, flags, *args, **kw):
        if path == ".." and not state["fired"]:
            state["fired"] = True
            raise FileNotFoundError("..")  # the way back up is gone
        return real_open(path, flags, *args, **kw)

    monkeypatch.setattr(safefile.os, "open", failing_dotdot)
    incomplete = [False]
    fd = real_open(tmp_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        total = safefile._walk_at(fd, tmp_path.name, False, [0], None, incomplete)
    finally:
        os.close(fd)
    assert state["fired"] and incomplete[0] is True
    assert total >= 3 * 8192  # it went on after reopening: z/f, a/g and b/f are all counted


def test_three_walks_in_a_row_that_could_not_finish_are_a_kill(tmp_path, monkeypatch):
    fast(monkeypatch)
    calls = []

    def always_changing() -> str:
        calls.append(1)
        raise safefile.WalkIncomplete("the tree changed")

    r = proc.run_logged(["sleep", "30"], 60, tmp_path / "x.log", cwd=tmp_path, scratch_check=always_changing)
    assert r.limited == "disk" and r.detail == "scratch could not be measured (the tree kept changing)"
    assert len(calls) == 3  # not on the first, not on the second


def test_a_complete_walk_resets_the_count(tmp_path, monkeypatch):
    fast(monkeypatch)
    results = iter([OSError("x"), OSError("x"), "", OSError("x"), OSError("x"), ""] + [""] * 50)

    def flaky() -> str:
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    r = proc.run_logged(["sleep", "3"], 60, tmp_path / "x.log", cwd=tmp_path, scratch_check=flaky)
    assert r.limited is None and r.rc == 0

# SPDX-License-Identifier: Apache-2.0
"""The disk watchdog: a slow walk does not make it blind, and a walk stops the moment a cap is passed."""

import itertools
import time

from tthfv import proc, safefile


def test_the_interval_between_checks_has_a_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(proc._Watchdog, "INTERVAL", 0.1)
    monkeypatch.setattr(proc._Watchdog, "SLOW", 0.05)
    monkeypatch.setattr(proc._Watchdog, "MAX_INTERVAL", 0.4)
    starts: list[float] = []

    def slow_check() -> str:
        starts.append(time.monotonic())
        time.sleep(0.5)  # four times this is 2 s: without a ceiling the next check would be 2 s later
        return ""

    proc.run_logged(["sleep", "4"], 30, tmp_path / "x.log", cwd=tmp_path, scratch_check=slow_check)
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert len(gaps) >= 3
    assert all(0.85 < g < 1.2 for g in gaps), gaps  # the 0.5 s walk plus at most the 0.4 s ceiling


def test_a_walk_stops_when_the_entry_cap_is_passed(tmp_path, monkeypatch):
    for i in range(3000):
        (tmp_path / f"f{i}").write_text("")
    monkeypatch.setattr(safefile, "MAX_ENTRIES", 100)
    seen: list[list[int]] = []
    real = safefile._walk_at

    def spy(parent_fd, name, remove, entries=None, limit=None):
        if entries is not None:
            seen.append(entries)
        return real(parent_fd, name, remove, entries, limit)

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

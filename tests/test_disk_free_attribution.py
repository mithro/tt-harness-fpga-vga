# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from another test module
"""The free-space rule with the real jail and scope: a large fall in free space only stops a tool whose own writes
(counted from /proc/<pid>/io and the open unlinked files of the processes in its scope) account for more than the
scratch cap plus the margin. What other processes write is noted, not punished; the floor stops anyone. Small caps
(8 MiB + 8 MiB), so that at most a few tens of MiB are written."""

import os
import threading
import time

from test_limits import box, jail, needs_limits, no_units  # noqa: F401

from tthfv import proc, safefile

MiB = 1 << 20
GIB = 1 << 30


def small_caps(monkeypatch):
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 8 * MiB)
    monkeypatch.setattr(proc._Watchdog, "FREE_MARGIN", 8 * MiB)


def run_jailed(jail, script, timeout=60):
    _ro, rw = jail
    return proc.run_logged(
        ["sh", "-c", script], timeout, rw / "w.log", cwd=rw, sandbox=box(jail),
        scratch_check=lambda: safefile.scratch_exceeded(rw), scratch_dir=rw,
    )  # fmt: skip


def falling_free(monkeypatch, after: float, by: int, start: int = 100 * GIB):
    """Fake the free space of the work filesystem: `start`, and `by` less from `after` seconds on (another process
    filling the disk)."""
    t0 = time.monotonic()
    monkeypatch.setattr(proc, "free_bytes", lambda path: start - (by if time.monotonic() - t0 > after else 0))


@needs_limits
def test_another_process_writing_a_large_file_does_not_stop_a_tool_that_writes_nothing(
    jail, monkeypatch, tmp_path
):
    """(a) The test itself writes 64 MiB next to the jail's directory (the same filesystem), keeps it 3 s and deletes
    it, while the jailed tool sleeps: free space falls by far more than cap + margin, but not because of the tool."""
    small_caps(monkeypatch)
    other = tmp_path / "other-process-data"

    def write_and_delete() -> None:
        time.sleep(1.0)
        with open(other, "wb") as f:
            f.writelines(b"\xa5" * MiB for _ in range(64))
            f.flush()
            os.fsync(f.fileno())
        time.sleep(3.0)
        other.unlink()

    writer = threading.Thread(target=write_and_delete, daemon=True)
    writer.start()
    r = run_jailed(jail, "sleep 6")
    writer.join(10)
    assert r.rc == 0 and r.limited is None and not r.timed_out, r
    assert "other processes" in r.free_space_note and "not stopped" in r.free_space_note, r.free_space_note
    log = (jail[1] / "w.log").read_text()
    assert log.count("[tthfv] free space on the work filesystem fell by") == 1, log  # once, not every tick
    assert no_units()


@needs_limits
def test_a_large_fall_caused_elsewhere_is_noted_and_does_not_stop_the_tool(jail, monkeypatch):
    """(a, deterministic) The same with the free space faked: it falls by 5 GiB a second into the run."""
    small_caps(monkeypatch)
    falling_free(monkeypatch, after=1.0, by=5 * GIB)
    r = run_jailed(jail, "sleep 4")
    assert r.rc == 0 and r.limited is None, r
    assert r.free_space_note.startswith("free space on the work filesystem fell by 5120 MiB"), (
        r.free_space_note
    )
    assert "the tool's own writes account for only 0 MiB" in r.free_space_note, r.free_space_note


@needs_limits
def test_a_tool_writing_past_the_cap_into_scratch_is_stopped_as_before(jail, monkeypatch):
    """(b) Visible files: the scratch walk stops it, limited "disk". 12 MiB: past the cap (8 MiB), not past cap +
    margin, which is how the real caps (2 GiB + 1 GiB) leave the walk to fire first."""
    small_caps(monkeypatch)
    rw = jail[1]
    r = run_jailed(jail, f"head -c {12 * MiB} /dev/zero > {rw}/big; sleep 30")
    assert r.limited == "disk" and not r.timed_out, r
    assert "scratch limit (8 MiB) exceeded during the run" in r.detail, r.detail
    assert no_units()


@needs_limits
def test_a_tool_holding_a_large_unlinked_file_open_is_stopped(jail, monkeypatch):
    """(c) The walk cannot see a file unlinked while open; the fall in free space and the tool's own accounted writes
    (its write_bytes and the open unlinked file) can."""
    small_caps(monkeypatch)
    rw = jail[1]
    script = f"exec 3> {rw}/hidden; rm {rw}/hidden; head -c {32 * MiB} /dev/zero >&3; sync; sleep 30"
    started = time.monotonic()
    r = run_jailed(jail, script)
    assert safefile.scratch_exceeded(rw) == ""
    assert r.limited == "disk-free" and not r.timed_out, r
    assert "fell by" in r.detail and "invisible data" in r.detail, r.detail
    assert "the tool's own writes account for" in r.detail and "/proc/<pid>/io" in r.detail, r.detail
    assert time.monotonic() - started < 20 and no_units()


@needs_limits
def test_the_floor_stops_a_tool_whoever_fills_the_disk(jail, monkeypatch):
    """(d) Below TTHFV_MIN_FREE / 2 the tool is stopped even though it wrote nothing."""
    small_caps(monkeypatch)
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 100 * GIB)  # so that only the floor can fire
    monkeypatch.setenv("TTHFV_MIN_FREE", "8G")
    falling_free(monkeypatch, after=1.0, by=7 * GIB, start=10 * GIB)  # 3 GiB < 8 GiB / 2
    r = run_jailed(jail, "sleep 30")
    assert r.limited == "disk-free" and not r.timed_out, r
    assert "below the floor of 4096 MiB (TTHFV_MIN_FREE / 2)" in r.detail, r.detail
    assert no_units()


@needs_limits
def test_a_tool_whose_writes_cannot_be_read_is_stopped_on_a_large_fall(jail, monkeypatch):
    """A process may make itself non-dumpable, and then its /proc/<pid>/io cannot be read: the fall cannot be
    attributed, so the rule does what it did before attribution existed and stops the tool."""
    small_caps(monkeypatch)
    falling_free(monkeypatch, after=1.0, by=5 * GIB)
    hide = "import ctypes, time; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); time.sleep(30)"  # PR_SET_DUMPABLE 0
    r = run_jailed(jail, f'python3 -c "{hide}"')
    assert r.limited == "disk-free" and not r.timed_out, r
    assert "could not be measured" in r.detail and "cannot be read" in r.detail, r.detail
    assert no_units()

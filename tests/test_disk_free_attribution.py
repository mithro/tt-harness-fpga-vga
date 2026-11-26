# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from another test module
"""The free-space rule with the real jail and scope: a large fall in free space only stops a tool whose own writes
(counted from /proc/<pid>/io and the open unlinked files of the processes in its scope) account for more than the
scratch cap plus the margin, or which leaves more than UNEXPLAINED_FACTOR times that unexplained. What other
processes write is noted, not punished; the floor stops anyone. Small caps (8 MiB + 8 MiB: trigger 16 MiB, bound
64 MiB), so that at most a few hundred MiB are written.

The work filesystem is shared with other jobs, whose writes move its free space by anything from under 1 MiB/s to
over 50 MiB/s. So the tests of the rule's logic take the fall in free space from the proc.free_bytes seam, driven by
marker files the jailed tool makes once it has done what the test is about (`fall_at_marker`), while everything the
tool does stays real: the jail, the scope, its /proc counters, its descriptors in flight and its auto-reaped
children. Two end-to-end tests read the real filesystem; each first measures how much the free space drifts by
itself and skips, saying so, when the drift could decide the result (`quiet_or_skip`)."""

import os
import sys
import threading
import time
from pathlib import Path

import pytest
from test_limits import box, jail, needs_limits, no_units  # noqa: F401

from tthfv import proc, safefile

MiB = 1 << 20
GIB = 1 << 30
BASE = 100 * GIB


def small_caps(monkeypatch):
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 8 * MiB)
    monkeypatch.setattr(proc._Watchdog, "FREE_MARGIN", 8 * MiB)


def run_jailed(jail, script, timeout=60):
    _ro, rw = jail
    return proc.run_logged(
        ["sh", "-c", script], timeout, rw / "w.log", cwd=rw, sandbox=box(jail),
        scratch_check=lambda: safefile.scratch_exceeded(rw), scratch_dir=rw,
    )  # fmt: skip


def fall_at_marker(monkeypatch, marker: Path, by: int, start: int = BASE) -> None:
    """Fake the free space of the work filesystem: `start` until `marker` exists, then `by` less. The marker is made
    by the jailed tool once it has done what the fall stands for, so the sequence does not depend on timing or on
    what other jobs write."""
    monkeypatch.setattr(proc, "free_bytes", lambda path: start - (by if marker.exists() else 0))


WRITE_PROBE = 4 * MiB


def _own_write_bytes() -> int:
    for line in Path("/proc/self/io").read_text().splitlines():
        name, _, value = line.partition(":")
        if name == "write_bytes":
            return int(value)
    raise AssertionError("no write_bytes in /proc/self/io")


def require_counted_writes(directory: Path) -> None:
    """Skip unless writes to directory's filesystem show in /proc/<pid>/io write_bytes, which the tests that rely on
    the tool's own measured writes need. Decided by a real probe, not by the filesystem's name: this process writes
    WRITE_PROBE bytes there, fsyncs, deletes the file, and looks at its own write_bytes. tmpfs (where pytest's default
    basetemp lives on many hosts) does not count them."""
    before = _own_write_bytes()
    probe = directory / ".write-probe"
    with open(probe, "wb") as f:
        f.write(b"\xa5" * WRITE_PROBE)
        f.flush()
        os.fsync(f.fileno())
    probe.unlink()
    moved = _own_write_bytes() - before
    if moved < WRITE_PROBE // 2:
        pytest.skip(
            f"{directory} is on a filesystem that does not count writes in /proc/<pid>/io write_bytes (a "
            f"{WRITE_PROBE >> 20} MiB write moved this process's write_bytes by {moved} bytes; tmpfs does this): "
            "run pytest with --basetemp on a disk filesystem"
        )


def test_the_write_probe_skips_where_writes_are_not_counted(tmp_path, monkeypatch):
    """require_counted_writes on a filesystem where write_bytes does not move (as on tmpfs; simulated here by a
    write_bytes that stays put) skips, saying why and what to do."""
    monkeypatch.setattr(sys.modules[__name__], "_own_write_bytes", lambda: 12345)
    with pytest.raises(
        pytest.skip.Exception, match=r"does not count writes .* --basetemp on a disk filesystem"
    ):
        require_counted_writes(tmp_path)
    assert not (tmp_path / ".write-probe").exists()


def steady_free(monkeypatch, start: int = BASE) -> None:
    """Fake a filesystem whose free space does not move: only the scratch walk can stop a tool."""
    monkeypatch.setattr(proc, "free_bytes", lambda path: start)


def drift_mib_per_s(path: Path, seconds: float = 1.0) -> float:
    """How fast the real free space of path's filesystem moves by itself, in either direction (MiB/s)."""
    before = proc.free_bytes(path)
    time.sleep(seconds)
    after = proc.free_bytes(path)
    if before is None or after is None:
        pytest.skip("the free space of the work filesystem cannot be read")
    return abs(after - before) / MiB / seconds


def quiet_or_skip(path: Path, headroom_mib: float, lifetime_s: float) -> float:
    """Skip, with the measured rate, unless the background drift over the test's lifetime stays below a quarter of
    its headroom (how far the fall can move either way before the result changes). Returns the rate."""
    rate = drift_mib_per_s(path)
    allowed = headroom_mib / 4 / lifetime_s
    if rate > allowed:
        pytest.skip(
            f"the work filesystem's free space drifts by {rate:.1f} MiB/s by itself (other jobs), more than the "
            f"{allowed:.1f} MiB/s this real-filesystem test tolerates ({headroom_mib:.0f} MiB of headroom over "
            f"{lifetime_s:.0f} s); the seam-driven tests cover the same logic"
        )
    return rate


# ---- what other processes write, and what the tool itself writes


@needs_limits
def test_a_fall_caused_elsewhere_within_the_bound_is_noted_and_does_not_stop_the_tool(jail, monkeypatch):
    """The free space falls by 40 MiB (past the 16 MiB trigger, within the 64 MiB bound) while the tool writes
    nothing: noted once in the log and the result, not stopped."""
    small_caps(monkeypatch)
    rw = jail[1]
    fall_at_marker(monkeypatch, rw / "started", 40 * MiB)
    r = run_jailed(jail, f"touch {rw}/started; sleep 3")
    assert r.rc == 0 and r.limited is None and not r.timed_out, r
    assert r.free_space_note.startswith("free space on the work filesystem fell by 40 MiB"), r.free_space_note
    assert "the tool's own writes account for only 0 MiB" in r.free_space_note, r.free_space_note
    assert "other processes" in r.free_space_note and "not stopped" in r.free_space_note, r.free_space_note
    log = (rw / "w.log").read_text()
    assert log.count("[tthfv] free space on the work filesystem fell by") == 1, log  # once, not every tick
    assert no_units()


@needs_limits
def test_a_fall_past_the_bound_stops_a_tool_that_wrote_nothing(jail, monkeypatch):
    """The free space falls by 5 GiB; the tool's writes explain none of it: past the bound, stopped."""
    small_caps(monkeypatch)
    rw = jail[1]
    fall_at_marker(monkeypatch, rw / "started", 5 * GIB)
    r = run_jailed(jail, f"touch {rw}/started; sleep 30")
    assert r.limited == "disk-free" and not r.timed_out, r
    assert r.detail.startswith("free space on the work filesystem fell by 5120 MiB"), r.detail
    assert "do not explain" in r.detail and "more than 4 x" in r.detail, r.detail
    assert no_units()


@needs_limits
def test_a_tool_writing_past_the_cap_into_scratch_is_stopped_as_before(jail, monkeypatch):
    """(b) Visible files: the scratch walk stops it, limited "disk". The free space is held steady, so that only the
    walk can fire (with the real filesystem, other jobs' writes could make the free-space rule fire first)."""
    small_caps(monkeypatch)
    steady_free(monkeypatch)
    rw = jail[1]
    r = run_jailed(jail, f"head -c {12 * MiB} /dev/zero > {rw}/big; sleep 30")
    assert r.limited == "disk" and not r.timed_out, r
    assert "scratch limit (8 MiB) exceeded during the run" in r.detail, r.detail
    assert no_units()


@needs_limits
def test_a_tool_holding_a_large_unlinked_file_open_is_stopped(jail, monkeypatch):
    """(c) The walk cannot see a file unlinked while open; the tool's own measured writes (its write_bytes and the
    open unlinked file) can, and pass cap + margin once the free space has fallen by the file's 32 MiB. No sync:
    write_bytes counts the data when it is dirtied, and a sync waits for every other job's dirty data too. Both
    measures must show the 32 MiB: on a filesystem that does not count write_bytes (tmpfs) the stop would come from
    the open unlinked file alone, so the test needs counted writes."""
    small_caps(monkeypatch)
    rw = jail[1]
    require_counted_writes(rw)
    fall_at_marker(monkeypatch, rw / "written", 32 * MiB)
    script = (
        f"exec 3> {rw}/hidden; rm {rw}/hidden; head -c {32 * MiB} /dev/zero >&3; touch {rw}/written; sleep 30"
    )
    r = run_jailed(jail, script)
    assert safefile.scratch_exceeded(rw) == ""
    assert r.limited == "disk-free" and not r.timed_out, r  # stopped by the rule, not by the 60 s timeout
    assert "fell by" in r.detail and "invisible data" in r.detail, r.detail
    assert "the tool's own writes account for" in r.detail and "/proc/<pid>/io" in r.detail, r.detail
    assert "(32 MiB written" in r.detail and "and 32 MiB in files they hold open" in r.detail, r.detail
    assert no_units()


@needs_limits
def test_the_floor_stops_a_tool_whoever_fills_the_disk(jail, monkeypatch):
    """(d) Below TTHFV_MIN_FREE / 2 the tool is stopped even though it wrote nothing."""
    small_caps(monkeypatch)
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 100 * GIB)  # so that only the floor can fire
    monkeypatch.setenv("TTHFV_MIN_FREE", "8G")
    rw = jail[1]
    fall_at_marker(monkeypatch, rw / "started", 7 * GIB, start=10 * GIB)  # 3 GiB < 8 GiB / 2
    r = run_jailed(jail, f"touch {rw}/started; sleep 30")
    assert r.limited == "disk-free" and not r.timed_out, r
    assert "below the floor of 4096 MiB (TTHFV_MIN_FREE / 2)" in r.detail, r.detail
    assert no_units()


@needs_limits
def test_a_tool_whose_writes_cannot_be_read_is_stopped_on_a_large_fall(jail, monkeypatch):
    """A process may make itself non-dumpable, and then its /proc/<pid>/io cannot be read: the fall cannot be
    attributed, so the rule does what it did before attribution existed and stops the tool. The fall comes only once
    the process is non-dumpable (its marker), so it is the unmeasurable branch that fires, not the bound."""
    small_caps(monkeypatch)
    rw = jail[1]
    fall_at_marker(monkeypatch, rw / "hidden", 40 * MiB)
    hide = (
        "import ctypes, time; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); "  # PR_SET_DUMPABLE 0
        f"open('{rw}/hidden', 'w').close(); time.sleep(30)"
    )
    r = run_jailed(jail, f'python3 -c "{hide}"')
    assert r.limited == "disk-free" and not r.timed_out, r
    assert "could not be measured" in r.detail and "cannot be read" in r.detail, r.detail
    assert no_units()


# ---- disk the tool holds where /proc cannot see it, and the bound on what may stay unexplained

# The reviewer's probes: a file whose descriptor is in flight in an AF_UNIX socketpair (SCM_RIGHTS) is in neither
# /proc/<pid>/fd nor maps; fallocate never shows in write_bytes; a child reaped automatically (SIGCHLD ignored) never
# adds its counters to its parent's. Each makes the marker `hidden` once all of it is hidden.
FALLOCATE_IN_FLIGHT = r"""
import os, socket, sys, time
d, mib, hold = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
p = os.path.join(d, "f")
fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
os.posix_fallocate(fd, 0, mib << 20)
socket.send_fds(a, [b"x"], [fd])
os.close(fd)
os.unlink(p)
open(os.path.join(d, "hidden"), "w").close()
time.sleep(hold)
"""

AUTO_REAPED_WRITERS = r"""
import os, signal, socket, sys, time
d, mib, hold = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
for i in range(mib // 4):
    if os.fork() == 0:
        p = os.path.join(d, f"f{i}")
        fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
        for _ in range(4):
            os.write(fd, b"\xa5" * (1 << 20))
        socket.send_fds(a, [b"x"], [fd])
        os.close(fd)
        os.unlink(p)
        os._exit(0)
    time.sleep(0.02)
while any(n.startswith("f") and n[1:].isdigit() for n in os.listdir(d)):  # every child has unlinked its file
    time.sleep(0.02)
time.sleep(0.5)  # and has exited: auto-reaped, its counters are gone
open(os.path.join(d, "hidden"), "w").close()
time.sleep(hold)
"""

HIDERS = {"fallocate-in-flight": FALLOCATE_IN_FLIGHT, "auto-reaped-writers": AUTO_REAPED_WRITERS}


def run_python(jail, code: str, mib: int, hold: float):
    """Run a hider in the jail: it hides `mib` MiB, makes the marker `hidden`, and then lives `hold` seconds."""
    _ro, rw = jail
    (rw / "hide.py").write_text(code)
    return proc.run_logged(
        ["python3", str(rw / "hide.py"), str(rw), str(mib), str(hold)], 60, rw / "w.log", cwd=rw, sandbox=box(jail),
        scratch_check=lambda: safefile.scratch_exceeded(rw), scratch_dir=rw,
    )  # fmt: skip


@needs_limits
@pytest.mark.parametrize("how", sorted(HIDERS))
def test_disk_hidden_from_proc_beyond_the_bound_stops_the_tool(jail, monkeypatch, how):
    """128 MiB the attribution cannot see, and a fall of 128 MiB: the unexplained part passes 4 x (cap + margin)
    = 64 MiB, so the tool is stopped as the rule did before attribution."""
    small_caps(monkeypatch)
    fall_at_marker(monkeypatch, jail[1] / "hidden", 128 * MiB)
    r = run_python(jail, HIDERS[how], 128, hold=8)
    assert r.limited == "disk-free" and not r.timed_out, r
    assert "do not explain" in r.detail and "4 x" in r.detail, r.detail
    assert no_units()


@needs_limits
@pytest.mark.parametrize("how", sorted(HIDERS))
def test_disk_hidden_from_proc_within_the_bound_is_noted_not_stopped(jail, monkeypatch, how):
    """24 MiB hidden the same way, and a fall of 24 MiB: past cap + margin (16 MiB), within the bound (64 MiB); the
    residual the rule accepts."""
    small_caps(monkeypatch)
    fall_at_marker(monkeypatch, jail[1] / "hidden", 24 * MiB)
    r = run_python(jail, HIDERS[how], 24, hold=3)
    assert r.rc == 0 and r.limited is None, r
    assert "not stopped" in r.free_space_note and "fell by 24 MiB" in r.free_space_note, r.free_space_note
    assert no_units()


def test_the_clamp_keeps_one_process_from_offsetting_anothers_writes(tmp_path, monkeypatch):
    """The clamp, exactly: own_writes reads two processes' counters through its real parsing path (cgroup.procs,
    /proc/<pid>/io, fd, maps, with PROC_ROOT pointing at files the test writes). The writer has write_bytes 20 MiB;
    the other has cancelled_write_bytes 20 MiB (it truncated the writer's dirty data) and wrote nothing. Clamped per
    process that is 20 + 0 MiB; summed without the clamp it would be 0. A real kernel cannot be made to cancel before
    writeback every time, so this is where the clamp itself is tested."""
    fake = tmp_path / "proc"
    for pid, written, cancelled in (("101", 20 * MiB, 0), ("102", 0, 20 * MiB)):
        (fake / pid / "fd").mkdir(parents=True)
        (fake / pid / "maps").write_text("")
        (fake / pid / "io").write_text(
            f"rchar: 0\nwchar: {written}\nsyscr: 0\nsyscw: 0\nread_bytes: 0\n"
            f"write_bytes: {written}\ncancelled_write_bytes: {cancelled}\n"
        )
    cgroup = tmp_path / "tthfv-1-1.scope"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("101\n102\n")
    monkeypatch.setattr(proc, "PROC_ROOT", fake)
    assert proc.own_writes(cgroup, None) == proc.OwnWrites(written=20 * MiB, unlinked=0)


# A writer and a truncater in one jail, with every step ordered by a handshake, not by time: the writer writes 20 MiB
# to a file it unlinked first (so the scratch walk never sees it), then sends the descriptor over a socketpair; the
# truncater receives it, truncates the file, closes it, and only then makes the marker that lets the free space fall.
TRUNCATE_ANOTHERS_FILE = r"""
import os, socket, sys, time
d = sys.argv[1]
a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
if os.fork() == 0:  # the writer
    p = os.path.join(d, "shared")
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    os.unlink(p)
    for _ in range(20):
        os.write(fd, b"\xa5" * (1 << 20))
    socket.send_fds(a, [b"x"], [fd])
    time.sleep(30)
    os._exit(0)
if os.fork() == 0:  # the truncater
    _msg, fds, _flags, _addr = socket.recv_fds(b, 1, 1)
    os.ftruncate(fds[0], 0)  # the writer's data: cancelled if still dirty, else already counted as written
    os.close(fds[0])
    open(os.path.join(d, "truncated"), "w").close()
    time.sleep(30)
    os._exit(0)
os.wait()
"""


@needs_limits
def test_a_writer_whose_data_another_process_truncated_is_still_counted(jail, monkeypatch):
    """The real-jail counterpart of the clamp test, asserting only what holds whatever the writeback order: the
    writer's 20 MiB count, whether the truncater cancelled them (its cancelled_write_bytes 20 MiB, clamped to 0) or
    writeback got there first (nothing cancelled). They pass cap + margin (16 MiB) on a fall of 40 MiB, which is within
    the bound, so only the tool's own writes can stop it. The marker comes after the truncate, and the file is
    unlinked before it is written, so the scratch walk cannot fire instead. On a filesystem that does not count
    write_bytes (tmpfs) the writer's 20 MiB read as 0, so the test needs counted writes."""
    small_caps(monkeypatch)
    rw = jail[1]
    require_counted_writes(rw)
    (rw / "t.py").write_text(TRUNCATE_ANOTHERS_FILE)
    fall_at_marker(monkeypatch, rw / "truncated", 40 * MiB)
    r = run_jailed(jail, f"python3 {rw}/t.py {rw}")
    assert r.limited == "disk-free" and not r.timed_out, r
    assert "the tool's own writes account for 20 MiB" in r.detail, r.detail
    assert no_units()


# ---- end to end, with the real free space of the work filesystem


@needs_limits
def test_real_filesystem_another_process_writing_does_not_stop_a_tool_that_writes_nothing(
    jail, monkeypatch, tmp_path
):
    """The real statvfs path: the test itself writes 40 MiB next to the jail's directory (the same filesystem), keeps
    it 2 s and deletes it, while the jailed tool sleeps. The fall passes cap + margin (16 MiB) and stays within the
    bound (64 MiB): 24 MiB of headroom either way, so the drift guard uses 24 MiB over the tool's ~6 s. The
    write starts only once the jailed tool has started (its marker): on a loaded host the jail and scope can take
    more than a second to start, and the watchdog's baseline must be taken before the write, not after it."""
    small_caps(monkeypatch)
    rw = jail[1]
    quiet_or_skip(rw, headroom_mib=24, lifetime_s=6)
    other = tmp_path / "other-process-data"
    started = rw / "tool-started"

    def write_and_delete() -> None:
        end = time.monotonic() + 30
        while not started.exists() and time.monotonic() < end:
            time.sleep(0.05)
        time.sleep(0.5)
        with open(other, "wb") as f:
            f.writelines(b"\xa5" * MiB for _ in range(40))
            f.flush()
            os.fsync(f.fileno())
        time.sleep(2.0)
        other.unlink()

    writer = threading.Thread(target=write_and_delete, daemon=True)
    writer.start()
    r = run_jailed(jail, f"touch {started}; sleep 4")
    assert started.exists(), "the jailed tool did not start"
    writer.join(10)
    assert r.rc == 0 and r.limited is None and not r.timed_out, r
    assert "other processes" in r.free_space_note and "not stopped" in r.free_space_note, r.free_space_note
    log = (rw / "w.log").read_text()
    assert log.count("[tthfv] free space on the work filesystem fell by") == 1, log  # once, not every tick
    assert no_units()


@needs_limits
def test_real_filesystem_disk_hidden_past_the_bound_stops_the_tool(jail, monkeypatch):
    """The real statvfs path: the jailed tool fallocates 128 MiB and keeps it only through a descriptor in flight.
    The real fall (128 MiB) passes the bound (64 MiB) by 64 MiB; only other jobs freeing that much could hide it, so
    the drift guard uses 64 MiB over the tool's ~10 s (writes by other jobs only add to the fall)."""
    small_caps(monkeypatch)
    rw = jail[1]
    quiet_or_skip(rw, headroom_mib=64, lifetime_s=10)
    r = run_python(jail, FALLOCATE_IN_FLIGHT, 128, hold=8)
    assert r.limited == "disk-free" and not r.timed_out, r
    assert "do not explain" in r.detail, r.detail
    assert no_units()

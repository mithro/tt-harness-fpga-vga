# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from another test module
"""The free-space rule with the real jail and scope: a large fall in free space only stops a tool whose own writes
(counted from /proc/<pid>/io and the open unlinked files of the processes in its scope) account for more than the
scratch cap plus the margin. What other processes write is noted, not punished; the floor stops anyone. Small caps
(8 MiB + 8 MiB), so that at most a few tens of MiB are written."""

import os
import threading
import time

import pytest
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
    """(a) The test itself writes 24 MiB next to the jail's directory (the same filesystem), keeps it 2 s and deletes
    it, while the jailed tool sleeps: free space falls by more than cap + margin (16 MiB), but not because of the tool,
    and by less than the bound on what the tool's writes may leave unexplained (4 x 16 MiB), with room for what other
    jobs on the shared filesystem write in the tool's 4 s. The write starts only once the jailed tool has started (it
    makes a marker file): on a loaded host the jail and scope can take more than a second to start, and the watchdog's
    baseline must be taken before the write, not after it."""
    small_caps(monkeypatch)
    other = tmp_path / "other-process-data"
    started = jail[1] / "tool-started"

    def write_and_delete() -> None:
        end = time.monotonic() + 30
        while not started.exists() and time.monotonic() < end:
            time.sleep(0.05)
        time.sleep(0.5)
        with open(other, "wb") as f:
            f.writelines(b"\xa5" * MiB for _ in range(24))
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
    log = (jail[1] / "w.log").read_text()
    assert log.count("[tthfv] free space on the work filesystem fell by") == 1, log  # once, not every tick
    assert no_units()


@needs_limits
def test_a_large_fall_caused_elsewhere_is_noted_and_does_not_stop_the_tool(jail, monkeypatch):
    """(a, deterministic) The same with the free space faked: it falls by 40 MiB a second into the run."""
    small_caps(monkeypatch)
    falling_free(monkeypatch, after=1.0, by=40 * MiB)
    r = run_jailed(jail, "sleep 4")
    assert r.rc == 0 and r.limited is None, r
    assert r.free_space_note.startswith("free space on the work filesystem fell by 40 MiB"), r.free_space_note
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


# ---- disk the tool holds where /proc cannot see it, and the bound on what may stay unexplained

# The reviewer's probes: a file whose descriptor is in flight in an AF_UNIX socketpair (SCM_RIGHTS) is in neither
# /proc/<pid>/fd nor maps; fallocate never shows in write_bytes; a child reaped automatically (SIGCHLD ignored) never
# adds its counters to its parent's. With caps of 8 + 8 MiB the bound is 4 x 16 = 64 MiB.
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
time.sleep(hold)
"""

HIDERS = {"fallocate-in-flight": FALLOCATE_IN_FLIGHT, "auto-reaped-writers": AUTO_REAPED_WRITERS}


def run_python(jail, code: str, mib: int, hold: float):
    """Run a hider in the jail: it hides `mib` MiB and then lives `hold` seconds."""
    _ro, rw = jail
    (rw / "hide.py").write_text(code)
    return proc.run_logged(
        ["python3", str(rw / "hide.py"), str(rw), str(mib), str(hold)], 60, rw / "w.log", cwd=rw, sandbox=box(jail),
        scratch_check=lambda: safefile.scratch_exceeded(rw), scratch_dir=rw,
    )  # fmt: skip


@needs_limits
@pytest.mark.parametrize("how", sorted(HIDERS))
def test_disk_hidden_from_proc_beyond_the_bound_stops_the_tool(jail, monkeypatch, how):
    """128 MiB the attribution cannot see: the fall it leaves unexplained passes 4 x (cap + margin), so the tool is
    stopped as the rule did before attribution."""
    small_caps(monkeypatch)
    r = run_python(jail, HIDERS[how], 128, hold=8)
    assert r.limited == "disk-free" and not r.timed_out, r
    assert "do not explain" in r.detail and "4 x" in r.detail, r.detail
    assert no_units()


@needs_limits
@pytest.mark.parametrize("how", sorted(HIDERS))
def test_disk_hidden_from_proc_within_the_bound_is_noted_not_stopped(jail, monkeypatch, how):
    """24 MiB hidden the same way: a fall past cap + margin (16 MiB), but within the bound (64 MiB); the residual the
    rule accepts. The work filesystem is shared with other jobs, so the tool lives only 3 s: their writes in that time
    must stay under the 40 MiB of headroom (observed on the build host: about 3 MiB/s)."""
    small_caps(monkeypatch)
    r = run_python(jail, HIDERS[how], 24, hold=3)
    assert r.rc == 0 and r.limited is None, r
    assert "not stopped" in r.free_space_note, r.free_space_note
    assert no_units()


@needs_limits
def test_a_fall_past_the_bound_stops_a_tool_that_wrote_nothing(jail, monkeypatch):
    """Deterministic: the faked free space falls by 5 GiB; the tool's writes explain none of it."""
    small_caps(monkeypatch)
    falling_free(monkeypatch, after=1.0, by=5 * GIB)
    r = run_jailed(jail, "sleep 30")
    assert r.limited == "disk-free" and not r.timed_out, r
    assert r.detail.startswith("free space on the work filesystem fell by 5120 MiB"), r.detail
    assert "do not explain" in r.detail and "more than 4 x" in r.detail, r.detail
    assert no_units()


TRUNCATE_ANOTHERS_FILE = r"""
import os, sys, time
d, role = sys.argv[1], sys.argv[2]
p, done = os.path.join(d, "shared"), os.path.join(d, "written")
if role == "writer":
    with open(p, "wb") as f:
        f.write(b"\xa5" * (20 << 20))
    open(done, "w").close()
else:
    while not os.path.exists(done):
        time.sleep(0.01)
    os.truncate(p, 0)  # the writer's dirty pages, cancelled before they reach the disk: this process's count
    os.unlink(p)
time.sleep(30)
"""


@needs_limits
def test_one_process_cancelling_anothers_writes_does_not_offset_them(jail, monkeypatch):
    """The clamp: a process whose cancelled_write_bytes exceed its write_bytes (it truncated another process's dirty
    data) counts 0, not minus 20 MiB. The writer's 20 MiB then pass cap + margin (16 MiB) on a faked fall of 40 MiB,
    which is within the bound, so only the tool's own writes can stop it."""
    small_caps(monkeypatch)
    rw = jail[1]
    (rw / "t.py").write_text(TRUNCATE_ANOTHERS_FILE)
    falling_free(monkeypatch, after=2.0, by=40 * MiB)
    script = f"python3 {rw}/t.py {rw} writer & python3 {rw}/t.py {rw} truncater & wait"
    r = run_jailed(jail, script)
    assert r.limited == "disk-free" and not r.timed_out, r
    assert "the tool's own writes account for 20 MiB" in r.detail, r.detail
    assert no_units()

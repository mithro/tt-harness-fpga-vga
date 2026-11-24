# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from another test module
"""The limit counters survive, or the record says they did not; /dev is bounded; a tool that fills its scratch
directory is stopped while it runs. Real systemd-run and bwrap (skipped where they cannot work)."""

import os
import subprocess
import sys
import time
from pathlib import Path

from test_limits import box, jail, needs_limits, no_units, run_sh, units  # noqa: F401

from tthfv import limits, proc, safefile


def test_read_cgroup_reads_the_counters_the_wrapper_reports(tmp_path):
    (tmp_path / "memory.events").write_text("low 0\nhigh 0\nmax 3\noom 1\noom_kill 1\n")
    (tmp_path / "pids.events").write_text("max 7\n")
    (tmp_path / "memory.peak").write_text("12345\n")
    (tmp_path / "pids.peak").write_text("9\n")
    (tmp_path / "cpu.stat").write_text("usage_usec 4242\nuser_usec 1\n")
    got = limits.read_cgroup(tmp_path)
    assert got == {"oom_kill": 1, "tasks_max": 7, "memory_peak": 12345, "tasks_peak": 9, "cpu_usec": 4242}
    assert limits.limited_by(got) == "memory"
    assert limits.read_cgroup(tmp_path / "gone") == {}


def test_a_cgroup_is_found_only_for_the_named_unit():
    assert limits.cgroup_of(os.getpid(), "tthfv-no-such-unit") is None
    assert limits.cgroup_of(2**30, "x") is None


def test_the_unknown_reason_says_what_happened():
    assert "resource counters missing: the limit wrapper was killed" in limits.Limits().reason("unknown")
    assert "most likely by the memory limit" in limits.Limits().reason("unknown")


@needs_limits
def test_the_tool_has_a_high_oom_score(jail):
    r = run_sh(jail, "cat /proc/self/oom_score_adj", timeout=30)
    assert r.rc == 0 and r.text.split()[-1] == "1000"
    assert no_units()


@needs_limits
def test_memory_held_in_tmpfs_is_reported_as_memory_and_the_wrapper_survives(jail, monkeypatch):
    monkeypatch.setenv("TTHFV_LIMIT_MEMORY", "128M")
    script = "head -c 100000000 /dev/zero > /tmp/a; head -c 100000000 /dev/zero > /sandbox-home/b; sleep 20"
    r = run_sh(jail, script, timeout=60)
    assert r.limited == "memory", r  # not None, and not "unknown": the wrapper lived to report
    assert "memory limit (128M) exceeded" in (jail[1] / "x.log").read_text()
    assert no_units()


@needs_limits
def test_a_memory_hit_followed_by_a_timeout_is_recorded_as_memory(jail, monkeypatch):
    monkeypatch.setenv("TTHFV_LIMIT_MEMORY", "128M")
    r = run_sh(jail, "dd if=/dev/zero bs=300M count=1 | wc -c; sleep 60", timeout=6)
    assert r.timed_out is True
    assert r.limited == "memory", r
    assert no_units()


@needs_limits
def test_a_program_that_exits_with_124_is_not_a_timeout(jail):
    r = run_sh(jail, "exit 124", timeout=30)
    assert r.rc == 124 and r.timed_out is False and r.limited is None


@needs_limits
def test_when_the_wrapper_is_killed_the_outcome_is_unknown_not_a_plain_exit_code():
    scope = proc._Scope(limits.Limits("1G", 512, "400%"), 30)
    argv = scope.argv(["sh", "-c", "kill -9 $PPID; sleep 2"])  # the wrapper is the tool's parent
    p = subprocess.Popen(
        argv, env=limits.host_env(), stdin=subprocess.DEVNULL, pass_fds=(scope.wfd,), start_new_session=True
    )
    scope.started_child(p.pid)
    rc = p.wait(30)
    limited, counters = scope.finish(rc, False)
    assert limited == "unknown", (limited, counters)
    deadline = time.monotonic() + 15  # the orphaned `sleep 2` keeps the scope for a moment
    while units() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert no_units()


@needs_limits
def test_dev_is_read_only_but_the_device_nodes_and_a_small_dev_shm_work(jail):
    ok = run_sh(jail, "echo x > /dev/null && head -c 4 /dev/urandom | wc -c && head -c 4 /dev/zero | wc -c")
    assert ok.rc == 0
    assert run_sh(jail, "echo x > /dev/file").rc != 0  # /dev is a tmpfs that cannot be sized: read-only
    assert run_sh(jail, "echo x > /dev/shm/small").rc == 0
    assert run_sh(jail, "head -c 100000000 /dev/zero > /dev/shm/big").rc != 0  # /dev/shm is 64 MiB


@needs_limits
def test_a_disk_filling_tool_is_stopped_by_the_watchdog_and_reported_as_disk(jail, monkeypatch, tmp_path):
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 300 << 20)
    _ro, rw = jail
    script = f"while :; do head -c 104857600 /dev/zero >> {rw}/big; sleep 1; done"
    started = time.monotonic()
    r = proc.run_logged(
        ["sh", "-c", script], 120, tmp_path / "w.log", cwd=rw, sandbox=box(jail),
        scratch_check=lambda: safefile.scratch_exceeded(rw),
    )  # fmt: skip
    assert r.limited == "disk" and "scratch limit (300 MiB) exceeded during the run" in r.detail
    log = (tmp_path / "w.log").read_text()
    assert "counters missing" not in log and "[tthfv] scratch limit (300 MiB) exceeded during the run" in log
    assert time.monotonic() - started < 20
    assert not r.timed_out and no_units()
    assert (rw / "big").stat().st_size < 700 << 20


def test_the_watchdog_works_without_a_sandbox_and_stops_a_fast_writer_within_seconds(tmp_path, monkeypatch):
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 300 << 20)
    d = tmp_path / "scratch"
    d.mkdir()
    script = f"while :; do head -c 104857600 /dev/zero >> {d}/big; sleep 1; done"  # 100 MiB per second
    started = time.monotonic()
    r = proc.run_logged(
        ["sh", "-c", script],
        120,
        tmp_path / "w.log",
        cwd=tmp_path,
        scratch_check=lambda: safefile.scratch_exceeded(d),
    )
    assert r.limited == "disk" and not r.timed_out
    assert time.monotonic() - started < 10, "stopped within about 5 s of the cap being passed"
    assert r.watchdog is not None and r.watchdog[0] >= 1


@needs_limits
def test_the_runs_slice_is_gone_when_the_process_that_made_it_exits(tmp_path):
    env = {**os.environ, "TTHFV_SANDBOX": "bwrap", "PYTHONPATH": os.pathsep.join(sys.path)}
    env.pop("TTHFV_LIMITS", None)
    driver = Path(__file__).parent / "slice_driver.py"
    done = subprocess.run(
        [sys.executable, str(driver), str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    pid, rc = done.stdout.split()
    assert rc == "0"
    listing = subprocess.run(
        [limits.SYSTEMCTL, "--user", "list-units", f"tthfv_run_{pid}.slice", "--all", "--no-legend", "--plain"],
        check=False, capture_output=True, text=True, timeout=30, env=limits.host_env(),
    )  # fmt: skip
    assert listing.stdout.strip() == "", listing.stdout

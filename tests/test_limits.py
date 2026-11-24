# SPDX-License-Identifier: Apache-2.0
"""Resource limits with real cgroups: each sandboxed program runs in a systemd user scope with a memory ceiling,
a task limit and a CPU quota. Real systemd-run and bwrap; skipped, with the reason, where either cannot work."""

import os
import stat
import subprocess
import time

import pytest

from tthfv import limits, proc, sandbox
from tthfv.errors import UserError

_WHY = ""
if not sandbox.available():
    _WHY = f"bwrap cannot run here: {sandbox.unavailable_reason()}"
elif limits.unavailable_reason():
    _WHY = f"no systemd user scope with limits here: {limits.unavailable_reason()}"
needs_limits = pytest.mark.skipif(bool(_WHY), reason=_WHY)


@pytest.fixture
def jail(tmp_path, monkeypatch):
    """A (rw, ro) pair of directories and the environment of a real, limited sandbox."""
    monkeypatch.setenv("TTHFV_SANDBOX", "bwrap")
    monkeypatch.delenv("TTHFV_LIMITS", raising=False)
    for k in ("TTHFV_LIMIT_MEMORY", "TTHFV_LIMIT_TASKS", "TTHFV_LIMIT_CPU"):
        monkeypatch.delenv(k, raising=False)
    ro, rw = tmp_path / "ro", tmp_path / "rw"
    ro.mkdir()
    rw.mkdir()
    return ro, rw


def box(jail):
    ro, rw = jail
    b = sandbox.for_stage(rw=rw, ro=[ro])
    assert b is not None and b.limits is not None
    return b


def run_sh(jail, script, timeout=60):
    _ro, rw = jail
    return proc.run_logged(["sh", "-c", script], timeout, rw / "x.log", cwd=rw, sandbox=box(jail))


def units() -> list[str]:
    done = subprocess.run(
        [limits.SYSTEMCTL, "--user", "list-units", "tthfv-*", "--all", "--no-legend", "--plain"],
        check=False, capture_output=True, text=True, timeout=30, env=limits.host_env(),
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    # only this process's units: other test runs on the machine have their own
    return [w for w in done.stdout.split() if w.startswith(f"tthfv-{os.getpid()}-") and w.endswith(".scope")]


def no_units(wait: float = 15.0) -> bool:
    """True once this process has no tthfv scope left; systemd needs a moment to remove one after a kill."""
    end = time.monotonic() + wait
    while units():
        if time.monotonic() > end:
            return False
        time.sleep(0.2)
    return True


# ---- configuration (no systemd needed)


def test_defaults_and_environment_overrides(monkeypatch):
    for k in ("TTHFV_LIMIT_MEMORY", "TTHFV_LIMIT_TASKS", "TTHFV_LIMIT_CPU"):
        monkeypatch.delenv(k, raising=False)
    assert limits.from_env() == limits.Limits("8G", 512, "400%")
    monkeypatch.setenv("TTHFV_LIMIT_MEMORY", "128M")
    monkeypatch.setenv("TTHFV_LIMIT_TASKS", "64")
    monkeypatch.setenv("TTHFV_LIMIT_CPU", "100%")
    assert limits.from_env().record() == {"memory": "128M", "tasks": 64, "cpu": "100%"}
    assert limits.from_env().memory_bytes == 128 << 20


@pytest.mark.parametrize(
    "var,value", [("TTHFV_LIMIT_MEMORY", "lots"), ("TTHFV_LIMIT_MEMORY", "0"), ("TTHFV_LIMIT_TASKS", "-1"),
                  ("TTHFV_LIMIT_TASKS", "many"), ("TTHFV_LIMIT_CPU", "4"), ("TTHFV_LIMIT_CPU", "0%")],
)  # fmt: skip
def test_a_bad_limit_is_a_user_error(monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    with pytest.raises(UserError, match=var):
        limits.from_env()


def test_the_scope_command_line():
    argv = limits.scope_argv(["bwrap", "x"], limits.Limits("8G", 512, "400%"), "tthfv-1-2", 100, 9)
    assert argv[:9] == [
        limits.SYSTEMD_RUN,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--unit",
        "tthfv-1-2",
        "--slice",
        limits.run_slice(),
    ]
    for prop in ("MemoryMax=8G", "MemorySwapMax=0", "TasksMax=512", "CPUQuota=400%", "RuntimeMaxSec=130"):
        assert prop in argv
    assert argv[-2:] == ["bwrap", "x"] and "9>&-" in argv[argv.index("-c") + 1]


def test_limits_none_is_explicit_and_warns_once(monkeypatch, capsys):
    monkeypatch.setenv("TTHFV_LIMITS", "none")
    monkeypatch.setattr(limits, "_warned", False)
    limits.announce()
    limits.announce()
    assert limits.for_stage() is None
    assert capsys.readouterr().err.count("WARNING: TTHFV_LIMITS=none") == 1


def test_a_bad_mode_is_a_user_error(monkeypatch):
    monkeypatch.setenv("TTHFV_LIMITS", "off")
    with pytest.raises(UserError, match="TTHFV_LIMITS"):
        limits.mode()


def test_without_systemd_the_stage_fails_with_instructions_and_runs_nothing(monkeypatch, tmp_path):
    fake = tmp_path / "systemd-run"
    marker = tmp_path / "ran"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(limits, "SYSTEMD_RUN", str(fake))
    monkeypatch.setenv("TTHFV_SANDBOX", "bwrap")
    monkeypatch.delenv("TTHFV_LIMITS", raising=False)
    limits.reset()
    try:
        with pytest.raises(UserError, match="TTHFV_LIMITS=none"):
            limits.for_stage()
        if sandbox.available():
            with pytest.raises(UserError, match="resource limits"):
                sandbox.for_stage(rw=tmp_path / "rw", ro=[])
    finally:
        limits.reset()
    assert not (tmp_path / "rw").exists()  # nothing was made for a stage that could not start


def test_a_missing_systemd_run_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(limits, "SYSTEMD_RUN", str(tmp_path / "nowhere"))
    limits.reset()
    try:
        assert "not installed" in limits.unavailable_reason()
    finally:
        limits.reset()


@needs_limits
def test_limits_none_runs_without_a_scope(jail, monkeypatch):
    monkeypatch.setenv("TTHFV_LIMITS", "none")
    ro, rw = jail
    b = sandbox.for_stage(rw=rw, ro=[ro])
    assert b is not None and b.limits is None
    r = proc.run_logged(["sh", "-c", "cat /proc/self/cgroup"], 30, rw / "x.log", cwd=rw, sandbox=b)
    assert r.rc == 0 and "tthfv-" not in r.text and r.limited is None


# ---- the limits themselves


@needs_limits
def test_the_limits_are_in_force_inside_the_scope(jail):
    r = run_sh(jail, "cat /proc/self/cgroup", timeout=30)
    assert r.rc == 0 and r.limited is None
    assert r.peak_memory and r.peak_tasks and r.peak_tasks >= 1
    assert no_units()


@needs_limits
def test_a_process_over_the_memory_limit_is_killed_and_reported(jail, monkeypatch):
    monkeypatch.setenv("TTHFV_LIMIT_MEMORY", "128M")
    r = run_sh(jail, "dd if=/dev/zero bs=512M count=1 | wc -c")  # a 512 MiB buffer, all touched
    assert r.limited == "memory", r
    assert "memory limit (128M) exceeded" in (jail[1] / "x.log").read_text()
    assert no_units()


@needs_limits
def test_a_process_inside_the_memory_limit_is_not_reported(jail, monkeypatch):
    monkeypatch.setenv("TTHFV_LIMIT_MEMORY", "256M")
    r = run_sh(jail, "dd if=/dev/zero bs=32M count=1 | wc -c")
    assert r.rc == 0 and r.limited is None


@needs_limits
def test_many_children_cannot_exceed_the_task_limit(jail, monkeypatch):
    monkeypatch.setenv("TTHFV_LIMIT_TASKS", "64")
    r = run_sh(jail, "for i in $(seq 2000); do sleep 3 & done; wait")
    assert r.limited == "tasks", r
    assert r.peak_tasks is not None and r.peak_tasks <= 64
    assert "task limit (64) reached" in (jail[1] / "x.log").read_text()
    assert no_units()


@needs_limits
def test_cpu_quota_holds_four_busy_threads_to_about_one_core(jail, monkeypatch):
    monkeypatch.setenv("TTHFV_LIMIT_CPU", "100%")
    started = time.monotonic()
    script = "for i in 1 2 3 4; do timeout 3 sh -c 'while :; do :; done' & done; wait"
    r = run_sh(jail, script, timeout=60)
    wall = time.monotonic() - started
    cpu = r.cpu_usec / 1e6  # user + system time of everything in the scope, from its cgroup
    assert r.limited is None
    assert wall >= 3
    assert cpu / 3 < 1.6, f"{cpu:.1f} s of CPU in 3 s of wall time"
    assert cpu > 1.0  # it was busy: the test measures something


@needs_limits
def test_a_timeout_stops_the_scope_and_leaves_no_unit(jail):
    token = f"sleep 4747.{os.getpid()}"
    started = time.monotonic()
    r = run_sh(jail, f"setsid {token} & {token}", timeout=2)
    assert r.rc == proc.TIMEOUT_RC and r.limited == "time"
    assert time.monotonic() - started < 20
    deadline = time.monotonic() + 10
    while units() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert no_units()
    ps = subprocess.run(["pgrep", "-f", token], check=False, capture_output=True, text=True)
    assert ps.stdout.strip() == ""


@needs_limits
def test_the_runtime_backstop_ends_a_scope_the_host_did_not_stop(jail, monkeypatch):
    monkeypatch.setattr(limits, "RUNTIME_SLACK", -97)  # RuntimeMaxSec=3 for a tool that is given 100 s
    started = time.monotonic()
    r = run_sh(jail, "sleep 60", timeout=100)
    assert time.monotonic() - started < 30
    assert r.limited == "time" and r.rc != 0
    assert no_units()


@needs_limits
def test_ctrl_c_kills_the_group_stops_the_scope_and_leaves_nothing(jail, monkeypatch):
    token = f"sleep 4848.{os.getpid()}"
    real_wait = subprocess.Popen.wait
    state = {"raised": False}

    def interrupted(self, timeout=None):
        if timeout is not None and not state["raised"]:
            state["raised"] = True
            time.sleep(1.5)  # let the program start
            raise KeyboardInterrupt
        return real_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_sh(jail, f"setsid {token} & {token}")
    monkeypatch.undo()
    deadline = time.monotonic() + 10
    while units() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert no_units()
    assert (
        subprocess.run(["pgrep", "-f", token], check=False, capture_output=True, text=True).stdout.strip()
        == ""
    )


def test_the_probe_is_asked_once_per_process(monkeypatch):
    limits.reset()
    calls = []
    monkeypatch.setattr(limits, "_probe", lambda: calls.append(1) or "")
    try:
        assert limits.unavailable_reason() == "" and limits.unavailable_reason() == ""
        assert len(calls) == 1
    finally:
        limits.reset()

# SPDX-License-Identifier: Apache-2.0

"""Nothing may outlive a run: not after a closed terminal (SIGHUP to the group), not after SIGKILL of the run and
its forkserver and workers together, not after SIGKILL of a standalone command running a quiet tool; and the
run's empty slice is stopped by the next run at the latest. Real sandbox, real scopes, limits on."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_limits import needs_limits, units  # noqa: F401
from test_orphans import alive, units_of
from test_sandbox_stages import _MISSING

from tthfv import limits, runner

HERE = Path(__file__).parent
needs_toolchain = pytest.mark.skipif(bool(_MISSING), reason=_MISSING)  # the standalone driver runs Verilator


def environment(tmp_path: Path) -> dict[str, str]:
    env = {
        **os.environ,
        "TTHFV_WORK": str(tmp_path / "work"),
        "TTHFV_SANDBOX": "bwrap",
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    env.pop("TTHFV_LIMITS", None)
    return env


def tool_processes(token: str) -> list[int]:
    """Host processes whose command line is `sleep <token>` (the tool inside a jail shows up here too)."""
    found = []
    needle = b"sleep\0" + token.encode() + b"\0"
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                if needle in (p / "cmdline").read_bytes():
                    found.append(int(p.name))
            except OSError:
                pass
    return found


def slices_of(pid: int) -> list[str]:
    done = subprocess.run(
        [limits.SYSTEMCTL, "--user", "list-units", f"tthfv_run_{pid}_*.slice", "--all", "--no-legend", "--plain"],
        check=False, capture_output=True, text=True, timeout=30, env=limits.host_env(),
    )  # fmt: skip
    return [w for w in done.stdout.split() if w.startswith("tthfv_run_")]


def wait_until(cond, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.25)
    return cond()


def start_run(tmp_path, token: str, new_session=True):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    p = subprocess.Popen(
        [sys.executable, str(HERE / "orphan_driver.py"), str(state), token],
        env=environment(tmp_path), stdout=subprocess.DEVNULL, start_new_session=new_session,
    )  # fmt: skip
    ok = wait_until(lambda: len(list(state.glob("worker-*"))) >= 2 and len(tool_processes(token)) >= 2, 90)
    workers = [int(w.name.split("-")[1]) for w in state.glob("worker-*")]
    assert ok, "the run did not get going"
    return p, state, workers


def cleanup(token: str, workers: list[int], *procs) -> None:
    """Only what this test started: its own driver, its workers and their scopes."""
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait()
    for pid in workers:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
        for unit in units_of([pid]):
            limits.stop_unit(unit.removesuffix(".scope"))


def token_for(tmp_path) -> str:
    return f"7{os.getpid()}{abs(hash(str(tmp_path))) % 1000}"


@needs_limits
def test_sighup_to_the_process_group_leaves_no_tool_no_scope_and_no_slice(tmp_path):
    token = token_for(tmp_path)
    run, _state, workers = start_run(tmp_path, token)
    try:
        os.killpg(run.pid, signal.SIGHUP)  # what a closed terminal does
        run.wait(30)
        assert wait_until(lambda: not tool_processes(token) and not units_of(workers), 10), (
            tool_processes(token), units_of(workers),
        )  # fmt: skip
        assert wait_until(lambda: not slices_of(run.pid), 10), slices_of(run.pid)
        assert run.returncode in (128 + signal.SIGHUP, -signal.SIGHUP), run.returncode
    finally:
        cleanup(token, workers, run)


@needs_limits
def test_sigkill_of_the_run_the_forkserver_and_the_workers_together_leaves_no_tool_and_the_next_run_sweeps_the_slice(
    tmp_path,
):
    token = token_for(tmp_path)
    run, _state, workers = start_run(tmp_path, token, new_session=False)
    try:
        victims = {run.pid, *workers, *[p for p in runner._descendants(run.pid) if _is_multiprocessing(p)]}
        victims = {v for v in victims if alive(v)}
        for v in victims:
            os.kill(v, signal.SIGKILL)  # one after the other, as fast as possible: no one is left to react
        run.wait(30)
        assert wait_until(lambda: not tool_processes(token) and not units_of(workers), 10), (
            tool_processes(token), units_of(workers),
        )  # fmt: skip
        stale = slices_of(run.pid)
        # the next run starts by stopping the slices whose owner is dead
        nxt = subprocess.Popen(
            [sys.executable, str(HERE / "orphan_driver.py"), str(tmp_path / "state2"), "1"],
            env=environment(tmp_path), stdout=subprocess.DEVNULL,
        )  # fmt: skip
        (tmp_path / "state2").mkdir(exist_ok=True)
        assert nxt.wait(180) == 0
        assert wait_until(lambda: not slices_of(run.pid), 10), (stale, slices_of(run.pid))
    finally:
        cleanup(token, workers, run)
        for w in (tmp_path / "state2").glob("worker-*"):
            cleanup(token, [int(w.name.split("-")[1])])


def _is_multiprocessing(pid: int) -> bool:
    try:
        return b"multiprocessing" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


@needs_limits
@needs_toolchain
def test_sigkill_of_a_standalone_command_with_a_quiet_tool_leaves_nothing(tmp_path):
    token = token_for(tmp_path)
    p = subprocess.Popen(
        [sys.executable, str(HERE / "standalone_driver.py"), token],
        env=environment(tmp_path), stdout=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        assert wait_until(lambda: len(tool_processes(token)) >= 1, 120), "the simulation did not start"
        assert units_of([p.pid]), "the tool is not in a scope of this process"
        p.send_signal(signal.SIGKILL)
        p.wait(30)
        assert wait_until(lambda: not tool_processes(token) and not units_of([p.pid]), 10), (
            tool_processes(token), units_of([p.pid]),
        )  # fmt: skip
    finally:
        cleanup(token, [p.pid], p)


@needs_limits
@needs_toolchain
def test_sigterm_to_a_standalone_command_stops_its_scope_and_slice(tmp_path):
    token = token_for(tmp_path) + "1"
    p = subprocess.Popen(
        [sys.executable, str(HERE / "standalone_driver.py"), token],
        env=environment(tmp_path), stdout=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        assert wait_until(lambda: len(tool_processes(token)) >= 1, 120)
        p.send_signal(signal.SIGTERM)
        assert p.wait(30) == 128 + signal.SIGTERM
        assert wait_until(lambda: not tool_processes(token) and not units_of([p.pid]), 10)
        assert wait_until(lambda: not slices_of(p.pid), 10), slices_of(p.pid)
    finally:
        cleanup(token, [p.pid], p)


# ---- the sweep of stale slices, without a run


def make_slice_scope(name: str, seconds: int) -> subprocess.Popen:
    """A scope of this test, in the named slice, running `sleep <seconds>`."""
    return subprocess.Popen(
        [limits.SYSTEMD_RUN, "--user", "--scope", "--quiet", "--collect", "--slice", name, "--", "sleep", str(seconds)],
        env=limits.host_env(), stdin=subprocess.DEVNULL,
    )  # fmt: skip


@needs_limits
def test_the_sweep_stops_slices_of_dead_or_reused_pids_and_never_one_of_a_living_owner(tmp_path):
    me = os.getpid()
    mine = limits.start_time(me)
    dead = f"tthfv_run_2147483000_{mine}.slice"  # no such process
    reused = f"tthfv_run_{me}_1.slice"  # this pid, but not this process's start time
    alive_one = f"tthfv_run_{me}_{mine}.slice"
    procs = [make_slice_scope(n, 300) for n in (dead, reused, alive_one)]
    try:
        assert wait_until(lambda: all(_slice_loaded(n) for n in (dead, reused, alive_one)), 20)
        stopped = limits.sweep_stale_slices()
        assert dead in stopped and reused in stopped and alive_one not in stopped
        assert wait_until(lambda: not _slice_loaded(dead) and not _slice_loaded(reused), 10)
        assert _slice_loaded(alive_one) and procs[2].poll() is None
    finally:
        for n in (dead, reused, alive_one):
            limits.stop_slice(n)
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.wait()


def _slice_loaded(name: str) -> bool:
    done = subprocess.run(
        [limits.SYSTEMCTL, "--user", "list-units", name, "--all", "--no-legend", "--plain"],
        check=False, capture_output=True, text=True, timeout=30, env=limits.host_env(),
    )  # fmt: skip
    return name in done.stdout.split()


# ---- a worker killed in the first milliseconds of a tool's start-up


@needs_limits
@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: a worker SIGKILLed 20-90 ms after it started a tool leaves the jail (the inner bwrap) and its "
    "payload running inside the scope for the rest of the tool's timeout, in about 13% of such kills: the "
    "`setpriv --pdeathsig KILL` wrapper dies and takes the outer bwrap with it, but the inner bwrap had not armed "
    "--die-with-parent yet. Only the next run's sweep of dead owners' slices or RuntimeMaxSec stops it. "
    "Remove this mark when it is closed (see the README, Known limitations).",
)
def test_a_worker_killed_while_its_tool_starts_leaves_no_process(tmp_path):
    import threading

    base = f"4343.{os.getpid()}"
    workers: list[subprocess.Popen] = []
    tokens: dict[int, str] = {}
    lock = threading.Lock()
    env = environment(tmp_path)

    def trial(i: int) -> None:
        token = f"{base}{i:03d}"
        marker = tmp_path / f"marker{i}"
        w = subprocess.Popen(
            [sys.executable, str(HERE / "backstop_worker.py"), str(tmp_path / f"j{i}"), token, str(marker)],
            env=env, stdout=subprocess.DEVNULL,
        )  # fmt: skip
        with lock:
            workers.append(w)
            tokens[w.pid] = token
        end = time.monotonic() + 60
        while not marker.exists() and time.monotonic() < end:
            time.sleep(0.0005)
        time.sleep(0.02 + 0.01 * (i % 8))  # 20 ms to 90 ms after the worker announced the start
        w.kill()
        w.wait()

    try:
        # five rounds of eight workers at a time: more would only slow the start
        for first in range(0, 40, 8):
            threads = [threading.Thread(target=trial, args=(i,)) for i in range(first, first + 8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(120)
        time.sleep(10)
        left = {t: tool_processes(t) for t in tokens.values() if tool_processes(t)}
        if not left:
            pytest.skip("the start-up window was not hit by 40 kills on this machine")
        assert not left, f"tools outlived their killed workers by 10 s: {left}"
    finally:
        for w in workers:  # only what this test started: its workers, their scopes and slices, their jails
            if w.poll() is None:
                w.kill()
                w.wait()
        for tok in tokens.values():
            for pid in tool_processes(tok):
                os.kill(pid, signal.SIGKILL)
        for pid in tokens:
            for unit in units_of([pid]):
                limits.stop_unit(unit.removesuffix(".scope"))
            for name in slices_of(pid):
                limits.stop_slice(name)

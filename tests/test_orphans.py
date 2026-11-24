# SPDX-License-Identifier: Apache-2.0

"""SIGKILL of the `tthfv run` process must not leave workers, tool scopes or jails running."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from driver_env import did_not_start, driver_env, stderr_tail
from test_limits import needs_limits, units  # noqa: F401

from tthfv import limits, runner


def alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def units_of(pids: list[int]) -> list[str]:
    done = subprocess.run(
        [limits.SYSTEMCTL, "--user", "list-units", "tthfv-*", "--all", "--no-legend", "--plain"],
        check=False, capture_output=True, text=True, timeout=30, env=limits.host_env(),
    )  # fmt: skip
    return [
        w
        for w in done.stdout.split()
        if w.endswith(".scope") and any(w.startswith(f"tthfv-{p}-") for p in pids)
    ]


@needs_limits
def test_sigkill_of_the_parent_leaves_no_worker_and_no_scope(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    env = driver_env(tmp_path)
    err = tmp_path / "driver.err"
    driver = Path(__file__).parent / "orphan_driver.py"
    with err.open("wb") as errf:
        parent = subprocess.Popen(
            [sys.executable, str(driver), str(state)], env=env, stdout=subprocess.DEVNULL, stderr=errf
        )
    try:
        deadline = time.monotonic() + 60
        workers: list[int] = []
        while time.monotonic() < deadline:
            workers = [int(p.name.split("-")[1]) for p in state.glob("worker-*")]
            if len(workers) >= 2 and len(units_of(workers)) >= 2:
                break
            time.sleep(0.5)
        assert len(workers) >= 2 and len(units_of(workers)) >= 2, did_not_start(err)
        parent.send_signal(signal.SIGKILL)
        parent.wait(30)
        end = time.monotonic() + 30
        while time.monotonic() < end and (any(alive(w) for w in workers) or units_of(workers)):
            time.sleep(0.5)
        assert not [w for w in workers if alive(w)], "worker processes of the killed run are still alive"
        assert units_of(workers) == [], "scopes of the killed run are still there"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        for p in state.glob("worker-*"):  # only pids this test saw its own run start
            pid = int(p.name.split("-")[1])
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
            for unit in units_of([pid]):
                limits.stop_unit(unit.removesuffix(".scope"))
    assert runner.DEFAULT_JOBS == 4


@needs_limits
def test_a_run_that_ends_normally_leaves_no_slice_either(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    env = driver_env(tmp_path)
    err = tmp_path / "driver.err"
    driver = Path(__file__).parent / "orphan_driver.py"
    with err.open("wb") as errf:
        run = subprocess.Popen(
            [sys.executable, str(driver), str(state), "1"], env=env, stdout=subprocess.DEVNULL, stderr=errf
        )
        code = run.wait(180)
    assert code == 0, f"exit status {code}; the driver said: {stderr_tail(err)}"
    assert len(list(state.glob("worker-*"))) >= 1
    listing = subprocess.run(
        [limits.SYSTEMCTL, "--user", "list-units", f"tthfv_run_{run.pid}_*.slice", "--all", "--no-legend", "--plain"],
        check=False, capture_output=True, text=True, timeout=30, env=limits.host_env(),
    )  # fmt: skip
    assert listing.stdout.strip() == "", listing.stdout
    assert units_of([int(p.name.split("-")[1]) for p in state.glob("worker-*")]) == []

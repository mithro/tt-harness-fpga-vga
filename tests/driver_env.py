# SPDX-License-Identifier: Apache-2.0
"""The one place that builds the environment of the driver subprocesses (tests/orphan_driver.py,
tests/standalone_driver.py) that run a real `tthfv run` in a real sandbox with the cgroup limits on.

`tthfv run` refuses a run whose jobs x TTHFV_LIMIT_MEMORY is more than half of the machine's memory
(runner.commitment), and the default limit is 8G: on a 16 GiB CI runner two jobs are refused. The tools in these
tests are `sleep`, so the drivers get a small explicit limit and no test depends on the size of the machine.
TTHFV_TEST_DRIVER_MEMORY overrides it, to show that a limit that is too big for the machine is what such a run
fails on."""

import os
import sys
from pathlib import Path

DRIVER_MEMORY = os.environ.get("TTHFV_TEST_DRIVER_MEMORY", "512M")
# A standalone command compiles a Verilator model inside its scope (the peak of the largest first-batch simulation is
# about 570 MiB), so it needs more; it makes no jobs x limit check, so a few GiB is fine on any machine.
STANDALONE_MEMORY = os.environ.get("TTHFV_TEST_STANDALONE_MEMORY", "2G")


def driver_env(tmp_path: Path, memory: str | None = None) -> dict[str, str]:
    """The environment of a driver: its own work directory, the sandbox, the limits ON (not the suite's default of
    none), and a memory limit small enough for any machine (`sleep` tools: DRIVER_MEMORY; pass STANDALONE_MEMORY
    for a driver that compiles)."""
    env = {
        **os.environ,
        "TTHFV_WORK": str(tmp_path / "work"),
        "TTHFV_SANDBOX": "bwrap",
        "TTHFV_LIMIT_MEMORY": memory or DRIVER_MEMORY,
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    env.pop("TTHFV_LIMITS", None)  # the default: limits on
    return env


def stderr_tail(path: Path, size: int = 1500) -> str:
    """The end of a driver's stderr, for an assertion message."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return "(no stderr captured)"
    return text[-size:].strip() or "(stderr empty)"


def did_not_start(path: Path) -> str:
    return f"the run did not get going; the driver said: {stderr_tail(path)}"

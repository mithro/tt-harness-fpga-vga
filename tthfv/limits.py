# SPDX-License-Identifier: Apache-2.0
"""Hard resource limits for every sandboxed tool: each invocation runs in its own transient systemd user scope
(cgroup v2) with a memory ceiling (and no swap), a limit on tasks (processes and threads) and a CPU quota.

    systemd-run --user --scope --quiet --collect --unit tthfv-<pid>-<n> \\
        -p MemoryMax=8G -p MemorySwapMax=0 -p TasksMax=512 -p CPUQuota=400% -p RuntimeMaxSec=<timeout+30> \\
        -- /bin/bash -c '<wrapper>' bash bwrap ...

--scope runs the command in the foreground with our own stdin, stdout and stderr (the log capture needs that);
--collect unloads the unit when it ends, also a failed one, so nothing is left behind. The wrapper shell
(outside the jail, in the scope) runs bwrap, and when it has finished reads the scope's own counters before
systemd removes the cgroup: memory.events oom_kill (a process was killed for exceeding MemoryMax),
pids.events max (a fork or thread creation was refused at TasksMax), and memory.peak and pids.peak. They
are written to a pipe the host passed as file descriptor 3. (`systemctl --user show -p Result` cannot be used
after the fact: a collected scope no longer exists, and a scope whose processes were killed by the kernel's
OOM killer ends with result "success" anyway.) RuntimeMaxSec is a backstop above the tool's own timeout.

Defaults are the constants below; TTHFV_LIMIT_MEMORY, TTHFV_LIMIT_TASKS and TTHFV_LIMIT_CPU override them and
the values in force are recorded in build.json and reference.json. TTHFV_LIMITS=none turns the limits off
explicitly (a warning is printed once per run); without it, a machine that cannot make such a scope is a
UserError, as for the sandbox.
"""

import itertools
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Literal

from tthfv.errors import UserError

SYSTEMD_RUN = shutil.which("systemd-run") or "systemd-run"
SYSTEMCTL = shutil.which("systemctl") or "systemctl"
ENV_MODE = "TTHFV_LIMITS"
DEFAULT_MEMORY = "8G"
DEFAULT_TASKS = 512
DEFAULT_CPU = "400%"
RUNTIME_SLACK = 30  # seconds added to a tool's timeout for RuntimeMaxSec, the backstop
PROBE_TIMEOUT = 30
UNIT_PREFIX = "tthfv-"

Limited = Literal["memory", "tasks", "time"]

_MEMORY = re.compile(r"^([1-9]\d*)([KMGT]?)$")
_CPU = re.compile(r"^([1-9]\d*)%$")
_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}

# Runs the command given as arguments (with the pipe's descriptor closed for it), then reports the scope's
# counters on that descriptor. Everything here is trusted host code; it runs outside the jail. The last value
# of a name in the report is the one that counts, and only this script writes after the jail has ended.
WRAPPER = r"""
"$@" {fd}>&-
rc=$?
cg=/sys/fs/cgroup$(sed -n 's/^0:://p' /proc/self/cgroup)
{{
  echo "oom_kill $(sed -n 's/^oom_kill //p' "$cg/memory.events")"
  echo "tasks_max $(sed -n 's/^max //p' "$cg/pids.events")"
  echo "memory_peak $(cat "$cg/memory.peak")"
  echo "tasks_peak $(cat "$cg/pids.peak")"
  echo "cpu_usec $(sed -n 's/^usage_usec //p' "$cg/cpu.stat")"
}} >&{fd}
exit $rc
"""


@dataclass(frozen=True)
class Limits:
    memory: str = DEFAULT_MEMORY  # MemoryMax, e.g. "8G"
    tasks: int = DEFAULT_TASKS  # TasksMax
    cpu: str = DEFAULT_CPU  # CPUQuota, e.g. "400%"

    @property
    def memory_bytes(self) -> int:
        m = _MEMORY.match(self.memory)
        assert m
        return int(m.group(1)) * _UNITS[m.group(2)]

    def record(self) -> dict:
        """What build.json and reference.json carry, and what the reuse digests include."""
        return asdict(self)

    def reason(self, limited: str) -> str:
        return {
            "memory": f"memory limit ({self.memory}) exceeded",
            "tasks": f"task limit ({self.tasks}) reached",
            "time": "runtime limit exceeded",
        }[limited]


def mode() -> Literal["scope", "none"]:
    value = os.environ.get(ENV_MODE) or "scope"
    if value not in ("scope", "none"):
        raise UserError(f"{ENV_MODE} must be unset or 'none', not {value!r}")
    return value  # type: ignore[return-value]


def from_env() -> Limits:
    """The limits in force: the defaults, overridden by the environment. UserError for a bad value."""
    memory = os.environ.get("TTHFV_LIMIT_MEMORY") or DEFAULT_MEMORY
    tasks = os.environ.get("TTHFV_LIMIT_TASKS") or str(DEFAULT_TASKS)
    cpu = os.environ.get("TTHFV_LIMIT_CPU") or DEFAULT_CPU
    if not _MEMORY.match(memory):
        raise UserError(f"TTHFV_LIMIT_MEMORY must look like 512M or 8G, not {memory!r}")
    if not tasks.isdigit() or int(tasks) < 1:
        raise UserError(f"TTHFV_LIMIT_TASKS must be a positive number, not {tasks!r}")
    if not _CPU.match(cpu):
        raise UserError(f"TTHFV_LIMIT_CPU must look like 400%, not {cpu!r}")
    return Limits(memory, int(tasks), cpu)


def _properties(limits: Limits) -> list[str]:
    out: list[str] = []
    for prop in (
        f"MemoryMax={limits.memory}",
        "MemorySwapMax=0",
        f"TasksMax={limits.tasks}",
        f"CPUQuota={limits.cpu}",
        # the default (stop) would have systemd kill the whole scope, wrapper included, when the kernel kills one
        # process for the memory limit; the wrapper must live to report it
        "OOMPolicy=continue",
    ):
        out += ["-p", prop]
    return out


def _probe() -> str:
    """'' if a scope with the three properties can be made and the controllers are really in force, else why not."""
    probe = Limits("64M", 64, "200%")
    check = (
        'cg=/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup); '
        'echo "$(cat $cg/memory.max) $(cat $cg/memory.swap.max) $(cat $cg/pids.max) $(cat $cg/cpu.max)"'
    )
    args = [
        SYSTEMD_RUN,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        *_properties(probe),
        "--",
        "/bin/sh",
        "-c",
        check,
    ]
    try:
        done = subprocess.run(
            args, check=False, capture_output=True, text=True, timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL,
            env=host_env(),
        )  # fmt: skip
    except FileNotFoundError:
        return f"{SYSTEMD_RUN!r} is not installed"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"{type(e).__name__}: {e}"
    if done.returncode != 0:
        return done.stderr.strip() or f"systemd-run exited with {done.returncode}"
    got = done.stdout.split()
    if got[:3] != ["67108864", "0", "64"] or got[3:4] != ["200000"]:
        return f"the scope was made but its limits are not in force (memory.max, swap.max, pids.max, cpu.max = {done.stdout.strip()!r}); the memory, pids and cpu controllers may not be delegated to your user"
    return ""


_probed: str | None = None


def unavailable_reason() -> str:
    """'' if limited scopes can be made here, else why not. Asked once per process."""
    global _probed
    if _probed is None:
        _probed = _probe()
    return _probed


def reset() -> None:
    """Forget the probe's answer; for tests that change SYSTEMD_RUN."""
    global _probed
    _probed = None


def host_env() -> dict[str, str]:
    """The little systemd-run needs to reach the user manager (it is not the jail's environment: bwrap clears it)."""
    env = {"PATH": "/usr/bin:/bin"}
    for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def for_stage() -> Limits | None:
    """The limits for a stage's tools, None for TTHFV_LIMITS=none. UserError where scopes cannot be made."""
    if mode() == "none":
        return None
    limits = from_env()
    if why := unavailable_reason():
        raise UserError(
            f"resource limits need systemd-run --user --scope with the memory, pids and cpu controllers, but: {why}. "
            f"Fix your user session (systemd user manager, controllers delegated), or set {ENV_MODE}=none to run "
            "without limits (the machine is then not protected from a design that uses all its memory or processes)"
        )
    return limits


_warned = False
_counter = itertools.count(1)


def announce() -> None:
    """The one warning line for TTHFV_LIMITS=none, once per run, from the process the user started."""
    global _warned
    if mode() == "none" and not _warned:
        _warned = True
        print(
            f"WARNING: {ENV_MODE}=none: no limit on the memory, processes or CPU a design may use",
            file=sys.stderr,
            flush=True,
        )


def new_unit() -> str:
    return f"{UNIT_PREFIX}{os.getpid()}-{next(_counter)}"


def scope_argv(argv: list[str], limits: Limits, unit: str, runtime_max: float, fd: int) -> list[str]:
    """The command line that runs argv inside a limited scope named <unit>.scope, whose wrapper reports on fd."""
    return [
        SYSTEMD_RUN, "--user", "--scope", "--quiet", "--collect", "--unit", unit,
        *_properties(limits), "-p", f"RuntimeMaxSec={int(runtime_max) + RUNTIME_SLACK}",
        "--", "/bin/bash", "-c", WRAPPER.format(fd=fd), "bash", *argv,
    ]  # fmt: skip


def stop_unit(unit: str) -> None:
    """Best effort: stop <unit>.scope so that nothing in it outlives a timeout."""
    try:
        subprocess.run(
            [SYSTEMCTL, "--user", "stop", f"{unit}.scope"], check=False, capture_output=True,
            timeout=15, stdin=subprocess.DEVNULL, env=host_env(),
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        pass


def parse_counters(text: str) -> dict[str, int]:
    """The wrapper's lines ("oom_kill 1") as numbers; a missing or garbled value is absent."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])  # a later line replaces an earlier one
    return out


def limited_by(counters: dict[str, int]) -> Limited | None:
    if counters.get("oom_kill", 0) > 0:
        return "memory"
    if counters.get("tasks_max", 0) > 0:
        return "tasks"
    return None

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

import atexit
import contextlib
import itertools
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from tthfv.errors import UserError

SYSTEMD_RUN = shutil.which("systemd-run") or "systemd-run"
SYSTEMCTL = shutil.which("systemctl") or "systemctl"
SETPRIV = "/usr/bin/setpriv"  # util-linux; starts the scope's wrapper with a death signal (see scope_argv)
ENV_MODE = "TTHFV_LIMITS"
DEFAULT_MEMORY = "8G"
DEFAULT_TASKS = 512
DEFAULT_CPU = "400%"
RUNTIME_SLACK = 30  # seconds added to a tool's timeout for RuntimeMaxSec, the backstop
PROBE_TIMEOUT = 30
UNIT_PREFIX = "tthfv-"

Limited = Literal["memory", "tasks", "time", "disk", "unknown"]

_MEMORY = re.compile(r"^([1-9]\d*)([KMGT]?)$")
_CPU = re.compile(r"^([1-9]\d*)%$")
_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}

# Runs the command given as arguments (with the pipe's descriptor closed for it), then reports the scope's
# counters on that descriptor. Everything here is trusted host code; it runs outside the jail. The last value
# of a name in the report is the one that counts, and only this script writes after the jail has ended.
# The first line closes the race between the fork and setpriv's prctl: if the process that started this scope has
# already died, the wrapper has been reparented and its death signal can no longer come, so it must not start the tool.
# The tool is started with oom_score_adj 1000, which anyone may raise for themselves, so that when the scope
# runs out of memory the kernel prefers it to this wrapper and its guard (which keep their inherited, lower, score).
# A tool inside the jail CAN lower its own score again, and its parent's inside the jail, but only to the minimum
# it inherited (lowering further needs CAP_SYS_RESOURCE); what makes this safe is that the outer bwrap, the
# process the kernel then looks at first, stays at 1000 and cannot be reached from inside the jail.
#
# The guard: bubblewrap's --die-with-parent has a start-up window (the first ~5-40 ms) in which a jail survives the
# death of its parent, so the death of the worker is also watched here, inside the scope's own cgroup, before the jail
# exists. {gfd} is the read end of a pipe whose only write end the worker holds (it is not inherited by anything
# else, and not passed to the tool); the kernel closes it however the worker dies, the guard's `read` (a shell
# builtin: no fork, so a tight task limit cannot stop it) then sees EOF, and the guard writes 1 to the scope's
# cgroup.kill, which kills every process in the scope at once (the guard too), or, where that file cannot be written,
# has systemd kill the unit. No pid is involved, so a recycled pid cannot fool it. The guard is in its own session
# (setsid) so that the wrapper's death signal and the group kill on a timeout do not reach it first; when the tool
# ends by itself the wrapper stops it after the counters are reported, and the kill is done only after EOF, never on
# that signal. It is detached from every pipe it inherited: its stdin is the lifeline, its stdout and stderr are
# /dev/null (its stderr would otherwise be the tool's output pipe, whose reader is dead when it matters, and an
# error message on it would kill the guard with SIGPIPE before the fallback runs), and the counters descriptor is
# closed, so a guard that outlives the wrapper by a moment cannot hold up the worker's EOF on either pipe. If the
# descriptor is not open the redirection fails and the guard does nothing (it never kills a healthy tool).
WRAPPER = r"""
[ "$PPID" = "{ppid}" ] || exit 99
cg=/sys/fs/cgroup$(sed -n 's/^0:://p' /proc/self/cgroup)
setsid bash -c 'while read -r _; do :; done; echo 1 > "$0/cgroup.kill" || systemctl --user kill --signal=SIGKILL "$1"' "{guard_cg}" "{unit}.scope" <&{gfd} {fd}>&- {gfd}<&- >/dev/null 2>&1 &
guard=$!
( echo 1000 > /proc/self/oom_score_adj; exec "$@" ) {fd}>&- {gfd}<&-
rc=$?
{{
  echo "oom_kill $(sed -n 's/^oom_kill //p' "$cg/memory.events")"
  echo "tasks_max $(sed -n 's/^max //p' "$cg/pids.events")"
  echo "memory_peak $(cat "$cg/memory.peak")"
  echo "tasks_peak $(cat "$cg/pids.peak")"
  echo "cpu_usec $(sed -n 's/^usage_usec //p' "$cg/cpu.stat")"
}} >&{fd}
kill -- -$guard
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
            "unknown": "resource counters missing: the limit wrapper was killed, most likely by the memory limit",
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
        SETPRIV,
        "--pdeathsig",
        "KILL",
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
        return (
            done.stderr.strip()
            or f"systemd-run (or {SETPRIV}, from util-linux) exited with {done.returncode}"
        )
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
# The process that owns this run's scopes. Every tool scope of a run is put in one slice named after it, so that
# the whole run can be stopped with one `systemctl --user stop` (a worker that finds the run's parent dead does
# that). In a worker process it is set to the parent's pid by the pool's initializer.
RUN_PID = os.getpid()


def start_time(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat (the process's start time in clock ticks, which tells a pid from its reuse), or
    None if the process is gone or a zombie."""
    try:
        rest = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return None if rest[0] == "Z" else rest[19]


RUN_START = start_time(os.getpid())  # likewise set to the owner's by a worker's initializer


def run_slice() -> str:
    """tthfv_run_<pid>_<start time>.slice: the owner's pid and start time are in the name, so a later run can tell
    exactly whether the owner is still the same living process. Underscores: a hyphen in a slice name would mean a
    parent slice."""
    return f"tthfv_run_{RUN_PID}_{RUN_START}.slice"


_SLICE = re.compile(r"^tthfv_run_(\d+)_(\d+)\.slice$")


def sweep_stale_slices() -> list[str]:
    """Stop the run slices (and with them any scope left in them) whose owner is gone: its pid is dead, or alive
    with another start time. A slice whose owner is alive is never touched, nor is one whose name does not carry
    a start time (older versions). Returns the names stopped. Best effort."""
    try:
        done = subprocess.run(
            [SYSTEMCTL, "--user", "list-units", "tthfv_run_*.slice", "--all", "--no-legend", "--plain"],
            check=False, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, env=host_env(),
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return []
    stopped = []
    for word in done.stdout.split():
        m = _SLICE.match(word)
        if m and start_time(int(m.group(1))) != m.group(2):
            stop_slice(word)
            stopped.append(word)
    return stopped


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


_slice_cleanup = False


def stop_slice_at_exit() -> None:
    """In the process that owns the run's slice, arrange for the slice to be stopped when the process exits: a slice
    that systemd made implicitly stays loaded (empty) otherwise. The workers of a pool make the scopes, so the run's
    own process calls this when it starts a run; a process that makes scopes itself gets it from new_unit()."""
    global _slice_cleanup
    if not _slice_cleanup and os.getpid() == RUN_PID:
        _slice_cleanup = True
        atexit.register(stop_slice)


def new_unit() -> str:
    """A unique scope name."""
    stop_slice_at_exit()
    return f"{UNIT_PREFIX}{os.getpid()}-{next(_counter)}"


def scope_argv(
    argv: list[str],
    limits: Limits,
    unit: str,
    runtime_max: float,
    fd: int,
    guard_fd: int,
    parent_pid: int | None = None,
    guard_cgroup: str | None = None,
) -> list[str]:
    """The command line that runs argv inside a limited scope named <unit>.scope, whose wrapper reports on fd and
    whose guard waits for EOF on guard_fd (see WRAPPER). guard_cgroup is a seam for the test of the guard's
    fallback: the cgroup directory the guard writes cgroup.kill in (default: the scope's own, `$cg` in WRAPPER)."""
    return [
        SYSTEMD_RUN, "--user", "--scope", "--quiet", "--collect", "--unit", unit, "--slice", run_slice(),
        *_properties(limits), "-p", f"RuntimeMaxSec={int(runtime_max) + RUNTIME_SLACK}",
        # setpriv gives the wrapper a SIGKILL for when its parent (the process that started this program, in the
        # same thread) dies, and keeps it across the exec of bash (it is cleared only by a setuid exec or a change
        # of credentials): then bwrap, whose parent the wrapper is, loses its own parent and --die-with-parent
        # takes the jail down, except in bwrap's first milliseconds, which the guard of WRAPPER covers: nothing outlives
        # a worker or a standalone command killed with SIGKILL.
        "--", SETPRIV, "--pdeathsig", "KILL", "--", "/bin/bash", "-c", WRAPPER.format(fd=fd, gfd=guard_fd, unit=unit, guard_cg=guard_cgroup or "$cg", ppid=os.getpid() if parent_pid is None else parent_pid), "bash", *argv,
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


class Signalled(KeyboardInterrupt):
    """SIGHUP or SIGTERM arrived. A KeyboardInterrupt, so everything that unwinds for Ctrl-C (killing the tool's
    group, stopping its scope, killing the pool) does the same for a closed terminal or a `kill`."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


SIGNALLED: list[int] = []  # the signals this process turned into Signalled, the last one last


@contextlib.contextmanager
def terminate_on_signals():
    """Within the block SIGHUP and SIGTERM raise Signalled in the main thread; on the way out the run's slice is
    stopped (when this process owns it) so that no scope outlives the command."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame):
        SIGNALLED.append(signum)
        raise Signalled(signum)

    old = {s: signal.signal(s, handler) for s in (signal.SIGHUP, signal.SIGTERM)}
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        if os.getpid() == RUN_PID and _slice_cleanup:
            stop_slice()


def exit_code_for_signal() -> int | None:
    return 128 + SIGNALLED[-1] if SIGNALLED else None


def stop_slice(name: str | None = None) -> None:
    """Best effort: stop the run's slice, which stops every tool scope in it."""
    try:
        subprocess.run(
            [SYSTEMCTL, "--user", "stop", name or run_slice()], check=False, capture_output=True,
            timeout=30, stdin=subprocess.DEVNULL, env=host_env(),
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


def cgroup_of(pid: int, unit: str) -> Path | None:
    """The cgroup directory of the scope <unit>.scope that process pid is in, or None (not in it yet, or gone)."""
    try:
        line = Path(f"/proc/{pid}/cgroup").read_text().strip().splitlines()[-1]
    except (OSError, IndexError):
        return None
    where = line.split("::", 1)[-1]
    if not where.endswith(f"/{unit}.scope"):
        return None
    return Path("/sys/fs/cgroup") / where.lstrip("/")


def read_cgroup(path: Path) -> dict[str, int]:
    """The counters of a scope's cgroup as the wrapper reports them (same names), read from the host. Empty if the
    cgroup is gone. Files of a removed cgroup cannot be read through descriptors opened earlier (ENODEV), which is
    why the wrapper keeps the cgroup alive until it has reported, and this is used while it still exists."""
    out: dict[str, int] = {}

    def value(name: str, key: str | None) -> int | None:
        try:
            text = (path / name).read_text()
        except OSError:
            return None
        if key is None:
            return int(text) if text.strip().isdigit() else None
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == key and parts[1].isdigit():
                return int(parts[1])
        return None

    for name, file, key in (
        ("oom_kill", "memory.events", "oom_kill"),
        ("tasks_max", "pids.events", "max"),
        ("memory_peak", "memory.peak", None),
        ("tasks_peak", "pids.peak", None),
        ("cpu_usec", "cpu.stat", "usage_usec"),
    ):
        got = value(file, key)
        if got is not None:
            out[name] = got
    return out


def limited_by(counters: dict[str, int]) -> Limited | None:
    if counters.get("oom_kill", 0) > 0:
        return "memory"
    if counters.get("tasks_max", 0) > 0:
        return "tasks"
    return None

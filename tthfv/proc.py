# SPDX-License-Identifier: Apache-2.0
"""Run external programs the one way this project does: in their own process group, with a timeout,
and with the whole group killed on a timeout (yosys starts yosys-abc, verilator starts make and a
compiler, git starts helpers). Every stage module goes through here."""

import collections
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import IO, NamedTuple

from tthfv import limits as limits_mod
from tthfv import paths as paths_mod
from tthfv import safefile
from tthfv.sandbox import Sandbox

TIMEOUT_RC = 124  # the exit code reported for a program that was killed on its timeout
GIT_TIMEOUT = 300  # seconds, for each git command
LOG_CAP = 1 << 20  # bytes of a program's output kept in full by a capped log
LOG_TAIL = 64 << 10  # bytes of the end of the output kept after the cap, so a final message survives


class Logged(NamedTuple):
    rc: int
    text: str  # the output as written to the log (capped output includes the marker and the tail)
    limited: str | None = None  # "memory", "tasks" or "time": a resource limit ended or cut short the program
    peak_memory: int | None = None  # bytes, from the scope's cgroup (None without limits)
    peak_tasks: int | None = None
    cpu_usec: int | None = None  # CPU time used by everything in the scope
    timed_out: bool = False  # the program was killed on its timeout (an exit status of 124 is just a status)
    detail: str = ""  # for limited "disk" or "disk-free": what the watchdog saw
    free_space_rule: str = (
        ""  # "on", or "off: <why>" if the free space could not be read; "" if nothing was watched
    )
    watchdog: tuple[int, float] | None = None  # (checks made, seconds they took) of the disk watchdog


def _kill_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class _Scope:
    """The transient systemd scope one limited program runs in (see tthfv.limits): its unit name, the pipe on which
    the scope's wrapper reports the cgroup's counters when the program has ended, and a poller that reads the same
    counters from the host while the cgroup exists, so that they are known even if the wrapper is killed."""

    POLL = 0.2  # seconds between host-side reads

    def __init__(self, limits: limits_mod.Limits, timeout: float):
        self.limits, self.timeout = limits, timeout
        self.unit = limits_mod.new_unit()
        self.rfd, self.wfd = os.pipe()
        # The guard's lifeline: this process holds the only write end (gw); the scope's guard reads gr and, when gw is
        # closed, which the kernel does however this process dies, kills everything in the scope (tthfv.limits.WRAPPER).
        self.gr, self.gw = os.pipe()
        self.started = time.monotonic()
        self.runtime_max = timeout + limits_mod.RUNTIME_SLACK
        self.last: dict[str, int] = {}  # the latest counters read from the host side
        self.pid = 0
        self._cgroup: Path | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def argv(self, argv: list[str]) -> list[str]:
        return limits_mod.scope_argv(argv, self.limits, self.unit, self.timeout, self.wfd, self.gr)

    @property
    def fds(self) -> tuple[int, int]:
        """The descriptors the scope's command line needs open in the child (Popen's pass_fds)."""
        return (self.wfd, self.gr)

    def started_child(self, pid: int) -> None:
        os.close(self.wfd)  # the child has its own copy; EOF on rfd means the wrapper (and everyone) is done
        self.wfd = -1
        os.close(self.gr)  # likewise: the guard has the read end, only this process the write end
        self.gr = -1
        os.set_blocking(self.rfd, False)
        self.pid = pid
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def snapshot(self) -> None:
        """Read the counters from the cgroup now, if it exists."""
        if self._cgroup is None:
            self._cgroup = limits_mod.cgroup_of(self.pid, self.unit)
        if self._cgroup is not None and (got := limits_mod.read_cgroup(self._cgroup)):
            self.last = got

    def _poll(self) -> None:
        while not self._stop.wait(0.05 if self._cgroup is None else self.POLL):
            self.snapshot()

    def finish(
        self, rc: int, timed_out: bool, killed_by_us: bool = False
    ) -> tuple[str | None, dict[str, int]]:
        """(which limit was hit, the counters), after the program has ended. On a timeout the unit is also stopped
        by name, so that nothing in it can outlive the program. The wrapper's report is preferred; if it never
        came (it was killed) the host's last reading is used, and if that does not show a limit either, the outcome
        is "unknown": something killed the wrapper, most likely the memory limit."""
        if timed_out:
            limits_mod.stop_unit(self.unit)
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        chunks = []
        try:
            while data := os.read(self.rfd, 4096):
                chunks.append(data)
        except (BlockingIOError, OSError):
            pass
        self.close()
        reported = limits_mod.parse_counters(b"".join(chunks).decode(errors="replace"))
        counters = reported or self.last
        limited = limits_mod.limited_by(counters)  # a memory hit before a timeout is still a memory hit
        if limited is None:
            if timed_out or (rc < 0 and time.monotonic() - self.started >= self.runtime_max - 2):
                limited = (
                    "time"  # our own timeout, or systemd's RuntimeMaxSec backstop (a signal ends the scope)
                )
            elif not reported and not killed_by_us:
                limited = "unknown"
        return limited, counters

    def release(self) -> None:
        """Close the guard's lifeline: the program has ended, so whatever is still in its scope goes (a no-op when the
        wrapper has already stopped the guard, as it does when the program ends by itself)."""
        if self.gw >= 0:
            os.close(self.gw)
            self.gw = -1

    def close(self) -> None:
        self.release()
        for name in ("rfd", "wfd", "gr"):
            fd = getattr(self, name)
            if fd >= 0:
                os.close(fd)
                setattr(self, name, -1)


def _spawn(
    args: list[str], cwd, env, stdout, stderr, sandbox: Sandbox | None = None, timeout: float = 0.0
) -> tuple[subprocess.Popen, _Scope | None]:
    """Start a program in its own session (so its process group can be killed). With a sandbox the
    program runs inside bwrap with exactly the environment the sandbox sets (env is not used), and
    killing bwrap's group, whose own namespace dies with it, kills everything in the sandbox. If the sandbox
    has limits, bwrap runs in a transient systemd scope that enforces them."""
    scope = None
    pass_fds: tuple[int, ...] = ()
    if sandbox is not None:
        if cwd is None:
            raise ValueError("a sandboxed program needs a working directory")
        args, env, cwd = sandbox.wrap(args, Path(cwd)), {}, None
        if sandbox.limits is not None:
            scope = _Scope(sandbox.limits, timeout)
            args, env, pass_fds = scope.argv(args), limits_mod.host_env(), scope.fds
    try:
        p = subprocess.Popen(
            args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            start_new_session=True, pass_fds=pass_fds,
        )  # fmt: skip
    except BaseException:
        if scope:
            scope.close()
        raise
    if scope:
        scope.started_child(p.pid)
    return p, scope


def _wait(p: subprocess.Popen, timeout: float, scope: _Scope | None = None) -> tuple[int, bool]:
    """Wait for p. Returns (exit code, whether it was killed on the timeout; the code is then TIMEOUT_RC). On a
    timeout, or any exception such as Ctrl-C, the whole process group is killed (and the scope stopped by name);
    on a timeout the scope's counters are read first, while the cgroup still exists."""
    try:
        return p.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        if scope:
            scope.snapshot()
        _kill_group(p)
        p.wait()
        return TIMEOUT_RC, True
    except BaseException:
        _kill_group(p)
        if scope:
            limits_mod.stop_unit(scope.unit)
            scope._stop.set()
            scope.close()
        p.wait()
        raise


def free_bytes(path) -> int | None:
    """Bytes available to an ordinary user on the filesystem of path, or None if it cannot be asked. The watchdog
    reads this, not tthfv.paths.free_bytes, so tests that fake the disk for the stage guards do not blind it."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


class _Watchdog(threading.Thread):
    """While a program runs, keeps three eyes on the disk and stops the program (its process group and scope) at
    once when one of them sees too much:

    - the free space of the filesystem of `fs_path`, every TICK seconds, whatever the walk is doing: it must not have
      fallen by more than the scratch cap plus FREE_MARGIN since the program started (this is what sees data the walk
      cannot: a file unlinked while it is still open and written; other jobs writing to the same filesystem count
      too, which is the conservative direction), and must not be below half of TTHFV_MIN_FREE;
    - `check` (returns "" or a reason; it walks the scratch directory), run in a thread of its own so that a slow
      walk never delays the free-space reading; the next walk starts INTERVAL seconds after the last one ended
      (four times its duration if it was slow, at most MAX_INTERVAL);
    - the walks themselves: one that raises (the tree kept changing) is not a verdict, but MAX_BLIND of them in a row
      are: "scratch could not be measured"."""

    TICK = 1.0
    INTERVAL = 2.0
    SLOW = 0.5
    MAX_INTERVAL = 4.0  # however slow a walk is, the next one starts at most this long after it ends
    FREE_MARGIN = 1 << 30
    MAX_BLIND = 3

    def __init__(self, check, p: subprocess.Popen, scope: _Scope | None, fs_path=None):
        super().__init__(daemon=True)
        self.check, self.p, self.scope, self.fs_path = check, p, scope, fs_path
        self.reason = ""
        self.kind = (
            "disk"  # "disk": the scratch walk's rules; "disk-free": the free-space rule (see _free_rule)
        )
        self.free_space_rule = ""
        self.walks = 0
        self.seconds = 0.0
        self.blind = 0
        self.base_free = free_bytes(fs_path) if fs_path is not None else None
        if fs_path is not None:
            self.free_space_rule = (
                "on" if self.base_free is not None else self._rule_off("statvfs failed at the start")
            )
        self._halt = threading.Event()
        self._walker: threading.Thread | None = None
        self._outcome: list = []  # [(reason or "", error or None, seconds)] from the walker, taken by the tick
        self._next_walk = time.monotonic() + self.INTERVAL

    def _rule_off(self, why: str) -> str:
        """The free-space rule cannot run: say so once, on stderr and in the record; never kill because of it."""
        note = f"off: {why}"
        if not self.free_space_rule.startswith("off"):
            print(
                f"WARNING: the free-space check of the disk watchdog is {note}", file=sys.stderr, flush=True
            )
        return note

    def stop(self) -> None:
        self._halt.set()
        self.join(5)

    def _walk(self) -> None:
        started = time.monotonic()
        try:
            result = (self.check(), None)
        except Exception as e:  # noqa: BLE001 - what the walk could not do is counted, never swallowed
            result = ("", e)
        self._outcome.append((*result, time.monotonic() - started))

    def _free_rule(self) -> str:
        if self.base_free is None:
            return ""
        free = free_bytes(self.fs_path)
        if free is None:
            self.free_space_rule = self._rule_off("statvfs failed during the run")
            return ""
        cap = safefile.MAX_SCRATCH
        fell = self.base_free - free
        if fell > cap + self.FREE_MARGIN:
            return (
                f"free space on the work filesystem fell by {fell >> 20} MiB while the tool ran, more than the "
                f"scratch cap ({cap >> 20} MiB) plus {self.FREE_MARGIN >> 20} MiB: this tool's own invisible data, "
                "or other processes writing to the same filesystem; the result is not cached"
            )
        try:
            floor = paths_mod.min_free() // 2
        except Exception:  # noqa: BLE001 - a bad TTHFV_MIN_FREE is reported by the stage guard
            floor = paths_mod.MIN_FREE_DEFAULT // 2
        if free < floor:
            return (
                f"free space on the work filesystem is down to {free >> 20} MiB, below the floor of {floor >> 20} MiB "
                "(TTHFV_MIN_FREE / 2), whoever is filling it; the result is not cached"
            )
        return ""

    def _harvest(self) -> str:
        if self._walker is None or self._walker.is_alive() or not self._outcome:
            return ""
        why, error, took = self._outcome.pop()
        self._walker = None
        self.walks += 1
        self.seconds += took
        interval = min(self.MAX_INTERVAL, max(self.INTERVAL, 4 * took)) if took > self.SLOW else self.INTERVAL
        self._next_walk = time.monotonic() + interval
        if error is not None:
            self.blind += 1
            if self.blind >= self.MAX_BLIND:
                return "scratch could not be measured (the tree kept changing)"
            return ""
        self.blind = 0
        return why

    def run(self) -> None:
        while not self._halt.wait(self.TICK):
            why = self._free_rule()
            if why:
                self.kind = "disk-free"
            else:
                why = self._harvest()
            if not why and self._walker is None and time.monotonic() >= self._next_walk:
                self._walker = threading.Thread(target=self._walk, daemon=True)
                self._walker.start()
            if why:
                self.reason = why
                if self.scope:
                    self.scope.snapshot()
                _kill_group(self.p)
                if self.scope:
                    limits_mod.stop_unit(self.scope.unit)
                return


def run(
    args: list[str],
    timeout: float,
    *,
    cwd=None,
    env=None,
    stdout=None,
    stderr=None,
    sandbox: Sandbox | None = None,
) -> int:
    """Run a program and return its exit code, or TIMEOUT_RC if it was killed on the timeout.
    stdout and stderr are as for subprocess (a file object, DEVNULL, ...). A program that cannot be
    started raises OSError. With a sandbox the program runs inside it (see tthfv.sandbox)."""
    p, scope = _spawn(args, cwd, env, stdout, stderr, sandbox, timeout)
    rc, timed_out = _wait(p, timeout, scope)
    if scope:
        scope.finish(rc, timed_out)
    return rc


def capture(args: list[str], timeout: float, *, cwd=None, env=None, sandbox: Sandbox | None = None) -> Logged:
    """Run a program with stdout and stderr together captured; the text is at most the last LOG_CAP bytes."""
    with tempfile.TemporaryFile() as f:
        rc = run(args, timeout, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT, sandbox=sandbox)
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - LOG_CAP))
        return Logged(rc, f.read().decode(errors="replace"))


class _Sink:
    """Copies a stream to a log file: with the default cap the first `cap` bytes, a marker and the last LOG_TAIL
    bytes; with cap=None all of it. Memory is bounded by the cap and the tail either way (cap=None is for output
    that is known to be small).

    watch: phrases (matched without regard to case) whose lines a parser may need even if they fall in the part
    that is left out: the first few lines containing each are kept and written after the marker. They are found
    while the stream goes by, so the part left out is never held in memory."""

    MATCHES_PER_PHRASE = 10
    MATCH_LINE = 1024
    CARRY = 4096  # the unfinished last line of a read, looked at together with the next read

    def __init__(self, out: IO[bytes], cap: int | None, watch: tuple[str, ...] = ()):
        self.out, self.cap = out, cap
        self.head: list[bytes] = []
        self.kept = 0
        self.tail: collections.deque[bytes] = collections.deque()
        self.tail_size = 0
        self.dropped = 0
        self.watch = [w.lower().encode() for w in watch]
        self.matches: dict[bytes, list[bytes]] = {w: [] for w in self.watch}
        self.carry = b""

    def drain(self, stream: IO[bytes]) -> None:
        while chunk := stream.read1(1 << 16):
            self._add(chunk)

    def _add(self, chunk: bytes) -> None:
        if self.cap is None or self.kept < self.cap:
            take = chunk if self.cap is None else chunk[: self.cap - self.kept]
            self.out.write(take)
            self.head.append(take)
            self.kept += len(take)
            chunk = chunk[len(take) :]
        if chunk:
            if self.watch:
                self._scan(chunk)
            self.tail.append(chunk)
            self.tail_size += len(chunk)
            while self.tail and self.tail_size - len(self.tail[0]) >= LOG_TAIL:
                n = len(self.tail.popleft())
                self.dropped += n
                self.tail_size -= n

    def _scan(self, chunk: bytes, final: bool = False) -> None:
        """Look for the watched phrases in the complete lines of carry + chunk; keep the unfinished last line."""
        data = self.carry + chunk
        cut = len(data) if final else data.rfind(b"\n") + 1
        self.carry = b"" if final else data[cut:][-self.CARRY :]
        region = data[:cut]
        low = region.lower()
        for phrase in self.watch:
            found = self.matches[phrase]
            idx = low.find(phrase)
            while idx != -1 and len(found) < self.MATCHES_PER_PHRASE:
                start = low.rfind(b"\n", 0, idx) + 1
                end = low.find(b"\n", idx)
                end = len(region) if end == -1 else end
                found.append(region[start:end][: self.MATCH_LINE])
                idx = low.find(phrase, end)

    def finish(self) -> str:
        if self.watch:
            self._scan(b"", final=True)
        parts = list(self.head)
        if self.tail:
            tail = b"".join(self.tail)
            if len(tail) > LOG_TAIL:
                self.dropped += len(tail) - LOG_TAIL
                tail = tail[-LOG_TAIL:]
            marker = f"\n[output truncated: {self.dropped} bytes omitted]\n".encode()
            lines = [ln for found in self.matches.values() for ln in found]
            kept_lines = b""
            if lines and self.dropped:
                kept_lines = b"[lines of the omitted part that matter:]\n" + b"\n".join(lines) + b"\n[end]\n"
            parts += [marker, kept_lines, tail]
            self.out.write(marker + kept_lines + tail)
        self.out.flush()
        return b"".join(parts).decode(errors="replace")


def run_logged(
    args: list[str],
    timeout: float,
    log: Path,
    *,
    cwd=None,
    env=None,
    append: bool = True,
    header: bool = True,
    cap: int | None = LOG_CAP,
    watch: tuple[str, ...] = (),
    sandbox: Sandbox | None = None,
    scratch_check=None,
    scratch_dir=None,
) -> Logged:
    """Run a program writing its output to log (appended, or replacing it), after a "$ command" line if
    header. A program that talks more than `cap` bytes (default LOG_CAP; None for no limit, only for output
    known to be small) leaves its first `cap` bytes, a truncation marker, the lines containing any `watch`
    phrase from the omitted part, and its last LOG_TAIL bytes; neither the log nor memory grows with the output. On a timeout the group is killed and the log ends with "timed out after Ns".
    With a sandbox the program runs inside it; the header still shows the program's own command line.
    scratch_check: a function returning "" or a reason, called every few seconds while the program runs; the first
    reason stops the program and the result says limited == "disk" (see _Watchdog). scratch_dir: where the program
    writes; the free space of its filesystem is watched as well.
    Logged.timed_out says whether the program was killed on its timeout; its exit status may be anything."""
    with safefile.open_log(log, append) as f:
        if header:
            f.write(f"$ {' '.join(args)}\n".encode())
            f.flush()
        p, scope = _spawn(args, cwd, env, subprocess.PIPE, subprocess.STDOUT, sandbox, timeout)
        sink = _Sink(f, cap, watch)
        reader = threading.Thread(target=sink.drain, args=(p.stdout,), daemon=True)
        reader.start()
        dog = _Watchdog(scratch_check, p, scope, scratch_dir) if scratch_check else None
        if dog:
            dog.start()
        try:
            rc, timed_out = _wait(p, timeout, scope)
        finally:
            if scope:
                scope.release()  # the program is over: a guard still waiting in its scope ends what is left of it
            if dog:
                dog.stop()
            reader.join(5)
            if reader.is_alive():  # a leftover grandchild still holds the pipe: it is not wanted
                _kill_group(p)
                reader.join(5)
        text = sink.finish()
        disk = dog.reason if dog else ""
        watched = (dog.walks, dog.seconds) if dog else None
        timed_out = timed_out and not disk
        if timed_out:
            f.write(f"timed out after {timeout}s\n".encode())
        limited, counters = None, {}
        if scope is not None:
            limited, counters = scope.finish(rc, timed_out, killed_by_us=bool(disk))
            if limited in ("memory", "tasks", "unknown"):
                f.write(f"[tthfv] {sandbox.limits.reason(limited)}\n".encode())  # type: ignore[union-attr]
        if disk:
            limited = dog.kind  # type: ignore[union-attr]
            f.write(f"[tthfv] {disk}\n".encode())
        return Logged(
            rc, text, limited, counters.get("memory_peak"), counters.get("tasks_peak"), counters.get("cpu_usec"),
            timed_out, disk, dog.free_space_rule if dog else "", watched,
        )  # fmt: skip

# SPDX-License-Identifier: Apache-2.0
"""Run external programs the one way this project does: in their own process group, with a timeout,
and with the whole group killed on a timeout (yosys starts yosys-abc, verilator starts make and a
compiler, git starts helpers). Every stage module goes through here."""

import collections
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import IO, NamedTuple

from tthfv import limits as limits_mod
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


def _kill_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class _Scope:
    """The transient systemd scope one limited program runs in (see tthfv.limits): its unit name and the pipe
    on which the scope's wrapper reports the cgroup's counters when the program has ended."""

    def __init__(self, limits: limits_mod.Limits, timeout: float):
        self.limits, self.timeout = limits, timeout
        self.unit = limits_mod.new_unit()
        self.rfd, self.wfd = os.pipe()
        self.started = time.monotonic()
        self.runtime_max = timeout + limits_mod.RUNTIME_SLACK

    def argv(self, argv: list[str]) -> list[str]:
        return limits_mod.scope_argv(argv, self.limits, self.unit, self.timeout, self.wfd)

    def started_child(self) -> None:
        os.close(self.wfd)  # the child has its own copy; EOF on rfd means the wrapper (and everyone) is done
        self.wfd = -1
        os.set_blocking(self.rfd, False)

    def finish(self, rc: int, timed_out: bool) -> tuple[str | None, dict[str, int]]:
        """(which limit was hit, the counters), after the program has ended. On a timeout the unit is also stopped
        by name, so that nothing in it can outlive the program."""
        if timed_out:
            limits_mod.stop_unit(self.unit)
        chunks = []
        try:
            while data := os.read(self.rfd, 4096):
                chunks.append(data)
        except (BlockingIOError, OSError):
            pass
        self.close()
        counters = limits_mod.parse_counters(b"".join(chunks).decode(errors="replace"))
        limited = limits_mod.limited_by(counters)
        if limited is None and (
            timed_out or (rc < 0 and time.monotonic() - self.started >= self.runtime_max - 2)
        ):
            limited = "time"  # our own timeout, or systemd's RuntimeMaxSec backstop (it ends the scope with a signal)
        return limited, counters

    def close(self) -> None:
        for name in ("rfd", "wfd"):
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
            args, env, pass_fds = scope.argv(args), limits_mod.host_env(), (scope.wfd,)
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
        scope.started_child()
    return p, scope


def _wait(p: subprocess.Popen, timeout: float, scope: _Scope | None = None) -> int:
    """Wait for p; on a timeout, or any exception such as Ctrl-C, kill its whole process group (and stop its
    scope by name)."""
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(p)
        p.wait()
        return TIMEOUT_RC
    except BaseException:
        _kill_group(p)
        if scope:
            limits_mod.stop_unit(scope.unit)
            scope.close()
        p.wait()
        raise


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
    rc = _wait(p, timeout, scope)
    if scope:
        scope.finish(rc, rc == TIMEOUT_RC)
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
) -> Logged:
    """Run a program writing its output to log (appended, or replacing it), after a "$ command" line if
    header. A program that talks more than `cap` bytes (default LOG_CAP; None for no limit, only for output
    known to be small) leaves its first `cap` bytes, a truncation marker, the lines containing any `watch`
    phrase from the omitted part, and its last LOG_TAIL bytes; neither the log nor memory grows with the output. On a timeout the group is killed and the log ends with "timed out after Ns".
    With a sandbox the program runs inside it; the header still shows the program's own command line."""
    with safefile.open_log(log, append) as f:
        if header:
            f.write(f"$ {' '.join(args)}\n".encode())
            f.flush()
        p, scope = _spawn(args, cwd, env, subprocess.PIPE, subprocess.STDOUT, sandbox, timeout)
        sink = _Sink(f, cap, watch)
        reader = threading.Thread(target=sink.drain, args=(p.stdout,), daemon=True)
        reader.start()
        try:
            rc = _wait(p, timeout, scope)
        finally:
            reader.join(5)
            if reader.is_alive():  # a leftover grandchild still holds the pipe: it is not wanted
                _kill_group(p)
                reader.join(5)
        text = sink.finish()
        if rc == TIMEOUT_RC:
            f.write(f"timed out after {timeout}s\n".encode())
        if scope is None:
            return Logged(rc, text)
        limited, counters = scope.finish(rc, rc == TIMEOUT_RC)
        if limited in ("memory", "tasks"):
            f.write(f"[tthfv] {sandbox.limits.reason(limited)}\n".encode())  # type: ignore[union-attr]
        return Logged(
            rc,
            text,
            limited,
            counters.get("memory_peak"),
            counters.get("tasks_peak"),
            counters.get("cpu_usec"),
        )

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
from pathlib import Path
from typing import IO, NamedTuple

from tthfv import safefile
from tthfv.sandbox import Sandbox

TIMEOUT_RC = 124  # the exit code reported for a program that was killed on its timeout
GIT_TIMEOUT = 300  # seconds, for each git command
LOG_CAP = 1 << 20  # bytes of a program's output kept in full by a capped log
LOG_TAIL = 16 << 10  # bytes of the end of the output kept after the cap, so a final message survives


class Logged(NamedTuple):
    rc: int
    text: str  # the output as written to the log (capped output includes the marker and the tail)


def _kill_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _spawn(args: list[str], cwd, env, stdout, stderr, sandbox: Sandbox | None = None) -> subprocess.Popen:
    """Start a program in its own session (so its process group can be killed). With a sandbox the
    program runs inside bwrap with exactly the environment the sandbox sets (env is not used), and
    killing bwrap's group, whose own namespace dies with it, kills everything in the sandbox."""
    if sandbox is not None:
        if cwd is None:
            raise ValueError("a sandboxed program needs a working directory")
        args, env, cwd = sandbox.wrap(args, Path(cwd)), {}, None
    return subprocess.Popen(
        args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True
    )


def _wait(p: subprocess.Popen, timeout: float) -> int:
    """Wait for p; on a timeout, or any exception such as Ctrl-C, kill its whole process group."""
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(p)
        p.wait()
        return TIMEOUT_RC
    except BaseException:
        _kill_group(p)
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
    return _wait(_spawn(args, cwd, env, stdout, stderr, sandbox), timeout)


def capture(args: list[str], timeout: float, *, cwd=None, env=None, sandbox: Sandbox | None = None) -> Logged:
    """Run a program with stdout and stderr together captured; the text is at most the last LOG_CAP bytes."""
    with tempfile.TemporaryFile() as f:
        rc = run(args, timeout, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT, sandbox=sandbox)
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - LOG_CAP))
        return Logged(rc, f.read().decode(errors="replace"))


class _Sink:
    """Copies a stream to a log file: all of it, or with a cap the first `cap` bytes, a marker and the
    last LOG_TAIL bytes. Keeps what it wrote in memory, which the cap bounds."""

    def __init__(self, out: IO[bytes], cap: int | None):
        self.out, self.cap = out, cap
        self.head: list[bytes] = []
        self.kept = 0
        self.tail: collections.deque[bytes] = collections.deque()
        self.tail_size = 0
        self.dropped = 0

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
            self.tail.append(chunk)
            self.tail_size += len(chunk)
            while self.tail and self.tail_size - len(self.tail[0]) >= LOG_TAIL:
                n = len(self.tail.popleft())
                self.dropped += n
                self.tail_size -= n

    def finish(self) -> str:
        parts = list(self.head)
        if self.tail:
            tail = b"".join(self.tail)
            if len(tail) > LOG_TAIL:
                self.dropped += len(tail) - LOG_TAIL
                tail = tail[-LOG_TAIL:]
            marker = f"\n[output truncated: {self.dropped} bytes omitted]\n".encode()
            parts += [marker, tail]
            self.out.write(marker + tail)
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
    cap: int | None = None,
    sandbox: Sandbox | None = None,
) -> Logged:
    """Run a program writing its output to log (appended, or replacing it), after a "$ command" line if
    header. With cap, a program that talks too much leaves its first `cap` bytes, a truncation marker and
    its last LOG_TAIL bytes. On a timeout the group is killed and the log ends with "timed out after Ns".
    With a sandbox the program runs inside it; the header still shows the program's own command line."""
    with safefile.open_log(log, append) as f:
        if header:
            f.write(f"$ {' '.join(args)}\n".encode())
            f.flush()
        p = _spawn(args, cwd, env, subprocess.PIPE, subprocess.STDOUT, sandbox)
        sink = _Sink(f, cap)
        reader = threading.Thread(target=sink.drain, args=(p.stdout,), daemon=True)
        reader.start()
        try:
            rc = _wait(p, timeout)
        finally:
            reader.join(5)
            if reader.is_alive():  # a leftover grandchild still holds the pipe: it is not wanted
                _kill_group(p)
                reader.join(5)
        text = sink.finish()
        if rc == TIMEOUT_RC:
            f.write(f"timed out after {timeout}s\n".encode())
        return Logged(rc, text)

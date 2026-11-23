# SPDX-License-Identifier: Apache-2.0
"""Run third-party code (yosys, nextpnr, Verilator, the C++ compiler, the compiled simulation) inside
bubblewrap, because building and simulating a design executes code the design's author wrote.

What a sandboxed program sees:

- a new, empty root on a tmpfs, remounted read-only, with only these mounts in it:
  /usr read-only, and the usual /bin /sbin /lib /lib64 symlinks into it (the compiler, make, perl and
  the system headers and libraries live there); from /etc only alternatives/ (the compilers are
  alternatives symlinks) and ld.so.cache; a minimal /proc and /dev; a private tmpfs on /tmp and
  another on the empty home directory;
- the paths the stage asks for: read-only ones (the pinned toolchain, the design's checkout, this
  repository's harness directory) and one writable directory (the design's build or reference
  directory), each at its real absolute path;
- no network (its own network namespace, no interface), its own PID, IPC and UTS namespaces, and a new
  session (no terminal to inject keystrokes into);
- exactly three environment variables: PATH (the pinned bin directory, /usr/bin, /bin), HOME and LANG;
- no user namespaces of its own (--unshare-user --disable-userns), the host name "sandbox", tmpfs mounts of
  at most TMPFS_BYTES each, and prlimit limits (LIMITS) on everything inside.

/home, /root, /srv, /var, /run, /mnt, /opt and the rest of /etc are not there. With --die-with-parent
and the PID namespace, killing bwrap (what proc.py does on a timeout or Ctrl-C) kills every process in
the sandbox, including one that called setsid.

What this does not do: limit CPU time, the number of processes, or the disk space a program uses in its
writable directory (the host checks the size of that directory after each tool and fails the stage past
safefile.MAX_SCRATCH, but a real quota needs privileges), filter system calls, hide /usr, or protect against
a kernel or bubblewrap bug.

TTHFV_SANDBOX selects the mode: "bwrap" (the default) or "none". If bwrap is wanted and cannot run, the
stage fails with a UserError; it never quietly runs without it.
"""

import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tthfv.errors import UserError

BWRAP = shutil.which("bwrap") or "bwrap"
ENV_VAR = "TTHFV_SANDBOX"
HOME = "/sandbox-home"  # an empty tmpfs; the HOME of everything in the sandbox
LANG = "C.UTF-8"
PROBE_TIMEOUT = 30  # seconds
HOSTNAME = "sandbox"  # the host's own name is not shown
TMPFS_BYTES = 256 << 20  # size of each of the two private tmpfs mounts, /tmp and the home directory
# Limits for everything in the sandbox, applied by prlimit inside it (so they cover the whole jailed tree):
# address space, largest file, core dumps, open files. RLIMIT_NPROC is deliberately not set: the kernel counts
# it per real user over the whole machine, so a limit low enough to stop a fork bomb would also break the
# owner's other processes (and builds with many compiler processes). The PID namespace and the timeout bound it.
LIMITS = ("--as=17179869184", "--fsize=1073741824", "--core=0", "--nofile=1024")

# The system directories every sandboxed program needs. Symlinks on the host (/bin -> usr/bin) are
# recreated as symlinks; real directories are bound read-only.
_SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
# The only parts of /etc that are exposed: the compiler symlinks (cc, c++ and friends) and the dynamic
# linker cache. Found by building and simulating real designs with nothing else in /etc.
_ETC = ("/etc/alternatives", "/etc/ld.so.cache")

_warned = False


def mode() -> Literal["bwrap", "none"]:
    value = os.environ.get(ENV_VAR) or "bwrap"
    if value not in ("bwrap", "none"):
        raise UserError(f"{ENV_VAR} must be 'bwrap' or 'none', not {value!r}")
    return value  # type: ignore[return-value]


def _base_args() -> list[str]:
    return [
        BWRAP, "--unshare-user", "--disable-userns", "--unshare-pid", "--unshare-net", "--unshare-ipc",
        "--unshare-uts", "--hostname", HOSTNAME, "--die-with-parent", "--new-session",
    ]  # fmt: skip


def _probe() -> str:
    """'' if a sandbox can be made here, else why not."""
    args = [*_base_args(), *_system_mounts(), "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    args += ["--clearenv", "/usr/bin/prlimit", *LIMITS, "--", "/usr/bin/true"]
    try:
        done = subprocess.run(
            args, check=False, capture_output=True, text=True, timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError:
        return f"{BWRAP!r} is not installed"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"{type(e).__name__}: {e}"
    return "" if done.returncode == 0 else (done.stderr.strip() or f"bwrap exited with {done.returncode}")


_probed: str | None = None


def unavailable_reason() -> str:
    """'' if a sandbox can be made here, else why not. Asked once per process."""
    global _probed
    if _probed is None:
        _probed = _probe()
    return _probed


def available() -> bool:
    """True if bwrap exists and can create the namespaces a sandbox needs."""
    return not unavailable_reason()


def reset() -> None:
    """Forget the answer of available(); for tests that change BWRAP."""
    global _probed
    _probed = None


def _system_mounts() -> list[str]:
    args: list[str] = []
    for name in _SYSTEM:
        p = Path(name)
        if p.is_symlink():
            args += ["--symlink", os.readlink(p), name]
        elif p.is_dir():
            args += ["--ro-bind", name, name]
    for name in _ETC:
        args += ["--ro-bind-try", name, name]
    return args


def wrap(
    argv: Sequence[str],
    *,
    cwd: Path,
    rw: Iterable[Path],
    ro: Iterable[Path],
    path: Iterable[Path] = (),
) -> list[str]:
    """The bwrap command line that runs argv in cwd with ro read-only and rw writable (see the module
    docstring). path: directories put first on PATH, for the pinned toolchain's bin directory; they and
    cwd must be inside one of the ro or rw paths. Every path is bound at its resolved absolute path."""
    args = [*_base_args(), "--cap-drop", "ALL", *_system_mounts()]
    args += ["--proc", "/proc", "--dev", "/dev"]
    args += ["--size", str(TMPFS_BYTES), "--tmpfs", "/tmp", "--size", str(TMPFS_BYTES), "--tmpfs", HOME]
    for p in dict.fromkeys(Path(x).resolve() for x in ro):
        args += ["--ro-bind", str(p), str(p)]
    for p in dict.fromkeys(Path(x).resolve() for x in rw):
        args += ["--bind", str(p), str(p)]
    search = ":".join([*(str(Path(d).resolve()) for d in path), "/usr/bin", "/bin"])
    args += ["--remount-ro", "/", "--chdir", str(Path(cwd).resolve()), "--clearenv"]
    for k, v in (("PATH", search), ("HOME", HOME), ("LANG", LANG)):
        args += ["--setenv", k, v]
    return [*args, "--", "prlimit", *LIMITS, "--", *argv]


@dataclass(frozen=True)
class Sandbox:
    """What a stage needs: one writable directory, read-only paths, and directories for PATH."""

    rw: tuple[Path, ...]
    ro: tuple[Path, ...] = ()
    path: tuple[Path, ...] = ()

    def wrap(self, argv: Sequence[str], cwd: Path) -> list[str]:
        return wrap(argv, cwd=cwd, rw=self.rw, ro=self.ro, path=self.path)


def for_stage(*, rw: Path, ro: Iterable[Path], path: Iterable[Path] = ()) -> Sandbox | None:
    """The Sandbox for a stage, or None if TTHFV_SANDBOX=none. A stage that needs bwrap where it cannot
    run is a UserError, never an unsandboxed run. (The warning for `none` is announce()'s, once per run.)"""
    if mode() == "none":
        return None
    if not available():
        raise UserError(
            f"the sandbox needs bwrap (bubblewrap) with unprivileged user namespaces, but: {unavailable_reason()}. "
            f"Install bubblewrap and allow user namespaces, or run in a container or virtual machine you "
            f"trust and set {ENV_VAR}=none"
        )
    return Sandbox(rw=(Path(rw),), ro=tuple(Path(p) for p in ro), path=tuple(Path(p) for p in path))


def announce() -> None:
    """Print the one warning line for TTHFV_SANDBOX=none. Called once by each command that can run third-party
    code, from the process the user started (not from worker processes), before any work and whether or not
    the work turns out to be cached."""
    global _warned
    if mode() == "none" and not _warned:
        _warned = True
        print(
            f"WARNING: {ENV_VAR}=none: third-party design code runs with your privileges, unsandboxed",
            file=sys.stderr,
            flush=True,
        )


def name() -> Literal["bwrap", "none"]:
    """What to record in build.json and reference.json."""
    return mode()

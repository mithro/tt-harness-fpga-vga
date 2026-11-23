# SPDX-License-Identifier: Apache-2.0
"""Reading what third-party code left in a directory it could write, without trusting it.

The sandbox may write one directory (a stage's scratch directory). Code in it can leave symlinks, hard
links, FIFOs, devices, directories, huge files and files that change. The host process, which is not in
the sandbox, therefore never opens a path in there by name the ordinary way. It opens the directory once
(not following a symlink), then each expected file relative to that directory with O_NOFOLLOW and
O_NONBLOCK (so a FIFO cannot block it), asks fstat on the opened descriptor, and accepts only a regular
file with exactly one link and a size within the bound. Anything else is a NotPlainFile, whose text is the
reason a stage reports."""

import errno
import hashlib
import os
import shutil
import stat
from pathlib import Path

CHUNK = 1 << 20
# Bounds on what a stage accepts from its scratch directory, in bytes. The dumps have an exactly known size.
MAX_BITSTREAM = 1 << 20  # an iCE40UP5K bitstream is about 100 KB
MAX_ASC = 256 << 20  # nextpnr's text form of the placed design
MAX_JSON = 512 << 20  # yosys's netlist
MAX_SCRATCH = 2 << 30  # the whole scratch directory, checked after each tool finishes


class NotPlainFile(Exception):
    """A file the stage expected was missing, not a plain file, or the wrong size. str() says why."""


def open_dir(path: Path) -> int:
    """A descriptor for the directory itself; a symlink or anything not a directory is refused."""
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise NotPlainFile(f"{path.name}: not a plain directory ({e.strerror})") from e


def open_plain(dir_fd: int, name: str, *, max_size: int, exact: int | None = None) -> int:
    """Open dir_fd/name for reading and return the descriptor, having checked that it is a regular file with
    one link and (exactly `exact` bytes, or at most max_size bytes). The caller closes it."""
    if "/" in name or name in ("", ".", ".."):
        raise ValueError(f"not a plain name: {name!r}")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError as e:
        raise NotPlainFile(f"{name}: the design's output is missing") from e
    except OSError as e:
        why = "a symbolic link" if e.errno in (errno.ELOOP, errno.EMLINK) else e.strerror
        raise NotPlainFile(f"{name}: not a plain file ({why})") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise NotPlainFile(f"{name}: not a plain file (it is a {_kind(st.st_mode)})")
        if st.st_nlink != 1:
            raise NotPlainFile(f"{name}: not a plain file (it has {st.st_nlink} hard links)")
        if exact is not None and st.st_size != exact:
            raise NotPlainFile(f"{name}: wrong size, {st.st_size} bytes where {exact} are expected")
        if st.st_size > max_size:
            raise NotPlainFile(f"{name}: too large, {st.st_size} bytes where at most {max_size} are accepted")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _kind(mode: int) -> str:
    for test, name in (
        (stat.S_ISDIR, "directory"),
        (stat.S_ISLNK, "symbolic link"),
        (stat.S_ISFIFO, "FIFO"),
        (stat.S_ISSOCK, "socket"),
        (stat.S_ISBLK, "block device"),
        (stat.S_ISCHR, "character device"),
    ):
        if test(mode):
            return name
    return "special file"


def _chunks(fd: int, size: int):
    """The file's bytes, reading no more than `size` (what fstat said): a file that grows is cut off."""
    left = size
    while left > 0:
        chunk = os.read(fd, min(CHUNK, left))
        if not chunk:
            break
        left -= len(chunk)
        yield chunk


def read_plain(dir_fd: int, name: str, *, max_size: int, exact: int | None = None) -> bytes:
    fd = open_plain(dir_fd, name, max_size=max_size, exact=exact)
    try:
        return b"".join(_chunks(fd, os.fstat(fd).st_size))
    finally:
        os.close(fd)


def copy_plain(dir_fd: int, name: str, dest: Path, *, max_size: int, exact: int | None = None) -> str:
    """Copy dir_fd/name to dest (a path in a directory the sandbox cannot write) by a temporary file and a
    rename. Returns the sha256 of the bytes copied."""
    fd = open_plain(dir_fd, name, max_size=max_size, exact=exact)
    digest = hashlib.sha256()
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        size = os.fstat(fd).st_size
        with tmp.open("wb") as out:
            for chunk in _chunks(fd, size):
                digest.update(chunk)
                out.write(chunk)
        tmp.replace(dest)
    finally:
        os.close(fd)
        tmp.unlink(missing_ok=True)
    return digest.hexdigest()


def exists_plain(dir_fd: int, name: str) -> bool:
    """True if dir_fd/name is a regular file with one link (not followed, not opened)."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def tree_size(path: Path) -> int:
    """The bytes under path (sizes of entries, symlinks not followed). Costs one lstat per entry."""
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    st = e.stat(follow_symlinks=False)
                    total += st.st_blocks * 512 if st.st_blocks else st.st_size
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(e.path)
        except OSError:  # a directory the design made unreadable: its contents cannot be counted
            pass
    return total


def remove_tree(path: Path) -> None:
    """Delete path and everything in it without ever following a symlink; a symlink or file at path itself
    is just unlinked. Directories the sandbox made unreadable or unwritable are opened up first."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        return
    stack = [str(path)]
    while (
        stack
    ):  # pass 1: owner may do everything in every directory (a plain lstat check, no symlink is entered)
        d = stack.pop()
        try:
            if not stat.S_ISDIR(os.lstat(d).st_mode):
                continue
            os.chmod(d, 0o700)
            with os.scandir(d) as it:
                stack += [e.path for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            pass
    shutil.rmtree(path)  # pass 2: fd-based, never follows a symlink


def make_empty_dir(path: Path) -> None:
    """path as a fresh, empty directory owned by us, whatever was there."""
    remove_tree(path)
    if path.is_symlink() or path.exists():  # a file or something else in the way
        path.unlink()
    path.mkdir(parents=True)

# SPDX-License-Identifier: Apache-2.0
"""Reading what third-party code left in a directory it could write, without trusting it.

The sandbox may write one directory (a stage's scratch directory). Code in it can leave symlinks, hard
links, FIFOs, devices, directories, huge files and files that change. The host process, which is not in
the sandbox, therefore never opens a path in there by name the ordinary way. It opens the directory once
(not following a symlink), then each expected file relative to that directory with O_NOFOLLOW and
O_NONBLOCK (so a FIFO cannot block it), asks fstat on the opened descriptor, and accepts only a regular
file with exactly one link and a size within the bound. Anything else is a NotPlainFile, whose text is the
reason a stage reports."""

import contextlib
import errno
import hashlib
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

CHUNK = 1 << 20
# Bounds on what a stage accepts from its scratch directory, in bytes. The dumps have an exactly known size.
MAX_BITSTREAM = 1 << 20  # an iCE40UP5K bitstream is about 100 KB
MAX_ASC = 256 << 20  # nextpnr's text form of the placed design
MAX_JSON = 512 << 20  # yosys's netlist
MAX_SCRATCH = 2 << 30  # the whole scratch directory, checked after each tool finishes
MAX_ENTRIES = 200_000  # files, directories and links in the whole scratch directory, checked with its size


class NotPlainFile(Exception):
    """A file the stage expected was missing, not a plain file, or the wrong size. str() says why."""


def open_dir(path: Path) -> int:
    """A descriptor for the directory itself; a symlink or anything not a directory is refused, a missing one is
    FileNotFoundError."""
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise
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


def exists_plain(dir_fd: int, name: str) -> bool:
    """True if dir_fd/name is a regular file with one link (not followed, not opened)."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def is_plain(path: Path) -> bool:
    """True if path is a regular file with one link (a symlink is not followed, so it is not)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


# ---- writing: host-owned directories, written so that nothing found there can redirect the write

MAX_RECORD = 256 << 20  # the largest record (json, log) the host reads back from a stage directory


def _unlink_entry(dir_fd: int, name: str) -> None:
    """Remove dir_fd/name whatever it is (a file, a symlink, a directory) without following it."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        _remove_dir_at(dir_fd, name)
    else:
        os.unlink(name, dir_fd=dir_fd)


@contextlib.contextmanager
def create(path: Path, mode: int = 0o644) -> Iterator[BinaryIO]:
    """Write path (a binary file object, closed on exit) through a temporary name next to it, then replace
    path with it. The directory is opened without following a symlink, a stale entry of the temporary name
    (a symlink, a file, a directory) is removed first without being followed, the temporary file is made with
    O_CREAT|O_EXCL|O_NOFOLLOW, and the replacement is a rename by directory descriptor, which replaces a
    symlink at path rather than writing through it. On an error the temporary file is removed and path is
    left as it was."""
    dfd = open_dir(path.parent)
    tmp = path.name + ".tmp"
    try:
        _unlink_entry(dfd, tmp)
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dfd
        )
        done = False
        try:
            with os.fdopen(fd, "wb") as f:
                yield f
                f.flush()
            _unlink_dir_in_the_way(dfd, path.name)
            os.replace(tmp, path.name, src_dir_fd=dfd, dst_dir_fd=dfd)
            done = True
        finally:
            if not done:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=dfd)
    finally:
        os.close(dfd)


def _unlink_dir_in_the_way(dfd: int, name: str) -> None:
    """os.replace cannot replace a directory with a file: clear a directory that sits at the target name."""
    try:
        if stat.S_ISDIR(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode):
            _remove_dir_at(dfd, name)
    except FileNotFoundError:
        pass


def write_file(path: Path, data: bytes | str, mode: int = 0o644) -> None:
    with create(path, mode) as f:
        f.write(data.encode() if isinstance(data, str) else data)


def read_file(path: Path, max_size: int = MAX_RECORD) -> bytes:
    """The bytes of a host-written record: through a directory opened without following a symlink and the file
    opened with O_NOFOLLOW; a missing file is FileNotFoundError, anything not plain or too big is NotPlainFile."""
    dfd = open_dir(path.parent)
    try:
        return read_plain(dfd, path.name, max_size=max_size)
    except NotPlainFile as e:
        if "missing" in str(e):
            raise FileNotFoundError(str(path)) from e
        raise
    finally:
        os.close(dfd)


def read_text(path: Path, max_size: int = MAX_RECORD, errors: str = "strict") -> str:
    return read_file(path, max_size).decode(errors=errors)


def open_read(path: Path) -> BinaryIO:
    """An open binary file for reading a host-written file: regular, one link, not followed."""
    dfd = open_dir(path.parent)
    try:
        fd = open_plain(dfd, path.name, max_size=1 << 62)
    finally:
        os.close(dfd)
    return os.fdopen(fd, "rb")


def open_log(path: Path, append: bool = True) -> BinaryIO:
    """A log file for the host to write (the child gets a pipe, never this file): created or appended to without
    following a symlink; an entry that is not a plain file is removed and replaced."""
    dfd = open_dir(path.parent)
    try:
        # O_NONBLOCK: opening a FIFO for writing blocks until a reader turns up (or fails at once with no reader);
        # the host must never wait on something the sandbox left there. It is cleared again below.
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
            | os.O_CLOEXEC
            | (os.O_APPEND if append else os.O_TRUNC)
        )
        try:
            fd = os.open(path.name, flags, 0o644, dir_fd=dfd)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                os.close(fd)
                raise OSError(errno.ELOOP, "not a plain file")
            os.set_blocking(fd, True)
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.EACCES) and not os.path.lexists(path):
                raise
            _unlink_entry(dfd, path.name)
            fd = os.open(path.name, flags | os.O_EXCL, 0o644, dir_fd=dfd)
            os.set_blocking(fd, True)
        return os.fdopen(fd, "ab" if append else "wb")
    finally:
        os.close(dfd)


def copy_plain(dir_fd: int, name: str, dest: Path, *, max_size: int, exact: int | None = None) -> str:
    """Copy dir_fd/name (in the sandbox's directory) to dest (a host-owned path) safely. Returns the sha256."""
    fd = open_plain(dir_fd, name, max_size=max_size, exact=exact)
    digest = hashlib.sha256()
    try:
        with create(dest) as out:
            for chunk in _chunks(fd, os.fstat(fd).st_size):
                digest.update(chunk)
                out.write(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


# ---- walking and removing trees by directory descriptors: any depth, any name length, no symlink followed

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_subdir(parent_fd: int, name: str) -> int:
    """A descriptor for the directory parent_fd/name (not a symlink), made readable and writable for us first: a
    directory the sandbox left with mode 000 is opened through an O_PATH descriptor and /proc/self/fd."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except PermissionError:
        pfd = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            os.chmod(f"/proc/self/fd/{pfd}", 0o700)
        finally:
            os.close(pfd)
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    with contextlib.suppress(OSError):
        os.fchmod(fd, 0o700)
    return fd


class _OverCap(Exception):
    """A walk that was told its limits found it is past one of them and stopped counting."""

    def __init__(self, what: str):
        super().__init__(what)
        self.what = what  # "bytes" or "entries"


def _walk_at(
    parent_fd: int,
    name: str,
    remove: bool,
    entries: list[int] | None = None,
    limit: tuple[int, int] | None = None,
) -> int:
    """Walk the tree parent_fd/name iteratively, holding two descriptors at a time (so neither the depth nor the
    descriptor limit matters), going back up through "..", which is checked against what was recorded on the way
    down. Returns the allocated bytes of everything below, and adds the number of entries to entries[0] if given.
    With remove, every entry is deleted, then the directory itself. limit=(bytes, entries): raise _OverCap the moment
    the running totals pass either (entries counts every directory entry: files, directories and links), instead of
    finishing the count."""
    total = 0
    frames: list[
        tuple[str, tuple[int, int], list[str]]
    ] = []  # (directory name, (dev, ino), subdirectories left)

    def scan(fd: int, dirname: str) -> None:
        nonlocal total
        st = os.fstat(fd)
        subdirs: list[str] = []
        with os.scandir(fd) as it:
            for e in it:
                est = e.stat(follow_symlinks=False)
                total += est.st_blocks * 512
                if entries is not None:
                    entries[0] += 1
                if limit is not None:
                    if total > limit[0]:
                        raise _OverCap("bytes")
                    if entries is not None and entries[0] > limit[1]:
                        raise _OverCap("entries")
                if stat.S_ISDIR(est.st_mode):
                    subdirs.append(e.name)
                elif remove:
                    os.unlink(e.name, dir_fd=fd)
        frames.append((dirname, (st.st_dev, st.st_ino), subdirs))

    cur = _open_subdir(parent_fd, name)
    try:
        scan(cur, name)
        while frames:
            subdirs = frames[-1][2]
            if subdirs:
                sub = subdirs.pop()
                child = _open_subdir(cur, sub)
                os.close(cur)
                cur = child
                scan(cur, sub)
                continue
            done_name = frames.pop()[0]
            if not frames:
                break
            up = os.open("..", _DIR_FLAGS, dir_fd=cur)
            got = os.fstat(up)
            if (got.st_dev, got.st_ino) != frames[-1][1]:
                os.close(up)
                raise OSError(errno.EBUSY, "the directory tree changed while it was being walked")
            os.close(cur)
            cur = up
            if remove:
                os.rmdir(done_name, dir_fd=cur)
    finally:
        os.close(cur)
    if remove:
        os.rmdir(name, dir_fd=parent_fd)
    return total


def _remove_dir_at(parent_fd: int, name: str) -> None:
    _walk_at(parent_fd, name, True)


def tree_usage(path: Path) -> tuple[int, int]:
    """(allocated bytes, number of entries) under path, at any depth (symlinks not followed, unreadable
    directories opened up)."""
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        return 0, 0
    entries = [0]
    pfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        return _walk_at(pfd, path.name, False, entries), entries[0]
    finally:
        os.close(pfd)


def tree_size(path: Path) -> int:
    """The allocated bytes under path, at any depth (symlinks not followed, unreadable directories opened up)."""
    return tree_usage(path)[0]


def _size_text(n: int) -> str:
    for unit, name in ((1 << 30, "GiB"), (1 << 20, "MiB"), (1 << 10, "KiB")):
        if n >= unit and n % unit == 0:
            return f"{n // unit} {name}"
    return f"{n} bytes"


def scratch_exceeded(path: Path) -> str:
    """For the watchdog that looks while a tool runs: '' or "scratch limit (2 GiB) exceeded during the run". The walk
    stops the moment either cap is passed, so a flood of directories costs at most the cap's worth of entries."""
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        return ""
    pfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _walk_at(pfd, path.name, False, [0], (MAX_SCRATCH, MAX_ENTRIES))
    except _OverCap as e:
        if e.what == "bytes":
            return f"scratch limit ({_size_text(MAX_SCRATCH)}) exceeded during the run"
        return f"scratch limit ({MAX_ENTRIES} entries) exceeded during the run"
    finally:
        os.close(pfd)
    return ""


def scratch_overrun(path: Path) -> str:
    """'' if the stage's scratch directory is within MAX_SCRATCH bytes and MAX_ENTRIES entries, else the reason.
    There is no hard quota on a plain directory (RLIMIT_FSIZE bounds each file; this is checked after each
    tool), and many tiny files exhaust inodes, not bytes, so the entries are counted too."""
    used, entries = tree_usage(path)
    if used > MAX_SCRATCH:
        return f"the tool filled its scratch directory: {used} bytes, at most {MAX_SCRATCH} allowed"
    if entries > MAX_ENTRIES:
        return f"the tool filled its scratch directory: {entries} entries, at most {MAX_ENTRIES} allowed"
    return ""


def remove_tree(path: Path) -> None:
    """Delete path and everything in it without ever following a symlink, at any depth and with any names; a
    symlink or file at path itself is just unlinked. Directories the sandbox made unreadable or unwritable are
    opened up on the way. Raises OSError if something cannot be removed."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        return
    pfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _remove_dir_at(pfd, path.name)
    finally:
        os.close(pfd)


def _open_trash(trash: Path, create: bool) -> int:
    """A descriptor for the trash directory, which is never followed if it is a symlink (NotPlainFile) and is
    created (mode 0700) if create. The directory that holds it is opened the ordinary way: the work directory
    itself may be a symlink the user made."""
    pfd = open_dir(trash.parent)
    try:
        if create:
            try:
                os.mkdir(trash.name, 0o700, dir_fd=pfd)
            except FileExistsError:
                pass
        try:
            return os.open(trash.name, _DIR_FLAGS, dir_fd=pfd)
        except FileNotFoundError:
            raise
        except OSError as e:
            raise NotPlainFile(f"{trash.name}: not a plain directory ({e.strerror}; a symbolic link?)") from e
    finally:
        os.close(pfd)


def discard(path: Path, trash: Path) -> str:
    """Remove path; if that fails, move it aside into the host-owned trash directory so that the stage can go on.
    The trash is opened without following a symlink and the move is a rename by directory descriptors. Returns ""
    if it was removed, else a sentence saying what happened (the stage reports it as a failure)."""
    if not os.path.lexists(path):
        return ""
    try:
        remove_tree(path)
        return ""
    except Exception as e:  # noqa: BLE001 - whatever the tree did, the stage must not die of it
        why = f"{type(e).__name__}: {e}"
    dest_name = f"{path.parent.name}-{path.name}-{os.getpid()}-{time.time_ns()}"
    try:
        tfd = _open_trash(trash, create=True)
        try:
            sfd = open_dir(path.parent)
            try:
                os.rename(path.name, dest_name, src_dir_fd=sfd, dst_dir_fd=tfd)
            finally:
                os.close(sfd)
        finally:
            os.close(tfd)
    except (OSError, NotPlainFile) as e2:
        return f"{path.name} could not be removed ({why}) or moved aside ({e2})"
    return f"{path.name} could not be removed ({why}); moved to {trash / dest_name}"


def empty_trash_and_say(trash: Path) -> None:
    """empty_trash(), with the one line a command prints when it removed something."""
    if removed := empty_trash(trash):
        print(f"removed {removed} entries from {trash}", flush=True)


def empty_trash(trash: Path) -> int:
    """Best effort: remove everything in the trash directory (no symlink followed, by directory descriptors) and
    return how many entries were removed. A missing trash is 0; a trash that is a symlink is not touched."""
    try:
        tfd = _open_trash(trash, create=False)
    except (FileNotFoundError, NotPlainFile):
        return 0
    removed = 0
    try:
        with os.scandir(tfd) as it:
            names = [e.name for e in it]
        for name in names:
            try:
                _unlink_entry(tfd, name)
                removed += 1
            except OSError:
                pass  # best effort; the next run tries again
    finally:
        os.close(tfd)
    return removed


def make_empty_dir(path: Path, trash: Path | None = None) -> str:
    """path as a fresh, empty directory, whatever was there (removed, or moved to trash if it cannot be removed).
    Returns the note of discard(); a path that is still in the way is a UserError."""
    from tthfv import paths  # not at the top: paths must not import this module
    from tthfv.errors import UserError

    note = discard(path, trash or paths.WORK / "trash")
    if os.path.lexists(path):
        raise UserError(f"{path} is in the way and cannot be removed: {note}")
    path.mkdir(parents=True)
    return note


# ---- listing a checkout without following anything

MAX_TREE_DEPTH = 200
MAX_REL_PATH = 3500  # bytes; below PATH_MAX, so a relative path from the top always works


class TreeTooDeep(NotPlainFile):
    """A tree nested too deeply or with names too long to be a project's sources."""


def list_tree(top: Path, skip: tuple[str, ...] = (".git",)) -> list[tuple[str, str]]:
    """Every entry below top that is not a directory, as (path relative to top, kind) with kind "file", "link"
    (a symlink, whatever it points at, and not entered if it points at a directory) or "special" (FIFO, socket,
    device). Directories named in skip are not entered, at any depth. Walks by directory descriptor, without
    following anything; top itself must be a real directory (NotPlainFile otherwise). Depth and path length are
    bounded (TreeTooDeep). The relative paths pass only through real directories, so opening one with
    os.open(rel, ..., dir_fd=<descriptor of top>) and O_NOFOLLOW never leaves the tree."""
    found: list[tuple[str, str]] = []

    def walk(fd: int, prefix: str, depth: int) -> None:
        if depth > MAX_TREE_DEPTH:
            raise TreeTooDeep(f"{prefix[:60]}...: nested more than {MAX_TREE_DEPTH} directories deep")
        with os.scandir(fd) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            rel = prefix + e.name
            if len(rel.encode()) > MAX_REL_PATH:
                raise TreeTooDeep(f"{rel[:60]}...: path longer than {MAX_REL_PATH} bytes")
            st = e.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                if e.name in skip:
                    continue
                child = os.open(e.name, _DIR_FLAGS, dir_fd=fd)
                try:
                    walk(child, rel + "/", depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISLNK(st.st_mode):
                found.append((rel, "link"))
            elif stat.S_ISREG(st.st_mode):
                found.append((rel, "file"))
            else:
                found.append((rel, "special"))

    top_fd = open_dir(top)
    try:
        walk(top_fd, "", 0)
    finally:
        os.close(top_fd)
    return found

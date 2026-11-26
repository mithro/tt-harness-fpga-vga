# SPDX-License-Identifier: Apache-2.0
"""Publish the built bitstreams into the VGA video archive as `fpga-library.json` (schema 1).

The format is fixed by the spec of the tinytapeout-vga-videos repository, section 1 and section 6
(docs/superpowers/specs/2026-11-26-fpga-library-source-design.md in that repository). Nothing is imported from
it: the rules are repeated here and the archive's own checker is run as a program (`--check-cmd`).

The whole document is decided in memory first; nothing is written if anything fails there. Then each `.bin`
is written under a temporary name and linked to its final name without replacing a file (a file already at that
name must have the same bytes and is left alone; a `.bin` is never deleted or overwritten), the index is written
as `fpga-library.json.tmp`, the check command is run on it, and only if that exits 0 is the index renamed onto
`fpga-library.json`. The only thing deleted is the `.tmp` index.
"""

import argparse
import collections
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from tthfv import manifest, paths, safefile
from tthfv.errors import UserError
from tthfv.manifest import Design
from tthfv.reference import _PROFILE_NAME, SCRATCH
from tthfv.results import Invalid, _read, _videos_repo_commit

MAX_BITSTREAM = 262144
PREAMBLE = bytes.fromhex("7eaa997e")
PREAMBLE_WITHIN = 68  # the sync word must lie inside the first this-many bytes
DEFAULT_PROFILE = "boards/fpga-4.json"
INDEX_NAME = "fpga-library.json"
# The live archive: nothing is published there until the owner approves the format. The test suite points this
# at a scratch directory; nothing ever probes the real path.
LIVE_ARCHIVE = Path("/srv/data.wafer.space")
LIVE_APPROVAL_ENV = "TTHFV_PUBLISH_LIVE"
LIVE_APPROVAL = "approved-by-owner"
# The commit of tinytapeout-fpga-demos whose packaged demo names are reserved (see RESERVED_NAMES).
DEMO_NAMES_COMMIT = "da7d10d"
RESERVED_NAMES = (
    "tt_um_counter_7seg",
    "tt_um_factory_test",
    "tt_um_pwm_breathe",
    "tt_um_uart_hello",
    "tt_um_vga_pattern",
    "idle_display",  # the daemon's IDLE_NAME
)
# Shuttles, newest first. Source: the reverse of the shuttle list at https://index.tinytapeout.com/index.json
# (`updated` 2026-11-15T19:53:08.295Z). A published design whose shuttle is not listed fails the run.
SHUTTLE_ORDER = (
    "ttsky26c", "ttgf0p3", "ttgf26b", "ttgf26a", "ttsky26b", "ttsky26a", "ttihp0p4", "ttihp26a", "ttgf0p2",
    "ttsky25b", "ttgf0p1", "ttsky25a", "ttihp25b", "ttcad25a", "ttihp0p3", "ttihp25a", "tt09", "ttihp0p2",
    "tt08", "ttihp0p1", "tt07", "tt06", "tt05of", "tt05",
)  # fmt: skip

_STATUSES = ("ok", "timing-fail")
_HEX40 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SHUTTLE = re.compile(r"[a-z0-9]{1,16}")
_MACRO = re.compile(r"tt_um_[A-Za-z0-9_]{1,58}")
_VERDICT = re.compile(r"[a-z-]{1,32}")
_ID = re.compile(r"[a-z0-9_]{1,40}")
_ARCHIVE_PATH = re.compile(r"[a-z0-9]{1,16}/tt_um_[A-Za-z0-9_]{1,58}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_GENERATED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_PIN_PORTS = (("ui", "ui_in"), ("uo", "uo_out"), ("uio", "uio"))  # manifest key -> key of the document


class Skip(Exception):
    """The design is not published; the message is the reason listed."""


@dataclass
class Summary:
    archive: Path
    published: int = 0
    timing_not_met: int = 0
    skipped: list[dict] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # ids of the previous index that the new one drops


def library_id(shuttle: str, macro: str) -> str:
    """The id rule of the spec (Ruling AC): the design's name on the Pi, at most 40 characters of
    `[a-z0-9_]`. A macro without the `tt_um_` prefix is a ValueError."""
    if not macro.startswith("tt_um_"):
        raise ValueError(f"{shuttle}/{macro}: the macro does not start with tt_um_")
    name = re.sub(r"[^a-z0-9_]", "_", f"{shuttle}_{macro.removeprefix('tt_um_')}".lower())
    if len(name) <= 40:
        return name
    return name[:31] + "_" + hashlib.sha256(f"{shuttle}/{macro}".encode()).hexdigest()[:8]


def check_reserved(id: str) -> None:
    """UserError for an id equal to a packaged demo's name (at DEMO_NAMES_COMMIT) or to `idle_display`."""
    if id in RESERVED_NAMES:
        raise UserError(f"the id {id!r} is a reserved name (a packaged demo or the idle display)")


def _is_int(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _base(repo: str) -> str:
    return repo.rstrip("/").removesuffix(".git").rstrip("/")


def _pinout(design: Design) -> dict:
    out = {}
    for port, key in _PIN_PORTS:
        names = [("" if n is None else str(n))[:64] for n in (design.pinout.get(port) or [])][:8]
        out[key] = names + [""] * (8 - len(names))
    return out


def _build_record(work: Path, design: Design) -> tuple[dict, bytes]:
    """The build record and the bitstream bytes, or Skip."""
    name = f"build/{design.id}/build.json"
    try:
        b = _read(work / name, work)
    except Invalid as e:
        raise Skip(f"{name} is unreadable or damaged") from e
    if b is None:
        raise Skip("no build record")
    if b.get("status") not in _STATUSES:
        raise Skip(f"build status is {b.get('status')!r}, not ok or timing-fail")
    bitstream = b.get("bitstream")
    if not isinstance(bitstream, str) or not bitstream or "/" in bitstream or bitstream in (".", ".."):
        raise Skip("build record names no bitstream")
    if b.get("commit") != design.commit:
        raise Skip("stale build: its commit differs from the manifest's")
    try:
        paths.no_symlinks(work / "build" / design.id / bitstream, work)
        data = safefile.read_file(work / "build" / design.id / bitstream, max_size=1 << 24)
    except FileNotFoundError as e:
        raise Skip("bitstream missing") from e
    except (UserError, safefile.NotPlainFile, OSError) as e:
        raise Skip("bitstream missing or not a plain file") from e
    if hashlib.sha256(data).hexdigest() != b.get("bitstream_sha256"):
        raise Skip("bitstream sha256 differs from the build record's")
    if len(data) > MAX_BITSTREAM:
        raise Skip(f"bitstream is larger than {MAX_BITSTREAM} bytes")
    if PREAMBLE not in data[:PREAMBLE_WITHIN]:
        raise Skip(f"bitstream has no preamble within its first {PREAMBLE_WITHIN} bytes")
    if not _is_int(b.get("fmax_hz"), 1, 1_000_000_000):
        raise Skip("no fmax")
    if not _is_int(b.get("clock_hz"), 1, 200_000_000):
        raise Skip("clock_hz is not an integer from 1 to 200000000")
    return b, data


def _reference_record(work: Path, design: Design, profile: str, build: dict, missing: str) -> dict:
    """The reference record of a profile, current for the manifest's commit and the build's clock and with at
    least one frame, or Skip. `frames_complete` must equal the number of `frames` (one entry per complete
    frame); if not, the record is inconsistent."""
    base = f"reference/{design.id}/{profile}"
    try:
        ref = _read(work / base / "reference.json", work)
        fail = None if ref is not None else _read(work / base / "reference-fail.json", work)
    except Invalid as e:
        raise Skip(f"{base} record is unreadable or damaged") from e
    if ref is None:
        if fail is not None:
            raise Skip(f"reference failed under profile {profile}: {fail.get('status')}")
        raise Skip(missing)
    if ref.get("schema") != 3:
        raise Skip(f"reference.json has schema {ref.get('schema')!r}, not 3")
    complete, frames = ref.get("frames_complete"), ref.get("frames")
    if not isinstance(complete, int) or isinstance(complete, bool) or not isinstance(frames, list):
        raise Skip("reference record is inconsistent: frames_complete or frames has the wrong type")
    if complete != len(frames):
        raise Skip("reference record is inconsistent: frames_complete differs from the number of frames")
    if complete < 1:
        raise Skip("reference has no frames (no sync)")
    key = ref.get("key")
    if not isinstance(key, dict) or key.get("commit") != design.commit:
        raise Skip("stale reference: its key commit differs from the manifest's")
    if ref.get("clock_hz") != build.get("clock_hz"):
        raise Skip("stale reference: its clock_hz differs from the build's")
    return ref


def _plain_file_under(base: Path, rel: str) -> bool:
    """Whether base/rel is a regular file reached without a symlinked directory (or a symlink) under base."""
    path = base / rel
    try:
        paths.no_symlinks(path, base)
        return stat.S_ISREG(os.lstat(path).st_mode)
    except (UserError, OSError):
        return False


def _media(root: Path, archive: Path, shuttle: str, macro: str, suffix: str) -> str | None:
    """The archive path of a media file, or None. The site reads the archive, so the file must be there as a
    regular file; when the media root is somewhere else it must be there too."""
    rel = f"{shuttle}/{macro}/{shuttle}_{macro}_{suffix}"
    if not _ARCHIVE_PATH.fullmatch(rel):
        return None
    bases = [root] if os.path.abspath(root) == os.path.abspath(archive) else [root, archive]
    return rel if all(_plain_file_under(b, rel) for b in bases) else None


def _check_entry(e: dict) -> None:
    """Skip, naming the field, at the first field that breaks the spec's rules."""

    def bad(name: str, why: str):
        raise Skip(f"invalid {name}: {why}")

    def text(name: str, low: int, high: int) -> None:
        v = e[name]
        if not isinstance(v, str) or not low <= len(v) <= high:
            bad(name, f"a string of {low} to {high} characters is required")

    def url(name: str) -> None:
        v = e[name]
        if not isinstance(v, str) or len(v) > 500 or (v and not v.startswith("https://")):
            bad(name, "empty, or https:// and at most 500 characters")

    if not _SHUTTLE.fullmatch(e["shuttle"]):
        bad("shuttle", "must match [a-z0-9]{1,16}")
    if not _MACRO.fullmatch(e["macro"]):
        bad("macro", "must match tt_um_[A-Za-z0-9_]{1,58}")
    if e["id"] != library_id(e["shuttle"], e["macro"]) or not _ID.fullmatch(e["id"]):
        bad("id", "differs from the id rule")
    text("title", 1, 200)
    text("author", 0, 200)
    text("description", 0, 2000)
    url("docs_url")
    url("repo_url")
    if not _HEX40.fullmatch(e["commit"]):
        bad("commit", "must be 40 lower-case hex digits")
    if not _is_int(e["clock_hz"], 1, 200_000_000):
        bad("clock_hz", "an int from 1 to 200000000 is required")
    if not _is_int(e["ui_in_mask"], 0, 255):
        bad("ui_in_mask", "an int from 0 to 255 is required")
    if e["ui_in"] is None:
        if e["ui_in_mask"] != 0:
            bad("ui_in", "null with a non-zero mask")
    elif e["ui_in_mask"] == 0:
        bad("ui_in", "must be null when ui_in_mask is 0")
    elif not _is_int(e["ui_in"], 0, 255) or e["ui_in"] & ~e["ui_in_mask"]:
        bad("ui_in", "an int inside ui_in_mask is required")
    if not isinstance(e["bin_sha256"], str) or not _SHA256.fullmatch(e["bin_sha256"]):
        bad("bin_sha256", "must be 64 lower-case hex digits")
    if e["bin"] != f"{e['shuttle']}/{e['macro']}/{e['id']}-{e['bin_sha256'][:12]}.bin":
        bad("bin", "differs from <shuttle>/<macro>/<id>-<bin_sha256[:12]>.bin")
    if not _is_int(e["bin_bytes"], 1, MAX_BITSTREAM):
        bad("bin_bytes", "an int from 1 to 262144 is required")
    if not _is_int(e["fmax_hz"], 1, 1_000_000_000):
        bad("fmax_hz", "an int from 1 to 1000000000 is required")
    if not _VERDICT.fullmatch(e["verdict"]):
        bad("verdict", "must match [a-z-]{1,32}")
    for name in ("bin", "poster", "preview_gif", "video_10s_webm"):
        v = e[name]
        if v is None and name != "bin":
            continue
        parts = v.split("/") if isinstance(v, str) else []
        if not (
            isinstance(v, str) and _ARCHIVE_PATH.fullmatch(v) and ".." not in v
            and parts[:2] == [e["shuttle"], e["macro"]]
        ):  # fmt: skip
            bad(name, "not a valid archive path")
    for key, names in e["pinout"].items():
        if len(names) != 8 or any(not isinstance(n, str) or len(n) > 64 for n in names):
            bad("pinout", f"{key} needs exactly 8 names of at most 64 characters")


def _candidate(work: Path, design: Design, board: str, media_root: Path, archive: Path) -> tuple[dict, bytes]:
    """The entry of one design and its bitstream, or Skip."""
    if design.shuttle == "calibration":
        raise Skip("calibration design")
    if design.qspi is not None:
        raise Skip("needs a QSPI Pmod")
    b, data = _build_record(work, design)
    _reference_record(work, design, "ideal", b, "no reference")
    ref = _reference_record(work, design, board, b, f"no reference under profile {board}")
    mask, requested = ref.get("ui_in_mask_used"), ref.get("ui_in_requested")
    if not _is_int(mask, 0, 255):
        raise Skip("invalid ui_in_mask: the board reference has no int from 0 to 255")
    if not _is_int(requested, 0, 255):
        raise Skip("invalid ui_in_requested: the board reference has no int from 0 to 255")
    verdict = design.render_verdict
    if not isinstance(verdict, str) or not _VERDICT.fullmatch(verdict):
        raise Skip("no render verdict")
    if not _MACRO.fullmatch(design.macro):
        raise Skip("invalid macro: must match tt_um_[A-Za-z0-9_]{1,58}")
    sha = hashlib.sha256(data).hexdigest()
    id = library_id(design.shuttle, design.macro)
    rel = f"{design.shuttle}/{design.macro}"
    entry = {
        "id": id,
        "shuttle": design.shuttle,
        "macro": design.macro,
        "title": design.title or design.macro,
        "author": design.author,
        "description": "",
        "docs_url": f"https://tinytapeout.com/chips/{design.shuttle}/{design.macro}",
        "repo_url": _base(design.repo),
        "commit": design.commit,
        "clock_hz": b["clock_hz"],
        "pinout": _pinout(design),
        "ui_in": (requested & mask) if mask else None,
        "ui_in_mask": mask,
        "bin": f"{rel}/{id}-{sha[:12]}.bin",
        "bin_bytes": len(data),
        "bin_sha256": sha,
        "build_status": b["status"],
        "fmax_hz": b["fmax_hz"],
        "validation": "none",
        "verdict": verdict,
        "qspi": "none",
        "poster": _media(media_root, archive, design.shuttle, design.macro, "poster.png"),
        "preview_gif": _media(media_root, archive, design.shuttle, design.macro, "preview.gif"),
        "video_10s_webm": _media(media_root, archive, design.shuttle, design.macro, "10s.webm"),
    }
    _check_entry(entry)
    return entry, data


def _tthfv_commit() -> str | None:
    """HEAD of this repository, or None if it cannot be read."""
    try:
        return manifest._head(paths.REPO_ROOT)
    except (OSError, UserError):
        return None


def _tree_is_dirty() -> bool:
    return False


def _now() -> datetime:
    return datetime.now(UTC)


def _check_archive(archive: Path) -> Path:
    """The archive as an absolute path; UserError unless it is an existing directory that is not the live archive
    (nor under it, also through symlinks) or the live archive has been approved."""
    archive = Path(os.path.abspath(archive))
    if not archive.is_dir():
        raise UserError(f"--archive {archive} must be an existing directory")
    live = os.path.realpath(LIVE_ARCHIVE)
    real = os.path.realpath(archive)
    if (real == live or real.startswith(live + os.sep)) and os.environ.get(
        LIVE_APPROVAL_ENV
    ) != LIVE_APPROVAL:
        raise UserError("publishing into the live archive is not approved yet")
    return archive


def _oserror(e: OSError) -> UserError:
    return UserError(f"{e.filename or 'archive'}: {e.strerror or e}")


def _ensure_dir(path: Path, archive: Path) -> None:
    paths.no_symlinks(path, archive)
    if os.path.lexists(path):
        if not path.is_dir():
            raise UserError(f"{path} exists and is not a directory")
        return
    os.mkdir(path, 0o755)
    os.chmod(path, 0o755)  # whatever the umask


def _write_bin(archive: Path, rel: str, data: bytes, sha: str) -> None:
    final = archive / rel
    _ensure_dir(final.parent.parent, archive)
    _ensure_dir(final.parent, archive)
    if os.path.lexists(final):
        _same_file(final, sha)
        return
    fd, tmp = tempfile.mkstemp(dir=final.parent, prefix=".tmp-bin-")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(data)
        try:
            os.link(tmp, final)  # never replaces an existing file
        except FileExistsError:
            _same_file(final, sha)
    finally:
        os.unlink(tmp)


def _same_file(path: Path, sha: str) -> None:
    """UserError unless an existing file at a bin's name is a regular file with the bytes it should have. Other
    hard links to it (a backup snapshot) are fine; the file is read in chunks, whatever its size."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise UserError(f"{path} already exists and is not a regular file; refusing to touch it")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise UserError(f"{path} already exists and is not a regular file; refusing to touch it")
            while chunk := f.read(1 << 20):
                digest.update(chunk)
    except OSError as e:
        raise UserError(f"{path}: cannot compare the existing file: {e.strerror or e}") from e
    if digest.hexdigest() != sha:
        raise UserError(f"{path} already exists with different bytes; refusing to touch it")


def _preflight(archive: Path, found: list[tuple[dict, bytes]], tmp: Path) -> None:
    """Everything that can be known to collide, before the first write: a final bin name that is taken by
    something other than the same bytes, a symlink or file in the way of a directory, and the .tmp index path."""
    for entry, _ in found:
        final = archive / entry["bin"]
        paths.no_symlinks(final, archive)
        for d in (final.parent.parent, final.parent):
            if os.path.lexists(d) and not d.is_dir():
                raise UserError(f"{d} exists and is not a directory")
        if os.path.lexists(final):
            _same_file(final, entry["bin_sha256"])
    if os.path.lexists(tmp) and (os.path.islink(tmp) or not stat.S_ISREG(os.lstat(tmp).st_mode)):
        raise UserError(f"{tmp} exists and is not a regular file; remove it by hand")


def _write_index_tmp(path: Path, text: str) -> None:
    if os.path.lexists(path):
        if path.is_dir() and not path.is_symlink():
            raise UserError(f"{path} is a directory")
        os.unlink(path)  # a leftover .tmp index: the one thing that may be deleted
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o644)
        f.write(text)


def _run_check(argv: list[str]) -> None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise UserError(f"the check command {argv[0]!r} could not be run: {e}") from e
    if done.returncode != 0:
        out = (done.stdout + done.stderr).strip()
        raise UserError(
            f"the check command failed (exit {done.returncode}); the index was not put in place:\n{out}"
        )


def run(
    designs: list[Design],
    archive: Path,
    check_cmd: str,
    work: Path | None = None,
    profile: str = DEFAULT_PROFILE,
    media_root: Path | None = None,
    allow_removals: bool = False,
) -> Summary:
    """Publish `designs` into `archive`. Raises UserError, leaving the previous index in place, on any failure."""
    work = Path(os.path.abspath(paths.WORK if work is None else work))
    argv = shlex.split(check_cmd)
    if not argv:
        raise UserError("--check-cmd is empty")
    board = Path(profile).stem
    if board == "ideal" or not _PROFILE_NAME.fullmatch(board) or board in (SCRATCH, "trash"):
        raise UserError(f"--profile {profile}: the stem {board!r} cannot name a board profile")
    archive = _check_archive(archive)
    media = archive if media_root is None else Path(media_root)
    tthfv_commit, videos_commit = _tthfv_commit(), _videos_repo_commit()
    if not tthfv_commit or not _HEX40.fullmatch(tthfv_commit):
        raise UserError("the git HEAD of this repository could not be read; the index requires it")
    if not videos_commit or not _HEX40.fullmatch(videos_commit):
        raise UserError("the videos repository's commit could not be read; the index requires it")

    summary = Summary(archive=archive)
    found: list[tuple[dict, bytes]] = []
    owner: dict[str, str] = {}
    for d in sorted(designs, key=lambda d: d.id):
        try:
            entry, data = _candidate(work, d, board, media, archive)
        except Skip as s:
            summary.skipped.append({"id": d.id, "reason": str(s)})
            continue
        except (KeyError, TypeError, ValueError, IndexError) as e:
            summary.skipped.append({"id": d.id, "reason": f"records unusable: {type(e).__name__}: {e}"})
            continue
        check_reserved(entry["id"])
        if entry["id"] in owner:
            raise UserError(f"{owner[entry['id']]} and {d.id} both get the id {entry['id']!r}")
        owner[entry["id"]] = d.id
        if d.shuttle not in SHUTTLE_ORDER:
            raise UserError(f"{d.id}: the shuttle {d.shuttle!r} is not in SHUTTLE_ORDER (tthfv/publish.py)")
        found.append((entry, data))
    found.sort(key=lambda p: (SHUTTLE_ORDER.index(p[0]["shuttle"]), p[0]["title"].casefold(), p[0]["id"]))
    document = {
        "schema": 1,
        "generated": _now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tthfv_commit": tthfv_commit,
        "videos_repo_commit": videos_commit,
        "designs": [e for e, _ in found],
    }
    text = json.dumps(document, indent=2, allow_nan=False) + "\n"
    summary.published = len(found)
    summary.timing_not_met = sum(e["build_status"] == "timing-fail" for e, _ in found)

    tmp, final = archive / f"{INDEX_NAME}.tmp", archive / INDEX_NAME
    summary.removed = sorted(_previous_ids(final) - {e["id"] for e, _ in found})
    if summary.removed and not allow_removals:
        raise UserError(
            f"the new index would remove {len(summary.removed)} design(s) that the current "
            f"{INDEX_NAME} lists: {', '.join(summary.removed)}; nothing was written "
            "(--allow-removals to publish anyway)"
        )
    try:
        _preflight(archive, found, tmp)
    except OSError as e:
        raise _oserror(e) from e
    try:
        for entry, data in found:
            _write_bin(archive, entry["bin"], data, entry["bin_sha256"])
        _write_index_tmp(tmp, text)
        _run_check([*argv, str(tmp), "--bins", str(archive)])
        os.replace(tmp, final)
    except OSError as e:
        _discard(tmp)
        raise _oserror(e) from e
    except BaseException:
        _discard(tmp)
        raise
    return summary


def _refuse_constant(name: str):
    raise ValueError(f"{name} is not allowed")


MAX_INDEX = 16 << 20


def _read_index(path: Path) -> str:
    """The text of the existing index: a regular file (lstat), opened without following a symlink, of at most
    MAX_INDEX bytes. Other hard links to it (a backup snapshot) are fine: the index is replaced by renaming a new
    file over it, never written through. Anything wrong is a UserError naming the path."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise UserError(f"the current {path} is not a regular file; nothing was written")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise UserError(f"the current {path} is not a regular file; nothing was written")
            data = f.read(MAX_INDEX + 1)
        if len(data) > MAX_INDEX:
            raise UserError(f"the current {path} is larger than {MAX_INDEX} bytes; nothing was written")
        return data.decode()
    except UnicodeDecodeError as e:
        raise UserError(f"the current {path} is not valid UTF-8 ({e}); nothing was written") from e
    except OSError as e:
        raise UserError(f"{path}: cannot be read: {e.strerror or e}; nothing was written") from e


def _previous_ids(path: Path) -> set[str]:
    """The ids listed by the index at `path` (none if there is no index). An index that cannot be read, has
    NaN or Infinity, is not an object or lists something that is not an id is a UserError."""
    if not os.path.lexists(path):
        return set()
    try:
        data = json.loads(_read_index(path), parse_constant=_refuse_constant)
    except ValueError as e:
        raise UserError(f"the current {path} is not valid JSON ({e}); nothing was written") from e
    designs = data.get("designs") if isinstance(data, dict) else None
    schema = data.get("schema") if isinstance(data, dict) else None
    ids = (
        [d.get("id") if isinstance(d, dict) else None for d in designs] if isinstance(designs, list) else None
    )
    if (
        not isinstance(schema, int) or isinstance(schema, bool) or schema != 1
        or ids is None or not all(isinstance(i, str) for i in ids) or len(set(ids)) != len(ids)
    ):  # fmt: skip
        raise UserError(
            f"the current {path} is not a library index (an object with schema 1 and a designs list of"
            " objects with an id); nothing was written"
        )
    return set(ids)


def _discard(tmp: Path) -> None:
    try:
        if os.path.lexists(tmp) and not tmp.is_dir():
            os.unlink(tmp)
    except OSError:
        pass


def _cmd_publish(args: argparse.Namespace) -> int:
    try:
        designs = manifest.load(extras=manifest.EXTRAS_DEFAULT)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as e:
        raise UserError(f"the manifest could not be read: {type(e).__name__}: {e}") from e
    if args.only:
        wanted = {manifest.by_id(designs, i).id for i in args.only}
        designs = [d for d in designs if d.id in wanted]
    s = run(
        designs, Path(args.archive), args.check_cmd, profile=args.profile,
        media_root=Path(args.media_root) if args.media_root else None, allow_removals=args.allow_removals,
    )  # fmt: skip
    print(
        f"published {s.published} designs ({s.timing_not_met} timing not met), skipped {len(s.skipped)} -> {s.archive}"
    )
    for reason, n in sorted(collections.Counter(x["reason"] for x in s.skipped).items()):
        print(f"  {n} x {reason}")
    if args.list_skipped:
        for x in s.skipped:
            print(f"{x['id']}: {x['reason']}")
    if s.removed:
        print(f"removed from the index: {', '.join(s.removed)}")
    print(f"reserved demo names from tinytapeout-fpga-demos at {DEMO_NAMES_COMMIT}")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("publish", help="write fpga-library.json and the bitstreams into the video archive")
    p.add_argument("--archive", required=True, metavar="DIR", help="the archive directory (must exist)")
    p.add_argument(
        "--check-cmd", required=True, metavar="CMD",
        help="run as: CMD <archive>/fpga-library.json.tmp --bins <archive>; must exit 0",
    )  # fmt: skip
    p.add_argument(
        "--profile", default=DEFAULT_PROFILE, help=f"board profile file (default: {DEFAULT_PROFILE})"
    )
    p.add_argument("--only", action="append", metavar="ID", help="<shuttle>/<macro>; repeatable")
    p.add_argument(
        "--media-root",
        default=None,
        metavar="DIR",
        help="where the poster, GIF and video are (default: the archive)",
    )
    p.add_argument(
        "--allow-removals", action="store_true",
        help="publish even if the new index drops designs the current one lists (they are listed in the output)",
    )  # fmt: skip
    p.add_argument("--list-skipped", action="store_true", help="print every skipped design with its reason")
    p.set_defaults(func=_cmd_publish)

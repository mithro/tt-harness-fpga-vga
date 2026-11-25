# SPDX-License-Identifier: Apache-2.0
"""Export the built bitstreams as a demo index for the tinytapeout.fpgas.online demo package.

`tthfv demos` writes a directory with one `<name>.bin` per design (a byte copy of work/build/<id>/design.bin)
and an `index.json` whose `demos` entries have the fields the demo daemon reads (name, title, author,
description, docs_url, repo_url, clock_hz, pinout) followed by extra keys of ours. A design is exported only
if its build is `ok` or `timing-fail`, the bitstream matches its record, the build is current for the
manifest's commit, and a reference with at least one frame exists for the profile. Anything else is listed in
`skipped` with the reason. A design that missed timing is exported, with a warning in its title, description
and `warnings`.

The directory is built next to the target (`<out>.new`) and swapped in with renames, so a reader never sees a
half-written export and a failed run leaves the previous one in place.
"""

import argparse
import collections
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from tthfv import manifest, paths, safefile
from tthfv.errors import UserError
from tthfv.manifest import Design
from tthfv.reference import _PROFILE_NAME, SCRATCH
from tthfv.results import Invalid, _read, _videos_repo_commit

MAX_BITSTREAM = 262144
PREAMBLE = bytes.fromhex("7eaa997e")
PREAMBLE_WITHIN = 68  # the sync word must lie inside the first this-many bytes
NAME_MAX = 40
_NAME = re.compile(r"[a-z0-9_]{1,40}")
_STATUSES = ("ok", "timing-fail")
_PINOUT_KEYS = {"ui": "ui_in", "uo": "uo_out", "uio": "uio"}  # manifest key -> key the demo daemon reads


class Skip(Exception):
    """The design is not exported; the message is the reason listed in the index."""


@dataclass
class Summary:
    out: Path
    exported: int = 0
    timing_not_met: int = 0
    skipped: list[dict] = field(default_factory=list)


def demo_name(design_id: str) -> str:
    """The demo's name, `^[a-z0-9_]{1,40}$`, from its id `<shuttle>/<macro>`. Over 40 characters, the first
    33 of the candidate, `_` and the first 6 hex digits of the sha256 of the id (original case)."""
    candidate = re.sub(r"[^a-z0-9_]", "_", design_id.lower())
    if len(candidate) <= NAME_MAX:
        return candidate
    return candidate[:33] + "_" + hashlib.sha256(design_id.encode()).hexdigest()[:6]


def _base(repo: str) -> str:
    repo = repo.rstrip("/")
    repo = repo.removesuffix(".git")
    return repo.rstrip("/")


def _pinout(design: Design) -> dict:
    out = {}
    for port, key in _PINOUT_KEYS.items():
        names = [("" if n is None else str(n)) for n in (design.pinout.get(port) or [])][:8]
        out[key] = names + [""] * (8 - len(names))
    return out


def _mhz(hz: object) -> str:
    return f"{hz / 1e6:.3f}" if isinstance(hz, (int, float)) and not isinstance(hz, bool) else "unknown"


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
    return b, data


def _reference_record(work: Path, design: Design, profile: str, build: dict) -> dict:
    """The reference record, current for the manifest's commit and the build's clock and with at least one
    frame, or Skip. `frames_complete` and the length of `frames` (one entry per complete frame) must agree;
    if they do not the record is inconsistent."""
    base = f"reference/{design.id}/{profile}"
    try:
        ref = _read(work / base / "reference.json", work)
        fail = None if ref is not None else _read(work / base / "reference-fail.json", work)
    except Invalid as e:
        raise Skip(f"{base} record is unreadable or damaged") from e
    if ref is None:
        if fail is not None:
            raise Skip(f"reference failed: {fail.get('status')}")
        raise Skip("no reference")
    if ref.get("schema") != 3:
        raise Skip(f"reference.json has schema {ref.get('schema')!r}, not 3")
    complete = ref.get("frames_complete")
    frames = ref.get("frames")
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


def _entry(design: Design, b: dict, data: bytes, ref: dict, profile: str) -> dict:
    timing_fail = b["status"] == "timing-fail"
    clock_hz = b["clock_hz"]
    if not isinstance(clock_hz, int) or isinstance(clock_hz, bool):
        raise Skip("build record has no integer clock_hz")
    ui_in, uio_in = ref["ui_in_requested"], ref["uio_in_requested"]
    warnings = []
    if timing_fail:
        warnings.append(
            f"WARNING: timing not met: place-and-route estimates {_mhz(b.get('fmax_hz'))} MHz,"
            f" the design's clock is {_mhz(clock_hz)} MHz; not validated on hardware;"
            " it may glitch or fail on some boards."
        )
    if not design.clock_declared:
        warnings.append("clock not declared by the project; 25.175 MHz assumed")
    parts = [f"{design.shuttle} {design.macro}."]
    parts.append(warnings[0] if timing_fail else "Not yet validated on hardware.")
    if ui_in:
        parts.append(f"Set ui_in to 0x{ui_in:02X}.")
    where = f"/{design.subdir.strip('/')}" if design.subdir.strip("/") else ""
    repo = _base(design.repo)
    title = design.title or design.macro
    return {
        "name": demo_name(design.id),
        "title": title + (" (timing not met)" if timing_fail else ""),
        "author": design.author,
        "description": " ".join(parts),
        "docs_url": f"{repo}/blob/{design.commit}{where}/docs/info.md",
        "repo_url": f"{repo}/tree/{design.commit}{where}",
        "clock_hz": clock_hz,
        "pinout": _pinout(design),
        "id": design.id,
        "shuttle": design.shuttle,
        "macro": design.macro,
        "top_module": b.get("top_module_used"),
        "ui_in": ui_in,
        "uio_in": uio_in,
        "source_repo": design.repo,
        "source_commit": design.commit,
        "bin_sha256": hashlib.sha256(data).hexdigest(),
        "bin_bytes": len(data),
        "status": {
            "build": b["status"],
            "fmax_hz": b.get("fmax_hz"),
            "clock_declared": design.clock_declared,
            "reference": "ok",
            "reference_profile": profile,
            "frames": ref["frames_complete"],
            "mode": ref.get("mode"),
            "first_frame_hash": ref["frames"][0]["hash"],
            "hardware": None,
        },
        "warnings": warnings,
    }


def _tthfv_commit() -> str | None:
    """HEAD of this repository, or None if it is not a git checkout."""
    try:
        return manifest._head(paths.REPO_ROOT)
    except (OSError, UserError):
        return None


def _swap_in(staging: Path, out: Path) -> None:
    """Make the finished staging directory the export. A previous export (already checked by _check_out) is
    renamed to a name made for this run, and only that directory is removed at the end; no other name is
    deleted. If putting the new one in place fails, the previous one is moved back."""
    aside = None
    if os.path.lexists(out):
        aside = Path(tempfile.mkdtemp(dir=out.parent, prefix=f".{out.name}.old-"))
        try:
            os.rename(out, aside)  # onto the empty directory just made
        except BaseException:
            os.rmdir(aside)
            raise
    try:
        os.rename(staging, out)
    except BaseException:
        if aside is not None and not os.path.lexists(out):
            os.rename(aside, out)
        raise
    if aside is not None:
        safefile.remove_tree(aside)


def _check_out(out: Path, work: Path) -> None:
    """UserError unless `out` may be replaced: not the current directory, the work directory or an ancestor
    of either, not a symlink, and either absent, empty, or a previous export (an index.json made by this
    command and nothing but `*.bin` files beside it)."""
    here = Path(os.getcwd())
    for keep, what in ((work, "the work directory"), (here, "the current directory")):
        if out == keep or out in keep.parents:
            raise UserError(f"{out} is {what} or contains it; refusing to replace it")
    if out.is_symlink():
        raise UserError(f"{out} is a symbolic link; refusing to replace it")
    if not os.path.lexists(out):
        return
    if not out.is_dir():
        raise UserError(f"{out} exists and is not a directory")
    names = sorted(p.name for p in out.iterdir())
    if not names:
        return
    why = f"{out} is not a previous tthfv demos export; refusing to replace it"
    for name in names:
        if (name != "index.json" and not name.endswith(".bin")) or not safefile.is_plain(out / name):
            raise UserError(f"{why} ({name} is not an export file)")
    try:
        previous = json.loads(safefile.read_text(out / "index.json"))
    except (OSError, ValueError, safefile.NotPlainFile) as e:
        raise UserError(f"{why} (index.json is unreadable: {e})") from e
    if not isinstance(previous, dict) or previous.get("generated_by") != "tthfv demos":
        raise UserError(f"{why} (index.json was not made by tthfv demos)")


def export(
    designs: list[Design], work: Path | None = None, out: Path | None = None, profile: str = "ideal"
) -> Summary:
    """Write the export of `designs` to `out` (default demos/ in the work directory)."""
    work = Path(os.path.abspath(paths.WORK if work is None else work))
    out = work / "demos" if out is None else Path(os.path.abspath(out))
    if not _PROFILE_NAME.fullmatch(profile) or profile in (SCRATCH, "trash"):
        raise UserError(f"invalid profile name {profile!r}")
    _check_out(out, work)
    summary = Summary(out=out)
    entries: dict[str, tuple[dict, bytes]] = {}
    owner: dict[str, str] = {}
    for d in sorted(designs, key=lambda d: d.id):
        try:
            b, data = _build_record(work, d)
            ref = _reference_record(work, d, profile, b)
            entry = _entry(d, b, data, ref, profile)
            if not _NAME.fullmatch(entry["name"]):
                raise UserError(f"{d.id}: demo name {entry['name']!r} does not match {_NAME.pattern}")
        except Skip as s:
            summary.skipped.append({"id": d.id, "reason": str(s)})
            continue
        except (KeyError, TypeError, ValueError, IndexError) as e:
            summary.skipped.append({"id": d.id, "reason": f"records unusable: {type(e).__name__}: {e}"})
            continue
        name = entry["name"]
        if name in owner:
            raise UserError(f"{owner[name]} and {d.id} both get the demo name {name!r}")
        owner[name] = d.id
        entries[name] = (entry, data)
        summary.exported += 1
        summary.timing_not_met += b["status"] == "timing-fail"
    index = {
        "schema": 1,
        "generated_by": "tthfv demos",
        "tthfv_commit": _tthfv_commit(),
        "videos_repo_commit": _videos_repo_commit(),
        "profile": profile,
        "demos": [entries[n][0] for n in sorted(entries)],
        "skipped": summary.skipped,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=out.parent, prefix=f".{out.name}.new-"))  # a name nobody has
    try:
        os.chmod(staging, 0o755)  # mkdtemp makes it private; the export is for others to read
        for name, (_, data) in entries.items():
            safefile.write_file(staging / f"{name}.bin", data)
        safefile.write_file(staging / "index.json", json.dumps(index, indent=2) + "\n")
        _swap_in(staging, out)
    except BaseException:
        if staging.exists():  # not yet renamed into place
            safefile.remove_tree(staging)
        raise
    return summary


def _cmd_demos(args: argparse.Namespace) -> int:
    try:
        designs = manifest.load(extras=manifest.EXTRAS_DEFAULT)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as e:
        raise UserError(f"the manifest could not be read: {type(e).__name__}: {e}") from e
    if args.only:
        wanted = {manifest.by_id(designs, i).id for i in args.only}
        designs = [d for d in designs if d.id in wanted]
    out = Path(args.out) if args.out else paths.WORK / "demos"
    s = export(designs, out=out, profile=args.profile)
    print(
        f"exported {s.exported} designs ({s.timing_not_met} with timing not met), skipped {len(s.skipped)} -> {out}"
    )
    for reason, n in sorted(collections.Counter(x["reason"] for x in s.skipped).items()):
        print(f"  {n} x {reason}")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser(
        "demos", help="export the built bitstreams as a demo index (index.json and <name>.bin)"
    )
    p.add_argument("--out", default=None, help="directory to write (default: demos/ in the work directory)")
    p.add_argument("--only", action="append", metavar="ID", help="<shuttle>/<macro>; repeatable")
    p.add_argument("--profile", default="ideal", help="reference profile directory to read (default: ideal)")
    p.set_defaults(func=_cmd_demos)

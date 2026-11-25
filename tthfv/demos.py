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
from dataclasses import dataclass, field
from pathlib import Path

from tthfv import manifest, paths, safefile
from tthfv.errors import UserError
from tthfv.manifest import Design
from tthfv.results import Invalid, _read, _videos_repo_commit

MAX_BITSTREAM = 262144
PREAMBLE = bytes.fromhex("7eaa997e")
PREAMBLE_WITHIN = 68  # the sync word must lie inside the first this-many bytes
NAME_MAX = 40
_NAME = re.compile(r"^[a-z0-9_]{1,40}$")
_PROFILE = re.compile(r"^[a-z0-9_-]+$")
_STATUSES = ("ok", "timing-fail")
_PINS = ("ui", "uo", "uio")


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
    for port in _PINS:
        names = [("" if n is None else str(n)) for n in (design.pinout.get(port) or [])][:8]
        out[port] = names + [""] * (8 - len(names))
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


def _reference_record(work: Path, design: Design, profile: str) -> dict:
    """The reference record, which has at least one frame, or Skip."""
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
    if not isinstance(complete, int) or isinstance(complete, bool) or complete < 1 or not frames:
        raise Skip("reference has no frames (no sync)")
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
    """Make the finished staging directory the export. The old one is renamed aside first and removed last,
    so a failure part way leaves one of the two complete directories at `out` (or the old one at `.old`)."""
    old = out.with_name(out.name + ".old")
    safefile.remove_tree(old)
    if os.path.lexists(out):
        os.rename(out, old)
    try:
        os.rename(staging, out)
    except OSError:
        if os.path.lexists(old) and not os.path.lexists(out):
            os.rename(old, out)
        raise
    safefile.remove_tree(old)


def export(
    designs: list[Design], work: Path | None = None, out: Path | None = None, profile: str = "ideal"
) -> Summary:
    """Write the export of `designs` to `out` (default demos/ in the work directory)."""
    work = paths.WORK if work is None else work
    out = work / "demos" if out is None else Path(out)
    if not _PROFILE.match(profile):
        raise UserError(f"invalid profile name {profile!r}")
    if out.is_symlink():
        raise UserError(f"{out} is a symbolic link; refusing to replace it")
    summary = Summary(out=out)
    entries: dict[str, tuple[dict, bytes]] = {}
    owner: dict[str, str] = {}
    for d in sorted(designs, key=lambda d: d.id):
        try:
            b, data = _build_record(work, d)
            ref = _reference_record(work, d, profile)
            entry = _entry(d, b, data, ref, profile)
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
    staging = out.with_name(out.name + ".new")
    safefile.make_empty_dir(staging, work / "trash")
    try:
        for name, (_, data) in entries.items():
            safefile.write_file(staging / f"{name}.bin", data)
        safefile.write_file(staging / "index.json", json.dumps(index, indent=2) + "\n")
        _swap_in(staging, out)
    except BaseException:
        safefile.remove_tree(staging)
        raise
    return summary


def _cmd_demos(args: argparse.Namespace) -> int:
    try:
        designs = manifest.load(extras=manifest.EXTRAS_DEFAULT)
    except (OSError, ValueError) as e:
        raise UserError(f"the manifest could not be read: {e}") from e
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

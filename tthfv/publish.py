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
import functools
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

from tthfv import build as build_stage
from tthfv import frames, manifest, paths, pdkcells, reference, results, safefile
from tthfv.errors import UserError
from tthfv.fetch import FetchResult
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
HARDWARE_RECORD = paths.REPO_ROOT / "data" / "hardware" / "fpga-4.json"
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
    "ttihp26b", "ttsky26c", "ttgf0p3", "ttgf26b", "ttgf26a", "ttsky26b", "ttsky26a", "ttihp0p4", "ttihp26a",
    "ttgf0p2", "ttsky25b", "ttgf0p1", "ttsky25a", "ttihp25b", "ttcad25a", "ttihp0p3", "ttihp25a", "tt09",
    "ttihp0p2", "tt08", "ttihp0p1", "tt07", "tt06", "tt05of", "tt05", "tt04", "tt03p5", "tt03", "tt02",
)  # fmt: skip

_STATUSES = ("ok", "timing-fail")
_VALIDATIONS = ("none", "mcu")  # the format also allows "full-speed"; nothing sets it
_RECORD_KEYS = {"bin_sha256", "board", "cause", "date", "vgacap_revision", "verdict"}
_RECORD_VERDICTS = ("exact", "mismatch", "no-match")
_RECORD_CAUSES = ("synthesis differs", "clock before reset", "both", "unexplained")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DIFFERENT_SOURCES = "bitstream and reference were made from different sources"
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
    dirty: bool = False  # the repository had uncommitted changes (--allow-dirty)


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


def _uses_pdk_latches(b: dict) -> bool:
    """Whether the build used PDK latch or clock-gate cells. A latch or clock-gate cell maps to a LUT loop on the
    iCE40: the bitstream may not behave like the simulation, so the readers warn. A build that used PDK cells but
    did not record whether latches were among them counts as using them (the cautious value); a record without
    `pdk_cells` does not."""
    return b.get("pdk_latches") is True or bool(b.get("pdk_cells") and b.get("pdk_latches") is not False)


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


def _reference_record(
    work: Path, design: Design, profile: reference.Profile, build: dict, missing: str
) -> dict:
    """The reference record of a profile, current for the manifest's commit and the build's clock and with at
    least one frame, or Skip. `frames_complete` must equal the number of `frames` (one entry per complete
    frame); if not, the record is inconsistent."""
    base = f"reference/{design.id}/{profile.name}"
    try:
        ref = _read(work / base / "reference.json", work)
        fail = None if ref is not None else _read(work / base / "reference-fail.json", work)
    except Invalid as e:
        raise Skip(f"{base} record is unreadable or damaged") from e
    if ref is None:
        if fail is not None:
            raise Skip(f"reference failed under profile {profile.name}: {fail.get('status')}")
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
    # The inputs and the profile the reference was simulated under must be today's (the key the reference stage
    # writes, rebuilt here from the same stimulus_of): otherwise ui_in and the mask would be those of the past.
    stim = reference.stimulus_of(design, profile)
    if (
        key.get("ui_in") != stim.ui_req or key.get("uio_in") != stim.uio_requested
        or ref.get("ui_in_requested") != design.ui_in or ref.get("uio_in_requested") != design.uio_in
    ):  # fmt: skip
        raise Skip("stale reference (inputs changed)")
    if key.get("ties") != stim.ties or key.get("profile") != {"name": profile.name, "data": profile.data}:
        raise Skip("stale reference (board profile changed)")
    if key.get("clocks") != reference.DEFAULT_CLOCKS:
        # uio_oe_any, and so the mask, are only as good as the clocks that were simulated.
        raise Skip(
            f"reference simulated for {key.get('clocks')} clocks, not the default {reference.DEFAULT_CLOCKS}"
        )
    if any(key.get(name) != value for name, value in _simulation_code(design).items()):
        raise Skip("stale reference (simulation code changed)")
    if "pdk_cells" in key:
        # simulated with PDK cell models (reference._pdk_key): they must be today's models of the cells it names
        entry = key["pdk_cells"]
        plan = pdkcells.plan_named(entry.get("pdk_cells") or []) if isinstance(entry, dict) else None
        if plan is None or plan.key_fields() != entry:
            raise Skip("stale reference (PDK cell models changed)")
    return ref


@functools.cache
def _testbench_sha256() -> str:
    return hashlib.sha256(reference.TB.read_bytes()).hexdigest()


def _simulation_code(design: Design) -> dict:
    """The parts of reference.sim_key that say which simulation code made a reference: the testbench, the
    stimulus and frame-cutter versions and the Verilator flags. The Verilator version string is left out on
    purpose: a tool upgrade alone must not unpublish every design."""
    return {
        "testbench_sha256": _testbench_sha256(),
        "stimulus_version": reference.STIMULUS_VERSION,
        "cutter_version": frames.CUTTER_VERSION,
        "verilator_flags": [str(f) for f in design.override.get("verilator_flags") or []],
        "base_flags": list(reference._BASE_FLAGS),
    }


def _check_same_sources(work: Path, design: Design, b: dict, keys: list[dict]) -> dict | None:
    """Skip unless the bitstream and every reference were made from the same sources and top module (and from
    those of the fetch record, when there is one). Returns the fetch record, if any."""
    fetch = results._fetch_record(work, design)
    for rec, name in [(b, "build.json"), *[(k, "reference.json key") for k in keys]]:
        if (rec.get("sources_sha256"), rec.get("top_module_used")) != (
            b.get("sources_sha256"), b.get("top_module_used"),
        ) or results._differences(rec, name, design.commit, fetch):  # fmt: skip
            raise Skip(DIFFERENT_SOURCES)
    if fetch is not None and fetch.get("top_module_used") != b.get("top_module_used"):
        raise Skip(DIFFERENT_SOURCES)
    return fetch


def _check_build_inputs(design: Design, b: dict, fetch: dict | None) -> None:
    """Skip unless the build's inputs digest is the one the build stage computes today. The digest is rebuilt
    with build._inputs_digest from the manifest, the fetch record (its compilation units, top module and
    sources digest: the source tree itself is not needed), today's harness files (wrapper and pin file), and from
    the build record: its tool versions and limits (the toolchain is not run, so a tool upgrade is not seen
    here) and the default number of attempts."""
    if fetch is None:
        raise Skip("sources not available to verify the build")
    src = Path("/fetch-json")  # only used to turn the compilation units into the names the digest holds
    fetched = FetchResult(
        status="ok", src_dir=src, sources=(), include_dirs=(), log=src / "log",
        synth_sources=tuple(src / s for s in fetch["synth_sources"]), top_module_used=fetch["top_module_used"],
        sources_sha256=fetch["sources_sha256"],
    )  # fmt: skip
    wrapper, n = re.subn(
        rf"\b{build_stage.PLACEHOLDER}\b",
        fetched.top_module_used,
        (build_stage.HARNESS / "tt_fpga_top.v").read_text(),
    )
    if n != 1:
        raise Skip("the harness wrapper does not name the placeholder once")
    # A build that took a coverage rung (PDK cell models, the Verilog globals reset) carries the digest with it: the
    # models are today's models of the cells the record names, so a changed model no longer matches.
    plan = None
    if b.get("pdk_cells"):
        plan = pdkcells.plan_named(b["pdk_cells"]) if isinstance(b["pdk_cells"], list) else None
        if plan is None:
            raise Skip("the build's PDK cell models are not today's")
    digest = build_stage._inputs_digest(
        design, fetched, wrapper, b.get("tools"), build_stage.DEFAULT_MAX_ATTEMPTS, b.get("limits"),
        plan, b.get("reset_vlog") is True,
    )  # fmt: skip
    if digest != b.get("inputs_sha256"):
        raise Skip("bitstream built from different inputs than today's")


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
    if type(e["pdk_latches"]) is not bool:
        bad("pdk_latches", "a JSON boolean is required")
    if e["validation"] not in _VALIDATIONS:
        bad("validation", 'must be "none" or "mcu"')
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


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def load_hardware_record(path: Path | None = None) -> dict[str, dict]:
    """The records of the hardware run in `data/hardware/fpga-4.json`, by library id. Any problem with the file
    (missing, not JSON, wrong shape) is a UserError: a bad record must never quietly publish "none" everywhere."""
    path = HARDWARE_RECORD if path is None else path

    def bad(why: str) -> UserError:
        return UserError(f"hardware record {path.name}: {why}")

    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise bad(f"cannot be read: {e}") from e
    try:
        top = json.loads(text, parse_constant=_refuse_constant, object_pairs_hook=_no_duplicates)
    except ValueError as e:
        raise bad(f"is not valid JSON: {e}") from e
    if (
        not isinstance(top, dict)
        or set(top) != {"schema", "records"}
        or type(top["schema"]) is not int
        or top["schema"] != 1
    ):
        raise bad('must be an object with exactly "schema" (1) and "records"')
    records = top["records"]
    if not isinstance(records, dict):
        raise bad('"records" must be an object keyed by library id')
    for id, r in records.items():
        if not isinstance(r, dict) or set(r) != _RECORD_KEYS:
            raise bad(f"{id}: must be an object with exactly the keys {sorted(_RECORD_KEYS)}")
        if r["verdict"] not in _RECORD_VERDICTS:
            raise bad(f"{id}: verdict must be one of {', '.join(_RECORD_VERDICTS)}")
        if not isinstance(r["bin_sha256"], str) or not _SHA256.fullmatch(r["bin_sha256"]):
            raise bad(f"{id}: bin_sha256 must be 64 lower-case hex digits")
        if r["board"] != "fpga-4":
            raise bad(f"{id}: board must be fpga-4")
        if not isinstance(r["vgacap_revision"], str) or not _HEX40.fullmatch(r["vgacap_revision"]):
            raise bad(f"{id}: vgacap_revision must be 40 lower-case hex digits")
        if not isinstance(r["date"], str) or not _DATE.fullmatch(r["date"]):
            raise bad(f"{id}: date must be YYYY-MM-DD")
        if (r["verdict"] == "exact") != (r["cause"] is None) or (
            r["cause"] is not None and r["cause"] not in _RECORD_CAUSES
        ):
            raise bad(f"{id}: cause is null for exact, one of {', '.join(_RECORD_CAUSES)} otherwise")
    return records


def _validation(id: str, sha: str, hardware: dict[str, dict]) -> str:
    """The `validation` of an entry. THE RULE: an entry gets "mcu" only when data/hardware/fpga-4.json has a
    record for its id whose verdict is "exact" AND whose bin_sha256 equals the entry's bin_sha256. Otherwise
    it gets "none". So a rebuilt bitstream falls back to "none" until it is run on the board again."""
    r = hardware.get(id)
    return "mcu" if r is not None and r["verdict"] == "exact" and r["bin_sha256"] == sha else "none"


def _candidate(
    work: Path,
    design: Design,
    board: reference.Profile,
    media_root: Path,
    archive: Path,
    hardware: dict[str, dict],
) -> tuple[dict, bytes]:
    """The entry of one design and its bitstream, or Skip."""
    if design.shuttle == "calibration":
        raise Skip("calibration design")
    if design.qspi is not None:
        raise Skip("needs a QSPI Pmod")
    b, data = _build_record(work, design)
    ideal = _reference_record(work, design, reference.IDEAL, b, "no reference")
    ref = _reference_record(work, design, board, b, f"no reference under profile {board.name}")
    fetch = _check_same_sources(work, design, b, [ideal["key"], ref["key"]])
    _check_build_inputs(design, b, fetch)
    mask, requested = ref.get("ui_in_mask_used"), ref.get("ui_in_requested")
    if not _is_int(mask, 0, 255):
        raise Skip("invalid ui_in_mask: the board reference has no int from 0 to 255")
    if not _is_int(requested, 0, 255):
        raise Skip("invalid ui_in_requested: the board reference has no int from 0 to 255")
    # Guards against a bug in the reference stage: whatever else is fresh, never publish a mask that lets the
    # board drive a ui_in pin whose tied uio pin the design enables.
    enabled = ref.get("uio_oe_any")
    if (
        not _is_int(enabled, 0, 255)
        or mask & ~reference.final_mask(board, enabled) & 0xFF
        or mask & reference.stimulus_of(design, board).ties & enabled
    ):
        raise Skip("mask drives a tied pin the design enables")
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
        "pdk_latches": _uses_pdk_latches(b),
        "validation": _validation(id, sha, hardware),
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
    """Whether tracked files of this repository have uncommitted changes (untracked files do not count)."""
    return bool(manifest._git("status", "--porcelain", "--untracked-files=no", cwd=paths.REPO_ROOT).strip())


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
            f.flush()
            os.fsync(f.fileno())  # on disk before it gets its final name
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
        f.flush()
        os.fsync(f.fileno())


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
    allow_dirty: bool = False,
) -> Summary:
    """Publish `designs` into `archive`. Raises UserError, leaving the previous index in place, on any failure."""
    work = Path(os.path.abspath(paths.WORK if work is None else work))
    argv = shlex.split(check_cmd)
    if not argv:
        raise UserError("--check-cmd is empty")
    stem = Path(profile).stem
    if stem == "ideal" or not _PROFILE_NAME.fullmatch(stem) or stem in (SCRATCH, "trash"):
        raise UserError(f"--profile {profile}: the stem {stem!r} cannot name a board profile")
    spec = Path(profile)
    if not spec.is_absolute() and not spec.exists():
        spec = paths.REPO_ROOT / spec  # the default boards/fpga-4.json, from any directory
    board = reference.load_profile(
        str(spec)
    )  # the profile as it is now: references must have been made under it
    hardware = load_hardware_record()
    archive = _check_archive(archive)
    media = archive if media_root is None else Path(media_root)
    tthfv_commit, videos_commit = _tthfv_commit(), _videos_repo_commit()
    if not tthfv_commit or not _HEX40.fullmatch(tthfv_commit):
        raise UserError("the git HEAD of this repository could not be read; the index requires it")
    if not videos_commit or not _HEX40.fullmatch(videos_commit):
        raise UserError("the videos repository's commit could not be read; the index requires it")
    dirty = _tree_is_dirty()
    if dirty and not allow_dirty:
        raise UserError(
            "this repository has uncommitted changes to tracked files, so tthfv_commit would not describe the "
            "code that ran (--allow-dirty to publish anyway)"
        )

    summary = Summary(archive=archive, dirty=dirty)
    found: list[tuple[dict, bytes]] = []
    owner: dict[str, str] = {}
    for d in sorted(designs, key=lambda d: d.id):
        try:
            entry, data = _candidate(work, d, board, media, archive, hardware)
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
        _sync_dir(archive)
    except OSError as e:
        _discard(tmp)
        raise _oserror(e) from e
    except BaseException:
        _discard(tmp)
        raise
    return summary


def _sync_dir(path: Path) -> None:
    """Flush a directory's entries (the rename of the index) to disk; best effort."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


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
    except (ValueError, RecursionError) as e:  # RecursionError: nested too deeply for the decoder
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
    if args.only and args.allow_removals:
        raise UserError("--only with --allow-removals would drop every other design from an existing index")
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
        allow_dirty=args.allow_dirty,
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
    if s.dirty:
        print("WARNING: tthfv_commit does not describe a dirty tree")
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
    p.add_argument(
        "--allow-dirty", action="store_true",
        help="publish although tracked files have uncommitted changes (tthfv_commit then does not describe the code)",
    )  # fmt: skip
    p.add_argument("--list-skipped", action="store_true", help="print every skipped design with its reason")
    p.set_defaults(func=_cmd_publish)

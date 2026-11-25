# SPDX-License-Identifier: Apache-2.0
"""Compare a capture of the design's uo_out pins with a reference, clock for clock.

A capture is a 1-D uint8 array: sample i is uo_out after the rising edge of clock i, the same convention
as the reference dump (docs/frame-definition.md). The reference is a directory with reference.json
(schema 3) and the gzipped dump it names.

`compare` says exact, mismatch or refused. Everything else here is for diagnosing a mismatch: `find_offset`
looks for a constant shift, `diff_report` says where the first difference is, `write_diff_images` draws it.
"""

import argparse
import bisect
import gzip
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image

from tthfv import frames, safefile
from tthfv.errors import UserError

SCHEMA = 3
BUILD_FIELDS = ("sources_sha256", "commit", "top_module_used")  # build.json fields that must equal the key's
MIN_OVERLAP = 100_000  # find_offset: clocks that must overlap for an offset to count
_QUICK = 4096  # find_offset: compare this many clocks first, to reject a wrong offset cheaply


@dataclass(frozen=True)
class Comparison:
    """The result of compare. Clock numbers are indexes into the reference (clock 0 is the first reset
    clock). The capture clock compared with reference clock i is i + offset.

    verdict "exact": every reference clock has a capture clock and they are all equal; a longer capture is
    fine. "mismatch": some clock differs, or the capture has no clock for some reference clocks (those count
    in diff_clocks, the first of them can be first_diff_clock, and `reason` says so). "refused": nothing was
    compared (`reason` says why); the counts are then 0 except capture_clocks.

    diff_bits and per_bit_first_diff only cover clocks present in both streams.
    """

    verdict: Literal["exact", "mismatch", "refused"]
    reason: str
    compared_clocks: int
    capture_clocks: int
    reference_clocks: int
    first_diff_clock: int | None
    diff_clocks: int
    diff_bits: int  # OR of all differing bits, as a mask over uo_out
    per_bit_first_diff: dict[int, int]  # bit -> first clock at which it differs
    offset: int = 0  # the shift used: capture[i + offset] was compared with reference[i]


class Refused(Exception):
    """The comparison cannot be made; the message is the reason."""


@dataclass(frozen=True)
class Reference:
    meta: dict  # reference.json
    samples: np.ndarray  # the dump, uint8


def load_reference(reference_dir: Path) -> Reference:
    """Read and verify a reference directory. Raises Refused if reference.json is missing, unreadable or
    not schema 3, or if the dump is missing or its sha256 differs from dump_sha256."""
    reference_dir = Path(reference_dir)
    path = reference_dir / "reference.json"
    try:
        meta = json.loads(safefile.read_text(path))
    except (OSError, ValueError, safefile.NotPlainFile) as e:
        raise Refused(f"{path}: cannot read the reference: {e}") from e
    if not isinstance(meta, dict) or meta.get("schema") != SCHEMA:
        found = meta.get("schema") if isinstance(meta, dict) else None
        raise Refused(f"{path}: schema {found!r}, need {SCHEMA} (run `tthfv reference --force`)")
    try:
        raw = gzip.decompress(safefile.read_file(reference_dir / meta["dump"], 4 << 30))
    except (OSError, KeyError, EOFError, gzip.BadGzipFile, safefile.NotPlainFile) as e:
        raise Refused(f"{path}: cannot read the dump: {e!r}") from e
    if hashlib.sha256(raw).hexdigest() != meta.get("dump_sha256"):
        raise Refused(f"{reference_dir / meta['dump']}: sha256 differs from dump_sha256 in reference.json")
    if meta.get("clocks") != len(raw):
        raise Refused(f"{path}: clocks {meta.get('clocks')} but the dump has {len(raw)} bytes")
    return Reference(meta, np.frombuffer(raw, dtype=np.uint8))


def _check_build(build_json: Path, key: dict) -> None:
    try:
        build = json.loads(safefile.read_text(Path(build_json)))
    except (OSError, ValueError, safefile.NotPlainFile) as e:
        raise Refused(f"{build_json}: cannot read the build record: {e}") from e
    for field in BUILD_FIELDS:
        if field not in build or field not in key:
            raise Refused(
                f"{field} is missing from {'build.json' if field not in build else 'the reference key'}"
            )
        if build[field] != key[field]:
            raise Refused(f"build.json {field} {build[field]!r} differs from the reference's {key[field]!r}")


def _check_key(expect: dict, key: dict) -> None:
    for item, want in expect.items():
        if item not in key:
            raise Refused(f"reference key has no {item!r} (expected {want!r})")
        if key[item] != want:
            raise Refused(f"reference key {item} is {key[item]!r}, expected {want!r}")


def _overlap(n_capture: int, n_reference: int, offset: int) -> tuple[int, int]:
    """The reference clocks [lo, hi) that have a capture clock at i + offset."""
    lo = max(0, -offset)
    hi = min(n_reference, n_capture - offset)
    return (lo, hi) if hi > lo else (0, 0)


def _as_samples(capture) -> np.ndarray:
    arr = np.asarray(capture)
    if arr.dtype != np.uint8 or arr.ndim != 1:
        raise Refused(f"capture must be a 1-D uint8 array, got {arr.dtype} with shape {arr.shape}")
    return arr


def compare(
    capture: np.ndarray,
    reference_dir: Path,
    *,
    build_json: Path | None,
    offset: int = 0,
    expect_key: dict | None = None,
) -> Comparison:
    """Compare capture[i + offset] with reference[i] for every clock i of the reference.

    `offset` is for diagnosis (see find_offset). This function does not judge it: a capture that is equal
    at a non-zero offset is reported `exact`, with `offset` in the result. A caller that wants hardware
    captures to start on clock 0 must itself treat a non-zero offset as a failure.

    Refused when the reference is not usable (see load_reference), when `build_json` is given and its
    sources_sha256, commit or top_module_used differ from the reference's key, or when `expect_key` is given
    and any of its items differs from the item of the same name in the reference's key (the first one is
    named; use it to say which stimulus profile the capture was made with).
    """
    n_capture = len(capture) if hasattr(capture, "__len__") else 0
    n_reference = 0
    try:
        ref = load_reference(reference_dir)
        n_reference = len(ref.samples)
        key = ref.meta.get("key")
        if not isinstance(key, dict):
            raise Refused("reference.json has no key")
        if build_json is not None:
            _check_build(build_json, key)
        if expect_key:
            _check_key(expect_key, key)
        cap = _as_samples(capture)
    except Refused as e:
        return Comparison("refused", str(e), 0, n_capture, n_reference, None, 0, 0, {}, offset)
    return _compare_arrays(cap, ref.samples, offset)


def _compare_arrays(cap: np.ndarray, ref: np.ndarray, offset: int) -> Comparison:
    lo, hi = _overlap(len(cap), len(ref), offset)
    n = hi - lo
    xor = cap[lo + offset : hi + offset] ^ ref[lo:hi]
    differing = np.flatnonzero(xor)
    first = lo + int(differing[0]) if len(differing) else None
    bits = int(np.bitwise_or.reduce(xor[differing])) if len(differing) else 0
    per_bit = {}
    for b in range(8):
        if bits >> b & 1:
            per_bit[b] = lo + int(np.argmax((xor >> b) & 1))
    missing = len(ref) - n
    reason = ""
    if missing:  # reference clocks the capture does not cover, before and/or after the overlap
        gap = 0 if lo > 0 or n == 0 else hi
        first = gap if first is None else min(first, gap)
        reason = f"the capture covers reference clocks {lo} to {hi} only; {missing} of {len(ref)} are missing"
        if hi < len(ref) and lo == 0:
            reason = f"the capture ends at reference clock {hi}, before the reference's {len(ref)} clocks"
    count = len(differing) + missing
    verdict = "exact" if count == 0 else "mismatch"
    return Comparison(verdict, reason, n, len(cap), len(ref), first, count, bits, per_bit, offset)


def find_offset(capture: np.ndarray, reference: np.ndarray, max_offset: int = 64) -> int | None:
    """Diagnosis: the constant shift at which capture and reference agree, or None.

    Searches offsets in [-max_offset, max_offset] for one at which capture[i + offset] == reference[i] over
    the whole overlap; a positive offset means the capture has that many extra clocks at the start. The
    overlap must be at least 100,000 clocks (or the whole of the shorter array, if that is smaller). If
    several offsets match (a static signal), the one with the smallest absolute value wins, positive
    before negative.
    """
    cap, ref = np.asarray(capture, np.uint8), np.asarray(reference, np.uint8)
    need = min(MIN_OVERLAP, len(cap), len(ref))
    if need == 0:
        return None
    for magnitude in range(max_offset + 1):
        for offset in (0,) if magnitude == 0 else (magnitude, -magnitude):
            lo, hi = _overlap(len(cap), len(ref), offset)
            if hi - lo < need:
                continue
            a, b = cap[lo + offset : hi + offset], ref[lo:hi]
            if np.array_equal(a[:_QUICK], b[:_QUICK]) and np.array_equal(a, b):
                return offset
    return None


def _locate(clock: int, meta: dict) -> dict:
    """Which frame, line and pixel of the reference a clock falls in, from its geometry."""
    starts = [f["start_clock"] for f in meta.get("frames", [])]
    line_clocks, lines = meta.get("line_clocks"), meta.get("lines")
    i = bisect.bisect_right(starts, clock) - 1
    if i < 0 or not line_clocks or not lines or clock >= starts[i] + lines * line_clocks:
        return {"frame": None, "region": "outside frames"}
    row, col = divmod(clock - starts[i], line_clocks)
    _, first_pixel, first_active, width, height = frames.MODES.get(
        (line_clocks, lines), (None, 0, 0, line_clocks, lines)
    )
    y, x = row - first_active, col - first_pixel
    if 0 <= y < height and 0 <= x < width:
        return {"frame": meta["frames"][i]["index"], "region": "active", "line": y, "pixel": x}
    return {
        "frame": meta["frames"][i]["index"], "region": "blanking", "raster_line": row, "raster_clock": col,
    }  # fmt: skip


def diff_report(
    capture: np.ndarray, reference: np.ndarray, comparison: Comparison, *, reference_meta: dict | None = None
) -> dict:
    """Describe the differences of a mismatching `comparison` (made with the same arrays).

    Keys: verdict, offset, first_diff_clock, diff_clocks (as in the Comparison), bit_histogram (bit -> number
    of compared clocks at which it differs; bits that never differ are absent), first_diffs (the first 16
    differing clocks as {clock, reference, capture}), and, when reference_meta (reference.json) is given,
    location: {frame, region: "active", line, pixel} for a clock in a frame's active picture (line and pixel
    count from the picture's top left), {frame, region: "blanking", raster_line, raster_clock} for a clock in a
    frame's blanking, or {frame: None, region: "outside frames"}.
    """
    report: dict = {
        "verdict": comparison.verdict,
        "offset": comparison.offset,
        "first_diff_clock": comparison.first_diff_clock,
        "diff_clocks": comparison.diff_clocks,
        "reason": comparison.reason,
    }
    if comparison.verdict != "mismatch":
        return report
    cap, ref = np.asarray(capture, np.uint8), np.asarray(reference, np.uint8)
    lo, hi = _overlap(len(cap), len(ref), comparison.offset)
    xor = cap[lo + comparison.offset : hi + comparison.offset] ^ ref[lo:hi]
    differing = np.flatnonzero(xor)
    report["bit_histogram"] = {
        b: int(np.count_nonzero((xor[differing] >> b) & 1)) for b in range(8) if comparison.diff_bits >> b & 1
    }
    report["first_diffs"] = [
        {"clock": lo + int(i), "reference": int(ref[lo + i]), "capture": int(cap[lo + i + comparison.offset])}
        for i in differing[:16]
    ]
    if reference_meta is not None and comparison.first_diff_clock is not None:
        report["location"] = _locate(comparison.first_diff_clock, reference_meta)
    return report


def _nth_frame(samples: np.ndarray, index: int) -> frames.Frame | None:
    for f in frames.iter_frames(samples, hash=False):
        if f.index == index:
            return f
    return None


def _nearest_frame(samples: np.ndarray, start_clock: int) -> frames.Frame | None:
    """The frame of `samples` whose start is closest to start_clock."""
    best = None
    for f in frames.iter_frames(samples, hash=False):
        if best is None or abs(f.start_clock - start_clock) < abs(best.start_clock - start_clock):
            best = f
        if f.start_clock >= start_clock:
            break
    return best


def _png(frame: frames.Frame, path: Path) -> None:
    Image.frombytes("RGB", (frame.width, frame.height), frame.rgb888).save(path)


def write_diff_images(capture: np.ndarray, reference_dir: Path, out_dir: Path, *, offset: int = 0) -> dict:
    """Write reference.png, capture.png and difference.png (differing pixels white on black) of the frame
    containing the first difference (frame 0 if there is none; the next frame if the difference is in no
    frame), cutting both streams with the frame cutter. The capture's frame is the one that starts nearest
    to the reference frame's start clock plus `offset`.

    Returns {"frame", "capture_frame", "files": {name: path}, "note"}. When something cannot be drawn (the
    capture has no frames, or its frame has a different size) `note` says so and the files that could be
    written still are; this does not raise. Raises Refused if the reference cannot be loaded.
    """
    ref = load_reference(reference_dir)
    cap = _as_samples(capture)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result: dict = {"frame": None, "capture_frame": None, "files": {}, "note": None}
    listed = ref.meta.get("frames", [])
    if not listed:
        result["note"] = "the reference has no frames"
        return result
    c = _compare_arrays(cap, ref.samples, offset)
    target = 0
    if c.first_diff_clock is not None:
        starts = [f["start_clock"] for f in listed]
        i = bisect.bisect_right(starts, c.first_diff_clock) - 1
        span = ref.meta["lines"] * ref.meta["line_clocks"]
        inside = i >= 0 and c.first_diff_clock < starts[i] + span
        target = i if inside else min(i + 1, len(listed) - 1)
    result["frame"] = listed[target]["index"]
    ref_frame = _nth_frame(ref.samples, result["frame"])
    if ref_frame is None:
        result["note"] = "the reference could not be cut into frames again"
        return result
    _png(ref_frame, out / "reference.png")
    result["files"]["reference"] = str(out / "reference.png")
    cap_frame = _nearest_frame(cap, ref_frame.start_clock + offset)
    if cap_frame is None:
        result["note"] = "the capture has no frames (no sync found), so only the reference frame is written"
        return result
    result["capture_frame"] = cap_frame.index
    _png(cap_frame, out / "capture.png")
    result["files"]["capture"] = str(out / "capture.png")
    if (cap_frame.width, cap_frame.height) != (ref_frame.width, ref_frame.height):
        result["note"] = (
            f"the capture's frame is {cap_frame.width}x{cap_frame.height}, the reference's is "
            f"{ref_frame.width}x{ref_frame.height}: no difference image"
        )
        return result
    shape = (ref_frame.height, ref_frame.width, 3)
    a = np.frombuffer(ref_frame.rgb888, np.uint8).reshape(shape)
    b = np.frombuffer(cap_frame.rgb888, np.uint8).reshape(shape)
    diff = np.where((a != b).any(axis=2, keepdims=True), 255, 0).astype(np.uint8)
    Image.fromarray(np.broadcast_to(diff, shape).copy(), "RGB").save(out / "difference.png")
    result["files"]["difference"] = str(out / "difference.png")
    return result


def _cmd_compare(args: argparse.Namespace) -> int:
    cap_path = Path(args.capture)
    try:
        capture = frames.load_dump(cap_path)
    except (OSError, EOFError, gzip.BadGzipFile) as e:
        raise UserError(f"{cap_path}: cannot read the capture: {e}") from e
    c = compare(capture, args.reference, build_json=args.build_json, offset=args.offset)
    sizes = (
        f"{c.compared_clocks} clocks compared (capture {c.capture_clocks}, reference {c.reference_clocks})"
    )
    if c.verdict == "refused":
        print(f"refused: {c.reason}")
        return 1
    if c.verdict == "exact":
        print(f"exact: {sizes}, offset {c.offset}")
        if c.offset:
            print("a capture that is exact only at a non-zero offset is not a pass")
        return 1 if c.offset else 0
    print(f"mismatch: {sizes}, offset {c.offset}")
    if c.reason:
        print(c.reason)
    ref = load_reference(args.reference)  # compare() has just loaded it, so this succeeds
    rep = diff_report(capture, ref.samples, c, reference_meta=ref.meta)
    print(
        f"first differing clock {c.first_diff_clock}, {c.diff_clocks} clocks differ, bits {c.diff_bits:#04x}"
    )
    if "location" in rep:
        print(f"location: {rep['location']}")
    print(f"bit histogram: {rep['bit_histogram']}")
    print(f"first differing clock per bit: {c.per_bit_first_diff}")
    for d in rep["first_diffs"]:
        print(f"  clock {d['clock']}: reference {d['reference']:#04x} capture {d['capture']:#04x}")
    if not args.offset:
        found = find_offset(capture, ref.samples)
        print(f"find_offset: {found if found is not None else 'none'}")
    if args.diff_dir is not None:
        images = write_diff_images(capture, args.reference, args.diff_dir, offset=args.offset)
        for name, path in images["files"].items():
            print(f"{name}: {path}")
        if images["note"]:
            print(images["note"])
    return 1


def add_parser(sub) -> None:
    p = sub.add_parser("compare", help="compare a capture of uo_out with a reference, clock for clock")
    p.add_argument("capture", type=Path, help="raw bytes, one per clock, or a .gz of them")
    p.add_argument(
        "reference",
        type=Path,
        help="reference directory (work/reference/<shuttle>/<macro>/<profile>, ideal or a board profile; a schema-2 reference from before profiles is not readable until it is made again with `tthfv reference`)",
    )
    p.add_argument(
        "--build-json", type=Path, default=None, help="refuse if this build does not match the reference"
    )
    p.add_argument(
        "--offset", type=int, default=0, help="diagnosis: compare capture[i + N] with reference[i]"
    )
    p.add_argument("--diff-dir", type=Path, default=None, help="on a mismatch, write diff images here")
    p.set_defaults(func=_cmd_compare)

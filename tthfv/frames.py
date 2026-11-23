# SPDX-License-Identifier: Apache-2.0
"""Frames and hashes: cut a per-clock uo_out dump into VGA frames. See docs/frame-definition.md.

Nothing here knows about the command line or the filesystem layout beyond write_reference.
"""

import gzip
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

from tthfv.errors import UserError
from tthfv.manifest import Design

if TYPE_CHECKING:
    from tthfv.reference import SimResult

FNV_OFFSET = 0xCBF29CE484222325
# The videos repo's tb.cpp has this in its fnv1a: the real offset with its last decimal digit missing.
VIDEOS_REPO_FNV_OFFSET = 0x14650FB0739D0383
_FNV_PRIME = 0x100000001B3
_MASK = (1 << 64) - 1

HSYNC_BIT = 7
VSYNC_BIT = 3
RESET_CLOCKS = 10  # the reference testbench holds rst_n low for clocks 0-9
CUTTER_VERSION = 1  # bump when the frame cutting or hashing rules change; part of the reference key

# (line_clocks, lines) -> (name, first active pixel, first active line, width, height)
MODES: dict[tuple[int, int], tuple[str, int, int, int, int]] = {
    (800, 525): ("640x480@60", 48, 33, 640, 480),
    (1056, 628): ("800x600@60", 88, 23, 800, 600),
    (1344, 806): ("1024x768@60", 160, 29, 1024, 768),
}


def frame_hash(rgb888: bytes, offset: int = FNV_OFFSET) -> int:
    """FNV-1a 64-bit hash of the bytes. Only the standard offset gives a frame hash; the parameter exists
    so a hash made with VIDEOS_REPO_FNV_OFFSET can be reproduced."""
    h = offset
    for b in rgb888:
        h = ((h ^ b) * _FNV_PRIME) & _MASK
    return h


@dataclass(frozen=True)
class Frame:
    index: int
    start_clock: int  # index into the samples of the frame's first line start
    width: int
    height: int
    rgb888: bytes
    hash: int


@dataclass(frozen=True)
class FrameSet:
    mode: str | None  # e.g. "640x480@60"; None when not in the table
    line_clocks: int | None
    lines: int | None
    hsync_active_low: bool | None
    vsync_active_low: bool | None
    width: int
    height: int
    frames: list[Frame]  # complete frames only
    partial_frames_dropped: int


def _active_level(bit: np.ndarray) -> int | None:
    """The pulse level of a sync pin: the level whose runs have the shorter median length.

    The dump is split into runs of constant level; the first and last run are cut off by the ends of the
    dump and ignored. None if either level has no interior run or the two medians are equal.
    """
    change = np.flatnonzero(np.diff(bit)) + 1  # first index of every run after the first
    bounds = np.concatenate([[0], change, [len(bit)]])
    lengths = np.diff(bounds)[1:-1]
    levels = bit[bounds[1:-2]]  # level of each interior run
    high, low = lengths[levels == 1], lengths[levels == 0]
    if len(high) == 0 or len(low) == 0:
        return None
    med_high, med_low = np.median(high), np.median(low)
    if med_high == med_low:
        return None
    return 1 if med_high < med_low else 0


def _ends_of_pulses(samples: np.ndarray, bit: int, level: int) -> np.ndarray:
    """Indices of the samples where the pin returns from `level` (active) to the other level."""
    active = ((samples >> bit) & 1) == level
    return np.flatnonzero(active[:-1] & ~active[1:]) + 1


def _mode(values: np.ndarray) -> int | None:
    """Most common value; the smallest of several equally common ones."""
    if len(values) == 0:
        return None
    uniq, counts = np.unique(values, return_counts=True)
    return int(uniq[np.argmax(counts)])


def _rgb888(pix: np.ndarray) -> np.ndarray:
    """uint8 array (..., 3) of R, G, B = 85 * the 2-bit channel values of the pin bytes."""
    p = pix.astype(np.uint8)
    r = ((p & 1) << 1) | ((p >> 4) & 1)
    g = (((p >> 1) & 1) << 1) | ((p >> 5) & 1)
    b = (((p >> 2) & 1) << 1) | ((p >> 6) & 1)
    return (np.stack([r, g, b], axis=-1) * 85).astype(np.uint8)


def cut_frames(samples: np.ndarray, max_frames: int | None = None) -> FrameSet:
    """Cut a 1-D uint8 array of uo_out samples (one per clock) into complete VGA frames.

    With max_frames, stop after that many complete frames; partial_frames_dropped then only counts the
    incomplete frames met before the last one returned.
    """
    samples = np.asarray(samples, dtype=np.uint8).reshape(-1)
    n = len(samples)

    def none(**kw) -> FrameSet:
        base = {
            "mode": None, "line_clocks": None, "lines": None,
            "hsync_active_low": None, "vsync_active_low": None,
            "width": 0, "height": 0, "frames": [], "partial_frames_dropped": 0,
        }  # fmt: skip
        return FrameSet(**{**base, **kw})

    if n < 2:
        return none()
    h_level = _active_level((samples >> HSYNC_BIT) & 1)
    v_level = _active_level((samples >> VSYNC_BIT) & 1)
    h_low = None if h_level is None else h_level == 0
    v_low = None if v_level is None else v_level == 0
    found = {"hsync_active_low": h_low, "vsync_active_low": v_low}
    if h_level is None:  # without the line rate there is nothing more to measure
        return none(**found)

    line_starts = _ends_of_pulses(samples, HSYNC_BIT, h_level)
    line_clocks = _mode(np.diff(line_starts))
    found["line_clocks"] = line_clocks
    if line_clocks is None or v_level is None:
        return none(**found)
    # A frame starts on the first line start at or after the end of a vsync pulse.
    vsync_ends = _ends_of_pulses(samples, VSYNC_BIT, v_level)
    first_line = np.unique(np.searchsorted(line_starts, vsync_ends))  # indices into line_starts
    first_line = first_line[first_line < len(line_starts)]
    lines = _mode(np.diff(first_line))
    if len(first_line) < 3 or lines is None or lines < 2:
        return none(lines=lines, **found)

    mode = MODES.get((line_clocks, lines))
    name, first_pixel, first_active, width, height = mode if mode else (None, 0, 0, line_clocks, lines)

    good = np.diff(line_starts) == line_clocks  # good[i]: line i is exactly line_clocks long
    good_sum = np.concatenate([[0], np.cumsum(good)])
    frame_list: list[Frame] = []
    dropped = 0
    for k, li in enumerate(first_line.tolist()):
        if max_frames is not None and len(frame_list) >= max_frames:
            break
        start = int(line_starts[li])
        if k + 1 < len(first_line):
            # the next frame must start exactly `lines` line starts later, all lines the right length
            complete = int(first_line[k + 1]) - li == lines and good_sum[li + lines] - good_sum[li] == lines
        else:
            # last frame: lines-1 inner lines, the last line must lie wholly inside the dump
            complete = (
                len(line_starts) - li == lines
                and good_sum[li + lines - 1] - good_sum[li] == lines - 1
                and start + lines * line_clocks <= n
            )
        if not complete:
            dropped += 1
            continue
        raster = samples[start : start + lines * line_clocks].reshape(lines, line_clocks)
        pix = raster[first_active : first_active + height, first_pixel : first_pixel + width]
        rgb = _rgb888(pix).tobytes()
        frame_list.append(Frame(len(frame_list), start, width, height, rgb, frame_hash(rgb)))
    return FrameSet(
        mode=name, line_clocks=line_clocks, lines=lines, hsync_active_low=h_low, vsync_active_low=v_low,
        width=width, height=height, frames=frame_list, partial_frames_dropped=dropped,
    )  # fmt: skip


def load_dump(path: Path) -> np.ndarray:
    """Read a uo_out dump, plain or gzipped, as a 1-D uint8 array."""
    path = Path(path)
    data = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    return np.frombuffer(data, dtype=np.uint8)


def _gzip_with_sha256(src: Path, dst: Path) -> str:
    """Write src gzipped (level 6, no name, mtime 0) to dst, by rename; return the sha256 of src's bytes."""
    digest = hashlib.sha256()
    tmp = dst.with_name(dst.name + ".tmp")
    with (
        src.open("rb") as r,
        tmp.open("wb") as f,
        gzip.GzipFile(filename="", fileobj=f, mode="wb", compresslevel=6, mtime=0) as z,
    ):
        while chunk := r.read(1 << 20):
            digest.update(chunk)
            z.write(chunk)
    tmp.rename(dst)
    return digest.hexdigest()


def _sha256_of_gz(src: Path) -> str:
    digest = hashlib.sha256()
    with gzip.open(src, "rb") as r:
        while chunk := r.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _compress(plain: Path) -> tuple[str, str]:
    """Gzip dir/name to dir/name.gz and remove the plain file; or, if only the .gz exists, just hash it.
    Returns (name of the gz file, sha256 of the uncompressed bytes)."""
    gz = plain.with_name(plain.name + ".gz")
    if plain.exists():
        sha = _gzip_with_sha256(plain, gz)
        plain.unlink()
    elif gz.exists():
        sha = _sha256_of_gz(gz)
    else:
        raise UserError(f"{plain}: no dump to write the reference from")
    return gz.name, sha


def write_reference(design: Design, sim: "SimResult", frames: FrameSet, n: int = 8) -> Path:
    """Write work/reference/<id>/reference.json (last, by rename), PNGs of the first n frames, and gzip
    the dumps. reference.json lists every complete frame (index, start_clock, hash; `png` only for the
    first n). Returns the path of reference.json."""
    out = Path(sim.dump).parent
    ref = out / "reference.json"
    ref.unlink(missing_ok=True)
    try:
        stamp = json.loads((out / "sim.json").read_text())
    except (OSError, ValueError) as e:
        raise UserError(f"{out / 'sim.json'}: cannot read the simulation record: {e}") from e
    for stale in out.glob("frame-*.png"):
        stale.unlink()

    dump_name, dump_sha = _compress(out / "uo_out.bin")
    uio_name, uio_sha = _compress(out / "uio.bin")
    listed = []
    for f in frames.frames:
        entry = {"index": f.index, "start_clock": f.start_clock, "hash": f"{f.hash:016x}"}
        if f.index < n:  # a PNG for the first n frames only; every frame has its hash and start clock
            png = f"frame-{f.index:03d}.png"
            Image.frombytes("RGB", (f.width, f.height), f.rgb888).save(out / png)
            entry["png"] = png
        listed.append(entry)
    record = {
        "schema": 2,
        "id": design.id,
        "clock_hz": design.clock_hz,
        "clock_declared": design.clock_declared,
        "reset_clocks": RESET_CLOCKS,
        "ui_in": stamp["key"]["ui_in"],
        "uio_in": stamp["key"]["uio_in"],
        "clocks": sim.clocks,
        "dump": dump_name,
        "dump_sha256": dump_sha,
        "uio_dump": uio_name,
        "uio_dump_sha256": uio_sha,
        "mode": frames.mode,
        "line_clocks": frames.line_clocks,
        "lines": frames.lines,
        "hsync_active_low": frames.hsync_active_low,
        "vsync_active_low": frames.vsync_active_low,
        "width": frames.width,
        "height": frames.height,
        "frames_complete": len(frames.frames),
        "frames": listed,
        "partial_frames_dropped": frames.partial_frames_dropped,
        "tools": {"verilator": stamp["key"]["verilator"]},
        "key": stamp["key"],
        "key_sha256": stamp["key_sha256"],
    }
    tmp = out / "reference.json.tmp"
    tmp.write_text(json.dumps(record, indent=2) + "\n")
    tmp.rename(ref)
    return ref

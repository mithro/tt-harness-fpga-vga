# SPDX-License-Identifier: Apache-2.0
"""Read a capture made by vgacap's `ttcap capture` (a `.vgacap` stream) into one uo_out byte per project clock.

The format and its reader belong to vgacap (https://github.com/mithro/vgacap, `vgacap.stream.read_stream`);
this module only checks that the stream is what the comparator needs and turns it into the array `compare`
takes. A streaming capture (`ttcap capture --mode stream`, the board program `capture_rp2.py`) is one RAW
sample per project clock edge for as long as the board keeps up; RLE and version 2 RUNS chunks are runs of
such samples and expand losslessly. When the board falls behind it says so in the stream, with a TIME item
whose dropped count is above 0 (message "overrun", written straight after the chunk it damaged), and again in
the closing TIME summary ("overruns=N rxstall=R ..."). Any such record is refused here (CaptureGap): a stream
with a gap is never stitched into one array.

What the stream does not say: where its first sample is relative to the design's reset release. ttcap holds
the design in reset, starts the clock and then starts the sampler over the REPL, so the first sample comes an
unknown number of clocks after the release (`first_sample_clock` is None). The comparison locates it.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from vgacap.stream import CLOCK_BIT_NONE, TINYVGA_MAP, read_chunks, read_stream

from tthfv import safefile
from tthfv.errors import UserError

MAX_CAPTURE_BYTES = 4 << 30
MODE_EXTCLK = 0  # VGCH mode: sampled on the project's external clock edge (vgacap include/vgacap/stream.h)
SIGNALS = ("HSYNC", "VSYNC", "R1", "R0", "G1", "G0", "B1", "B0")  # signal_map's order (VGACAP_SIG_*)
SIG_ABSENT = 0xFF
# Chunks that do not carry one sample per clock from the start of the stream: whole-frame and window capture
# (FRAM), simulator change events (EVNT).
_REFUSED_TAGS = {"FRAM": "whole-frame or window capture", "EVNT": "change events"}


class CaptureError(UserError):
    """The capture file cannot be used: unreadable, malformed, or not one sample per project clock."""


class CaptureGap(CaptureError):
    """The stream records lost samples (an overrun, a dropped count, an RX FIFO stall)."""


@dataclass(frozen=True)
class CaptureData:
    uo_out: np.ndarray  # uint8, one per project clock, in capture order
    samples: int  # len(uo_out)
    clock_hz: int  # the header's project clock (0: unknown)
    design: str | None  # desc project=: the tt.shuttle entry ttcap enabled
    board: str | None  # desc board=, only if the capture's --desc gave it
    board_profile: str | None  # desc profile=: ttcap's board profile (rp2350-dbv3, rp2040-tt06map)
    edge: str | None  # desc edge=: the clock edge the board sampled on (falling: the low phase)
    desc: str  # the header's whole desc line
    summary: str | None  # the closing TIME message, None if the stream has none (a capture cut short)
    overruns: int | None  # from the summary
    rxstall: int | None  # from the summary
    sysclk_hz: int | None  # from the summary
    dropped: int  # the last TIME item's cumulative dropped count (0 if none)
    time_messages: tuple[str, ...]  # every TIME message, in order
    # Clocks between reset release and the first sample. The stream does not record it, so this is None.
    first_sample_clock: int | None = None


def _desc_fields(desc: str) -> dict[str, str]:
    out = {}
    for token in desc.split():
        key, eq, value = token.partition("=")
        if eq and key not in out:
            out[key] = value
    return out


def _keyed_int(msg: str, key: str) -> int | None:
    v = _desc_fields(msg).get(key)
    return int(v) if v is not None and v.isdigit() else None


def _to_uo_out(values: np.ndarray, signal_map: tuple, sample_bits: int, path: Path) -> np.ndarray:
    """Samples in the board's GPIO window to uo_out bytes: signal k is sample bit signal_map[k] and uo_out bit
    TINYVGA_MAP[k] (the Tiny VGA pinout the reference's bytes use)."""
    for name, bit in zip(SIGNALS, signal_map):
        if bit == SIG_ABSENT or bit >= sample_bits:
            raise CaptureError(f"{path}: signal {name} is not in the samples (signal_map {list(signal_map)})")
    if tuple(signal_map) == TINYVGA_MAP and sample_bits == 8:
        return values.astype(np.uint8)
    out = np.zeros(len(values), np.uint8)
    for k, bit in enumerate(signal_map):
        out |= (((values >> np.uint64(bit)) & np.uint64(1)).astype(np.uint8)) << np.uint8(TINYVGA_MAP[k])
    return out


def load_capture(path: Path) -> CaptureData:
    """Read a .vgacap capture. Raises CaptureError if it cannot be read or is not one sample per project clock,
    CaptureGap (a CaptureError) if it records any lost samples, naming where."""
    path = Path(path)
    try:
        data = safefile.read_file(path, MAX_CAPTURE_BYTES)
    except (OSError, safefile.NotPlainFile) as e:
        raise CaptureError(f"{path}: cannot read the capture: {e}") from e
    try:
        tags = [tag for tag, _ in read_chunks(data, check_min=False)]
        header, items = read_stream(data)
    except ValueError as e:
        raise CaptureError(f"{path}: not a well-formed vgacap stream: {e}") from e
    if tags.count("VGCH") != 1:
        raise CaptureError(f"{path}: {tags.count('VGCH')} VGCH headers; a capture has one")
    for tag, what in _REFUSED_TAGS.items():
        if tag in tags:
            raise CaptureError(f"{path}: has {tag} chunks ({what}), not one sample per project clock")
    if header.mode != MODE_EXTCLK:
        raise CaptureError(
            f"{path}: header mode {header.mode}, not {MODE_EXTCLK} (sampled on the project clock)"
        )
    if header.samples_per_clock != 1 or header.clock_bit != CLOCK_BIT_NONE:
        raise CaptureError(
            f"{path}: {header.samples_per_clock} samples per clock (clock bit {header.clock_bit}); "
            "this reader takes one sample per project clock"
        )

    values, counts, n = [], [], 0
    times: list[str] = []
    dropped = 0
    summary = None
    for i, item in enumerate(items):
        kind = item[0]
        if kind == "run":
            values.append(item[1])
            counts.append(item[2])
            n += item[2]
        elif kind == "time":
            _, _t, _clk, item_dropped, msg = item
            times.append(msg)
            dropped = item_dropped
            if msg.startswith("error="):
                raise CaptureError(f"{path}: the board reported an error at sample {n}: {msg}")
            if "overruns=" in msg:
                summary = msg
            if item_dropped > 0:
                raise CaptureGap(
                    f"{path}: {msg!r} at sample {n} (stream item {i}): {item_dropped} samples dropped so far; "
                    "the samples at or before it are not contiguous, and a capture with a gap is not stitched"
                )
    overruns = rxstall = sysclk = None
    if summary is not None:
        overruns = _keyed_int(summary, "overruns")
        rxstall = _keyed_int(summary, "rxstall")
        sysclk = _keyed_int(summary, "sysclk_hz")
        saturated = _keyed_int(summary, "saturated")
        if overruns or rxstall or saturated:
            raise CaptureGap(f"{path}: the board's closing summary {summary!r} records lost samples")

    if values:
        expanded = np.repeat(np.asarray(values, np.uint64), np.asarray(counts, np.int64))
    else:
        expanded = np.zeros(0, np.uint64)
    uo = _to_uo_out(expanded, header.signal_map, header.sample_bits, path)
    fields = _desc_fields(header.desc)
    return CaptureData(
        uo_out=uo, samples=len(uo), clock_hz=header.clock_hz, design=fields.get("project"),
        board=fields.get("board"), board_profile=fields.get("profile"), edge=fields.get("edge"),
        desc=header.desc, summary=summary, overruns=overruns, rxstall=rxstall, sysclk_hz=sysclk,
        dropped=dropped, time_messages=tuple(times),
    )  # fmt: skip

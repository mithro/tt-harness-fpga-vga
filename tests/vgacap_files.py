# SPDX-License-Identifier: Apache-2.0
"""Build .vgacap capture files for the tests with vgacap's own writer, laid out as `ttcap capture` lays them
out: the host's VGCH header (ttcap.capture.stream_header_bytes), then the board's RAW chunks of 16,384 samples
(capture_rp2.py's default buf_words 4096 x 4 samples), then its closing TIME summary."""

import io
from pathlib import Path

import numpy as np
from ttcap.boards import RP2350_DBV3
from ttcap.capture import CaptureRequest, stream_header_bytes
from vgacap.stream import Writer

CHUNK = 16_384
SUMMARY_OK = "overruns=0 rxstall=0 sysclk_hz=150000000"


def header_bytes(
    profile=RP2350_DBV3, *, design="tt_um_x", clock_hz=500_000, desc="", edge="falling"
) -> bytes:
    req = CaptureRequest(
        profile=profile, project=None, design=design, clock_hz=clock_hz, seconds=0, max_bytes=1, desc=desc,
        edge=edge,
    )  # fmt: skip
    return stream_header_bytes(req)


def board_bytes(header: bytes, parts) -> bytes:
    """The board's chunks. `parts` is a list of ("raw", samples), ("rle", pairs) and ("time", dropped, msg)."""
    from vgacap.stream import parse_header, read_chunks

    tag, payload = next(read_chunks(header))
    assert tag == "VGCH"
    buf = io.BytesIO()
    w = Writer(io.BytesIO(), parse_header(payload))
    w.fp = buf  # the board's chunks only: the header it would write first went to the scratch buffer
    for part in parts:
        if part[0] == "raw":
            samples = [int(s) for s in part[1]]
            for i in range(0, len(samples), CHUNK):
                w.raw(samples[i : i + CHUNK])
        elif part[0] == "rle":
            w.rle(part[1])
        elif part[0] == "time":
            w.time(0, 0, part[1], part[2])
        else:
            raise ValueError(part)
    return buf.getvalue()


def write_capture(
    path: Path,
    samples,
    *,
    parts=None,
    summary: str | None = SUMMARY_OK,
    summary_dropped: int = 0,
    profile=RP2350_DBV3,
    **header,
) -> Path:
    """A capture of `samples` (one per clock, already in the profile's sample layout). `parts`, when given,
    replaces the RAW chunks of `samples`; `summary` is the closing TIME message (None: no summary) and
    `summary_dropped` its cumulative dropped-sample count."""
    h = header_bytes(profile, **header)
    parts = [("raw", np.asarray(samples))] if parts is None else list(parts)
    if summary is not None:
        parts.append(("time", summary_dropped, summary))
    path.write_bytes(h + board_bytes(h, parts))
    return path

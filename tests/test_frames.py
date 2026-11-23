# SPDX-License-Identifier: Apache-2.0
import gzip
import time
from pathlib import Path

import numpy as np
import pytest

from tthfv import frames

DATA = Path(__file__).parent / "data"


def test_frame_hash_published_vectors():
    assert frames.frame_hash(b"") == 0xCBF29CE484222325
    assert frames.frame_hash(b"a") == 0xAF63DC4C8601EC8C
    assert frames.frame_hash(b"foobar") == 0x85944171F73967E8


def pins(r, g, b, vsync=0, hsync=0):
    """uo_out bytes from 2-bit channel values (arrays or ints) and sync bits (1 = pin high)."""
    r, g, b = (np.asarray(c, dtype=np.uint8) for c in (r, g, b))
    return (
        ((r >> 1) & 1) | (((g >> 1) & 1) << 1) | (((b >> 1) & 1) << 2) | (np.asarray(vsync, np.uint8) << 3)
        | ((r & 1) << 4) | ((g & 1) << 5) | ((b & 1) << 6) | (np.asarray(hsync, np.uint8) << 7)
    ).astype(np.uint8)  # fmt: skip


def gradient(x, y):
    return (x + 1) % 4, (y + 2) % 4, (x + y + 3) % 4


def make_dump(
    line_clocks=800, lines=525, hsync_clocks=96, vsync_lines=2, nframes=3,
    first_pixel=48, first_line=33, width=640, height=480, active_low=True,
):  # fmt: skip
    """A dump that starts in the vsync pulse before frame 0, so frame 0 starts at vsync_lines * line_clocks.
    A line is: back porch, active pixels, front porch, hsync pulse. A frame ends with its vsync lines.
    vsync is active for vsync_lines whole lines and ends on the LEADING edge of the last line's hsync pulse
    (as the bars generator does), so the next line start is the first one after it, with no ties."""
    total = vsync_lines + nframes * lines
    row = (np.arange(total) - vsync_lines) % lines  # row within the frame
    col = np.arange(line_clocks)
    x = col[None, :] - first_pixel
    y = row[:, None] - first_line
    active = (x >= 0) & (x < width) & (y >= 0) & (y < height)
    r, g, b = (np.where(active, c, 0) for c in gradient(x, y))
    hs = np.broadcast_to(col >= line_clocks - hsync_clocks, (total, line_clocks))
    t = row[:, None] * line_clocks + col[None, :]  # clock within the frame period
    vs_end = lines * line_clocks - hsync_clocks
    vs = (t >= vs_end - vsync_lines * line_clocks) & (t < vs_end)
    if active_low:
        hs, vs = ~hs, ~vs
    return pins(r, g, b, vs, hs).reshape(-1)


def test_cuts_640x480_frames():
    d = make_dump()
    # vsync (active low) ends on the leading edge of the last vsync line's hsync pulse, not on a line start
    assert d[1503] & 0x08 == 0 and d[1504] & 0x08 != 0 and d[1504] & 0x80 == 0
    fs = frames.cut_frames(d)
    assert fs.mode == "640x480@60"
    assert (fs.line_clocks, fs.lines, fs.width, fs.height) == (800, 525, 640, 480)
    assert fs.hsync_active_low is True and fs.vsync_active_low is True
    assert len(fs.frames) == 3
    assert fs.partial_frames_dropped == 0
    f0 = fs.frames[0]
    assert f0.start_clock == 2 * 800
    assert [f.start_clock for f in fs.frames] == [1600, 1600 + 420_000, 1600 + 840_000]
    assert len(f0.rgb888) == 640 * 480 * 3
    px = np.frombuffer(f0.rgb888, np.uint8).reshape(480, 640, 3)
    assert tuple(px[0, 0]) == tuple(85 * v for v in gradient(0, 0))
    assert tuple(px[479, 639]) == tuple(85 * v for v in gradient(639, 479))
    assert tuple(px[479, 639]) == (0, 85, 85)
    assert f0.hash == frames.frame_hash(f0.rgb888)
    assert f0.index == 0 and fs.frames[2].index == 2
    assert len({f.hash for f in fs.frames}) == 1


def test_inverted_syncs_give_identical_frames():
    a = frames.cut_frames(make_dump())
    b = frames.cut_frames(make_dump(active_low=False))
    assert b.hsync_active_low is False and b.vsync_active_low is False
    assert [(f.start_clock, f.hash) for f in a.frames] == [(f.start_clock, f.hash) for f in b.frames]
    assert b.mode == a.mode


def test_cut_off_mid_frame_drops_last_frame_and_counts_it():
    fs = frames.cut_frames(make_dump()[: 1600 + 2 * 420_000 + 100_000])
    assert len(fs.frames) == 2
    assert fs.partial_frames_dropped == 1
    assert fs.mode == "640x480@60"


def test_cut_one_clock_short_of_the_last_frame_drops_it():
    fs = frames.cut_frames(make_dump()[: 1600 + 3 * 420_000 - 1])
    assert len(fs.frames) == 2 and fs.partial_frames_dropped == 1


def test_exactly_whole_last_frame_is_kept():
    fs = frames.cut_frames(make_dump()[: 1600 + 3 * 420_000])
    assert len(fs.frames) == 3 and fs.partial_frames_dropped == 0


def test_max_frames():
    fs = frames.cut_frames(make_dump(), max_frames=2)
    assert len(fs.frames) == 2


def test_all_zero_dump_has_no_frames():
    fs = frames.cut_frames(np.zeros(2_000_000, np.uint8))
    assert fs.frames == [] and fs.mode is None
    assert fs.line_clocks is None and fs.lines is None


def test_empty_and_tiny_dumps():
    assert frames.cut_frames(np.zeros(0, np.uint8)).frames == []
    assert frames.cut_frames(np.zeros(1, np.uint8)).frames == []


def test_fewer_than_three_frame_starts_is_no_sync():
    fs = frames.cut_frames(make_dump(nframes=1))
    assert fs.frames == []
    assert fs.line_clocks == 800


def test_unknown_mode_is_cut_as_the_full_raster():
    d = make_dump(line_clocks=800, lines=506, nframes=3, first_pixel=0, first_line=0, width=800, height=506)
    fs = frames.cut_frames(d)
    assert fs.mode is None
    assert (fs.line_clocks, fs.lines, fs.width, fs.height) == (800, 506, 800, 506)
    assert len(fs.frames) == 3
    assert len(fs.frames[0].rgb888) == 800 * 506 * 3
    px = np.frombuffer(fs.frames[0].rgb888, np.uint8).reshape(506, 800, 3)
    assert tuple(px[505, 799]) == tuple(85 * v for v in gradient(799, 505))


def test_hash_changes_with_one_pixel():
    d = make_dump()
    base = frames.cut_frames(d).frames[0]
    d2 = d.copy()
    d2[1600 + 33 * 800 + 48] ^= 0x01  # R1 of pixel (0, 0) of frame 0
    other = frames.cut_frames(d2).frames[0]
    assert other.hash != base.hash
    assert other.rgb888[0] != base.rgb888[0]


def test_six_million_samples_cut_in_under_five_seconds():
    d = make_dump(nframes=14)
    d = np.concatenate([d, np.zeros(6_000_000 - len(d), np.uint8) + d[-1]])[:6_000_000]
    assert len(d) == 6_000_000
    t = time.monotonic()
    fs = frames.cut_frames(d)
    assert time.monotonic() - t < 5
    assert len(fs.frames) >= 13


def test_bars_fixture_first_two_frames_are_equal():
    d = np.frombuffer(gzip.decompress((DATA / "bars.bin.gz").read_bytes()), np.uint8)
    fs = frames.cut_frames(d)
    assert fs.mode == "640x480@60"
    assert len(fs.frames) == 3 and fs.partial_frames_dropped == 1
    assert fs.frames[0].start_clock == 393_561
    assert (
        fs.frames[0].hash == fs.frames[1].hash == fs.frames[2].hash == frames.frame_hash(fs.frames[0].rgb888)
    )


def test_a_free_running_counter_is_not_sync():
    # bit 7 and bit 3 of a counter toggle regularly, but a "frame" shorter than two lines is not a frame
    d = np.concatenate([np.zeros(10, np.uint8), (np.arange(20_000) % 256).astype(np.uint8)])
    fs = frames.cut_frames(d)
    assert fs.frames == []
    assert fs.hsync_active_low is None and fs.line_clocks is None  # 128 high, 128 low: undecided


def test_frame_hash_with_the_videos_repo_offset():
    # tb.cpp of the videos repo uses 1469598103934665603 = 0x14650fb0739d0383 as the FNV offset
    assert frames.VIDEOS_REPO_FNV_OFFSET == 1469598103934665603
    assert frames.frame_hash(b"") == 0xCBF29CE484222325
    assert frames.frame_hash(b"", offset=frames.VIDEOS_REPO_FNV_OFFSET) == 0x14650FB0739D0383
    assert frames.frame_hash(b"a", offset=frames.FNV_OFFSET) == 0xAF63DC4C8601EC8C
    assert frames.frame_hash(b"a", offset=frames.VIDEOS_REPO_FNV_OFFSET) == (
        (0x14650FB0739D0383 ^ 0x61) * 0x100000001B3 & (2**64 - 1)
    )


# ---- polarity does not depend on how long the pins sit at one level at the ends of the dump

IDLE = 2_000_000


@pytest.mark.parametrize("active_low", [True, False])
@pytest.mark.parametrize("where", ["before", "after"])
def test_long_stretch_at_the_active_levels_does_not_flip_polarity(active_low, where):
    d = make_dump(active_low=active_low)
    # both syncs held at their ACTIVE level: low for active-low syncs, high for active-high ones
    idle = np.full(IDLE, 0x00 if active_low else 0x88, np.uint8)
    padded = np.concatenate([idle, d] if where == "before" else [d, idle])
    plain = frames.cut_frames(d)
    fs = frames.cut_frames(padded)
    assert fs.hsync_active_low is active_low and fs.vsync_active_low is active_low
    assert fs.mode == "640x480@60" and len(fs.frames) == len(plain.frames) == 3
    shift = IDLE if where == "before" else 0
    assert [(f.start_clock, f.hash) for f in fs.frames] == [
        (f.start_clock + shift, f.hash) for f in plain.frames
    ]


def test_equal_median_run_lengths_are_undecided():
    # hsync and vsync high and low for 128 clocks each: nothing says which level is the pulse
    d = ((np.arange(20_000) // 128 % 2) * 0x88).astype(np.uint8)
    fs = frames.cut_frames(d)
    assert fs.frames == []
    assert fs.hsync_active_low is None and fs.vsync_active_low is None


def test_a_level_with_no_interior_run_is_undecided():
    d = np.full(5000, 0x88, np.uint8)
    d[1000:1100] = 0  # one low run, interior; no interior high run
    fs = frames.cut_frames(d)
    assert fs.frames == [] and fs.hsync_active_low is None


def test_undecided_vsync_still_reports_what_was_measured():
    d = make_dump()
    d = (d & 0xF7) | (((np.arange(len(d)) // 128 % 2).astype(np.uint8)) << 3)  # vsync: equal runs
    fs = frames.cut_frames(d)
    assert fs.frames == []
    assert fs.hsync_active_low is True and fs.vsync_active_low is None
    assert fs.line_clocks == 800


def test_fast_sync_like_pins_make_one_line_frames_not_frames():
    i = np.arange(20_000)
    d = (((i % 100 >= 20) * 0x80) | ((i % 30 >= 10) * 0x08)).astype(np.uint8)
    fs = frames.cut_frames(d)
    assert fs.frames == [] and fs.lines == 1


# ---- iter_frames: the same definition, one frame at a time


def test_iter_frames_matches_cut_frames():
    d = make_dump(nframes=4)[: 1600 + 3 * 420_000 + 1000]  # three whole frames and a partial one
    fs = frames.cut_frames(d)
    got = list(frames.iter_frames(d))
    assert got == fs.frames and len(got) == 3
    assert [f.hash for f in got] == [frames.frame_hash(f.rgb888) for f in got]


def test_iter_frames_is_lazy_and_honours_max_frames():
    it = frames.iter_frames(make_dump(), max_frames=2)
    assert next(it).index == 0
    assert [f.index for f in it] == [1]


def test_iter_frames_without_sync_yields_nothing():
    assert list(frames.iter_frames(np.zeros(100_000, np.uint8))) == []


def test_frame_sha256_is_the_sha256_of_the_rgb_bytes():
    import hashlib

    f = frames.cut_frames(make_dump()).frames[0]
    assert f.sha256 == hashlib.sha256(f.rgb888).hexdigest()


def test_iter_frames_can_skip_the_fnv_hash():
    f = next(frames.iter_frames(make_dump(), hash=False))
    assert f.hash is None and len(f.sha256) == 64
    assert f.sha256 == frames.cut_frames(make_dump(), max_frames=1).frames[0].sha256


def test_sixty_frame_capture_is_cut_and_hashed_within_bounds():
    import resource

    one = make_dump(nframes=3)[1600 : 1600 + 420_000]  # one frame period from its frame start
    d = np.concatenate([make_dump(nframes=1)[:1600]] + [one] * 60)  # 60 whole frames
    assert len(d) > 25_000_000
    t = time.monotonic()
    count = 0
    for f in frames.iter_frames(d):  # streaming: no frame is kept
        count += f.hash is not None
    elapsed = time.monotonic() - t
    assert count == 60
    assert elapsed < 30
    # ru_maxrss is the peak of the whole test process (KiB on Linux); well below 1 GB
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss < 1_000_000

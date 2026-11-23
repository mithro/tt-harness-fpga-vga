# Frame definition

This is the contract for what a "frame" and a "frame hash" are in this project. The reference
simulation, the FPGA board capture and the handheld harness all cut frames with it, and
`reference.json` records the result. The implementation is `tthfv/frames.py`; this document is
meant to be enough to reimplement it.

## Input

A sequence of bytes, one per clock of the design under test: byte `i` is `uo_out` sampled after
the rising edge of clock `i`. Clock 0 is the first reset clock (reset is held for clocks 0-9, so
`reset_clocks` is 10). All indexes below ("`start_clock`" and so on) count from clock 0 of this
sequence. If you are given a window of a longer capture, say which clock it starts at; the cutter
itself only sees the window and reports indexes into it.

## Pin map (Tiny VGA Pmod)

| bit of `uo_out` | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|---|
| signal | R1 | G1 | B1 | vsync | R0 | G0 | B0 | hsync |

A channel's 2-bit value is `(hi << 1) | lo` (R: `R1`, `R0`; G: `G1`, `G0`; B: `B1`, `B0`). It is
stored in RGB888 as `value * 85` (0, 85, 170, 255).

## Sync polarity

For each sync bit, count the samples at 1 and at 0 over the whole dump. The level held for less
than half of the samples is the *active* level; the other level is *inactive*. If neither level
is held for less than half of the samples (exactly half each), there is no usable sync and the
dump has no frames. `hsync_active_low` is true when the active level is 0, and likewise for vsync.

## Lines and frames

- An hsync pulse *ends* at a sample where hsync is inactive and the previous sample's hsync was
  active. That sample, the first inactive one, is a **line start**. (A pulse that is already
  active at sample 0 and ends later still produces a line start; the first sample of the dump
  never does.)
- `line_clocks` is the most common difference between consecutive line starts (the mode of the
  differences; of several equally common values, the smallest).
- A vsync pulse ends in the same way. A **frame start** is the first line start at or after
  the sample where a vsync pulse ends. Several vsync endings that map to the same line start give
  one frame start.
- `lines` is the most common number of line starts from one frame start to the next (same tie
  rule). Equivalently the mode of the differences between the frame starts' positions in the list
  of line starts.
- Fewer than three frame starts means **no sync**: `frames` is empty. `line_clocks` and `lines` are
  still reported if they could be measured, otherwise null.
- A frame start is **complete** when all of these hold: the next frame start (if there is one) is
  exactly `lines` line starts later, and every one of those `lines` lines is exactly `line_clocks`
  long (consecutive line starts, including the step into the next frame's first line, are exactly
  `line_clocks` apart). The last frame in the dump has no next frame start: it must have exactly
  `lines` line starts from its first to the end of the dump, the first `lines - 1` lines exactly
  `line_clocks` long, and its last line must lie wholly inside the dump,
  `start_clock + lines * line_clocks <= len`.
- Every other frame start is a **partial frame**: it is dropped, counted in
  `partial_frames_dropped`, and never hashed. (When the cutter is asked for at most `max_frames`
  frames it stops after that many complete frames; only partial frames before that point count.)
- Frames are numbered 0, 1, ... in order of complete frames. `start_clock` is the sample index of
  the frame's first line start.

## Mode table

The mode is matched on `(line_clocks, lines)` exactly, one clock per pixel:

| mode | line_clocks | lines | first active pixel after line start | first active line after frame start | width | height |
|---|---|---|---|---|---|---|
| 640x480@60 | 800 | 525 | 48 | 33 | 640 | 480 |
| 800x600@60 | 1056 | 628 | 88 | 23 | 800 | 600 |
| 1024x768@60 | 1344 | 806 | 160 | 29 | 1024 | 768 |

For a table mode, pixel `(x, y)` of frame `f` (`0 <= x < width`, `0 <= y < height`) is the
sample at index

    f.start_clock + (first_active_line + y) * line_clocks + first_active_pixel + x

A design whose `(line_clocks, lines)` is not in the table (an odd number of lines, two or a
fractional number of clocks per pixel, anything) has `mode = null` and is cut as the full raster:
`width = line_clocks`, `height = lines`, no cropping, the sync periods included. Pixel `(x, y)` is
the sample at `start_clock + y * line_clocks + x`. It is still compared exactly, just not cropped.

## Hash

The frame's RGB888 bytes are the pixels in row-major order (y outer, x inner), three bytes
(R, G, B) per pixel, each `value * 85`. `hash` is FNV-1a 64 over those bytes:

    h = 0xcbf29ce484222325
    for b in bytes: h = ((h ^ b) * 0x100000001b3) mod 2**64

`reference.json` writes it as 16 lowercase hex digits. Check values: the hash of the empty
string is `cbf29ce484222325` and of `a` is `af63dc4c8601ec8c`.

## Worked example, 640x480

Suppose active-low syncs, 800 clocks per line, hsync low for the last 96 clocks of each line,
vsync low for the last 2 lines of each frame, 525 lines per frame, and a dump that begins inside
the vsync pulse of the frame before frame 0 (as in the unit tests):

- Clocks 0-1599 are the two vsync lines. Within them, hsync is low for clocks 704-799 and 1504-1599.
  The hsync pulse ending at clock 799 gives a line start at 800, and the one ending at 1599 gives
  one at 1600. vsync goes high (inactive) at clock 1600, so the frame start is the first line start
  at or after 1600: clock 1600. `start_clock` of frame 0 is 1600. (The line start at 800 is
  before the vsync pulse ended and belongs to no frame.)
- Line `k` of the frame starts at `1600 + 800 * k`; the frame has 525 of them; the next frame
  starts at `1600 + 525 * 800 = 421600`, which is 420,000 clocks after frame 0.
- The first active line is `33`, so it starts at `1600 + 33 * 800 = 28000`. The first active pixel is
  48 clocks after that: pixel (0, 0) is sample `28048`. Pixel (639, 479) is the sample at
  `1600 + (33 + 479) * 800 + 48 + 639 = 411887`.
- The frame is 640 x 480 x 3 = 921,600 bytes, and is complete only if the dump holds at least
  `1600 + 525 * 800 = 421600` samples.

## How this differs from the other cutters

These were read for behaviour; nothing is imported from them.

- The videos repo testbench (`ttvga/harness/tb.cpp`) starts a frame at the clock where vsync ends
  and counts a line when hsync ends. For a frame start it is the same frame, but this definition
  names the first line start at or after the vsync end, which is the sample the first line's
  pixels are counted from. It matches modes approximately (pulse width and line count within a
  tolerance, clocks per pixel between 0.9 and 4.5, fractional clocks per pixel resampled
  mid-pixel), has a larger table, and for a design with a known line but an odd line count crops
  horizontally by the nearest mode's back porch and keeps every line. Here the match is exact and
  one clock per pixel, and anything else is the full uncropped raster.
- `vgacap` (`src/frame/`) reconstructs frames from a run-length stream with a timing learner,
  fills missing lines and accepts partial windows. Here a frame that is not entirely present is
  dropped and counted, never filled.

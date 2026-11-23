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

## Stimulus and initial state

The input bytes come from a simulation of the design's own top module with this stimulus. A board
that is to produce the same bytes has to reproduce it.

**Reset timing.** `ena` is high throughout. `rst_n` is low for the rising edges of clocks 0 to 9 and
high before the rising edge of clock 10 (`reset_clocks` is 10). Clock 0 is the first rising edge the
design ever sees: the clock is low while the model is first evaluated, and nothing happens on the design
before clock 0. Byte `i` of `uo_out.bin` is `uo_out` after the rising edge of clock `i`.

**Initial state.** The simulation starts from all-zero register and memory state (Verilator
`--x-initial fast`, with random reset values off). iCE40 flip-flops and block RAMs also power up zeroed
after configuration, so the two agree. A board therefore has to hold the project clock stopped from
configuration until clock 0: every clock edge before that would advance a design that the reference
has not started yet. A design whose behaviour depends on unknown (X) initial values is simulated with
zeros, which is one of the values it can have on a real device.

**Fixed inputs.** `ui_in` and `uio_in` are constants for the whole run, taken from the manifest: an
override's fixed `ui_in` or `uio_in`, else the first line of the design's `.inputs` script, else 0. Timed
scripts and gamepad scripts are not replayed. They are recorded in `reference.json` as `ui_in` and
`uio_in`.

**uio readback.** The `uio` pins are bidirectional pads. The design sees, on `uio_in`,

    uio_in = (uio_oe & uio_out) | (~uio_oe & fixed_uio_in)

that is, its own output on every pin it drives and the fixed input on the others. This matches the FPGA
wrapper, whose input buffers read the pad. The value the design sees during clock `i` is derived from
the `uio_out` and `uio_oe` it drove after the rising edge of clock `i-1` (before clock 0, from its state
after the initial evaluation). After each rising edge the testbench settles the pad as a combinational
pad would: if the derived value differs from the current `uio_in` it is applied and the model is
evaluated again, until the value is stable (at most 8 evaluations), before the outputs are recorded.
A design that never drives `uio` sees `fixed_uio_in` on all pins, as before. A design that reads back its
own outputs sees them; its frames differ from a simulation that holds `uio_in` constant.

**`uio.bin`.** Two bytes per clock: `uio_out`, then `uio_oe`, both after the rising edge of that clock
(byte `2i` and `2i+1` for clock `i`). Only the bits with `uio_oe` set are meaningful outputs; the other
bits of `uio_out` are whatever the design assigns and are not driven on a board. `uio.bin` is not used by
the frame definition; it is kept so that a board's `uio` pins can be compared.

## `reference.json`, schema 2

Written by `tthfv.frames.write_reference`. Fields: `schema` (2), `id`, `clock_hz`, `clock_declared`,
`reset_clocks`, `ui_in`, `uio_in`, `clocks` (bytes in the dump), `dump` and `uio_dump` (gzipped file
names) with `dump_sha256` and `uio_dump_sha256` (of the uncompressed bytes), `mode`, `line_clocks`,
`lines`, `hsync_active_low`, `vsync_active_low`, `width`, `height`, `frames_complete`,
`partial_frames_dropped`, `tools`, and:

- `frames`: one entry for **every** complete frame, in order: `index`, `start_clock`, `hash` (16 hex
  digits, FNV-1a 64) and, for the first 8 frames only, `png` (a file name). Entries beyond the eighth have
  no `png` key.
- `key` and `key_sha256`: everything the simulation depended on (repo, commit, `sources_sha256`,
  compilation units, top module used, `ui_in`, `uio_in`, clocks, Verilator flags and version, testbench
  hash, stimulus and cutter versions) and the sha256 of its canonical JSON. A reference is reused only if
  its key equals the current one.

Version 1 listed only the frames that had a PNG and had no `key`.

## Pin map (Tiny VGA Pmod)

| bit of `uo_out` | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|---|
| signal | R1 | G1 | B1 | vsync | R0 | G0 | B0 | hsync |

A channel's 2-bit value is `(hi << 1) | lo` (R: `R1`, `R0`; G: `G1`, `G0`; B: `B1`, `B0`). It is
stored in RGB888 as `value * 85` (0, 85, 170, 255).

## Sync polarity

For each sync bit separately, split the dump into runs of constant level and ignore the first and
the last run (the ends of the dump cut them off, and a board that sat idle before the design ran,
or after it, makes them arbitrarily long). Take the median length of the interior high runs and of
the interior low runs. The median of an odd number of lengths is the middle one; the median of an even
number is the **mean of the two middle values** (so it can end in .5; this is numpy's `median`), and the
two medians are compared as exact numbers. The level with the **shorter** median run length is the *active* (pulse)
level; the other level is *inactive*. If either level has no interior run, or the two medians are
equal, the polarity is **undecided** and the dump has no frames. `hsync_active_low` is true when the
active level is 0, and likewise for vsync; each is null when undecided.

(An earlier version of this definition used "the level held for less than half of the samples". That
flips polarity when a capture contains a long stretch at the active level, so it was replaced.)

## Lines and frames

- An hsync pulse *ends* at a sample where hsync is inactive and the previous sample's hsync was
  active. That sample, the first inactive one, is a **line start**. (A pulse that is already
  active at sample 0 and ends later still produces a line start; the first sample of the dump
  never does.)
- `line_clocks` is the most common difference between consecutive line starts (the mode of the
  differences). **Ties: the smallest.** Where several values are equally common, the smallest wins. This
  rule applies to both `line_clocks` and `lines` below.
- A vsync pulse ends in the same way. A **frame start** is the first line start at or after
  the sample where a vsync pulse ends. The frame starts are a **set** of line starts, listed in
  increasing order: several vsync endings that map to the same line start give one frame start, not
  several. Only after that is `lines` measured.
- `lines` is the most common number of line starts from one frame start to the next, measured on that
  set (ties: the smallest, as for `line_clocks`). Equivalently the mode of the differences between the frame starts' positions in the list
  of line starts.
- Fewer than three frame starts, or `lines` below 2 (a pin toggling faster than the line rate, such as a
  free-running counter, gives one "frame" per line), or an undecided polarity of either pin, means
  **no sync**: `frames` is empty, `width` and `height` are 0, `mode` is null,
  and `partial_frames_dropped` is 0. What could be measured is still reported: each pin's polarity
  when it was decided; `line_clocks` when hsync's polarity was decided and at least two line starts
  exist (otherwise null); `lines` when both polarities were decided and at least two frame starts exist
  (otherwise null).
- Samples before the first frame start are ignored; they are not a dropped frame. A vsync pulse that
  ends after the last line start yields no frame start and is ignored too.
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
vsync low for 2 lines of each frame ending on the leading edge of the last of those lines' hsync pulse,
525 lines per frame, and a dump that begins inside the vsync pulse of the frame before frame 0 (as in the
unit tests):

- Clocks 0-1599 are the two vsync lines. Within them, hsync is low for clocks 704-799 and 1504-1599.
  The hsync pulse ending at clock 799 gives a line start at 800, and the one ending at 1599 gives
  one at 1600. vsync goes high (inactive) at clock 1504, so the frame start is the first line start
  at or after 1504: clock 1600. `start_clock` of frame 0 is 1600. (The line start at 800 is
  before the vsync pulse ended and belongs to no frame.)
- Line `k` of the frame starts at `1600 + 800 * k`; the frame has 525 of them; the next frame
  starts at `1600 + 525 * 800 = 421600`, which is 420,000 clocks after frame 0.
- The first active line is `33`, so it starts at `1600 + 33 * 800 = 28000`. The first active pixel is
  48 clocks after that: pixel (0, 0) is sample `28048`. Pixel (639, 479) is the sample at
  `1600 + (33 + 479) * 800 + 48 + 639 = 411887`.
- The frame is 640 x 480 x 3 = 921,600 bytes, and is complete only if the dump holds at least
  `1600 + 525 * 800 = 421600` samples.

## Vertical phase

The frame starts at the first line start at or after the end of the vsync pulse. A design whose vsync
pulse ends *after* the trailing edge of that line's hsync pulse therefore has its first line start counted
as the previous frame's last line, and the frame is cut one line later than the design's own row 0
(and so on for every further line the vsync end is late by). This is deliberate and matches the videos
repo's testbench, which also starts a frame at the vsync end. Two examples:

- The calibration bars generator (`calibration/tt_um_vgacal_bars`) ends vsync on the leading edge of the hsync
  pulse, before the line start: our row 0 is the design's row 0.
- The VGA Playground style generator ends vsync after the hsync trailing edge: our row 0 is the design's
  row 1, and our row 479 is therefore the design's row 480, a black front porch line, while the design's
  last active row is cut as row 478.

Both are right under the definition; a different cutter that starts at the line start *before* the vsync
end would give a different picture and different hashes.

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
- **The published hashes use a different FNV offset.** The videos repo's `frame_hashes.txt` values are
  FNV-1a 64 computed with the non-standard offset `0x14650fb0739d0383` (decimal 1469598103934665603),
  because of a typo at its `tb.cpp:238` (the real offset, 14695981039346656037, is missing its last digit).
  For a design at one clock per pixel in a table mode, the pixels and the crop are otherwise identical to
  ours. Their frame 0 is our first frame whose start is after their `capture_after_clock`, and their
  testbench's clock counter equals our sample index plus 1. `frames.frame_hash(rgb888, offset=...)` and
  `frames.VIDEOS_REPO_FNV_OFFSET` reproduce their values. Worked check, `tt08/tt_um_tinytapeout_logo_screensaver`
  (`capture_after_clock` 1703936): our frame 4 (start clock 2074362, standard hash `14e77679a4e8077e`)
  re-hashed with that offset is `f8a61a32ac42fc84`, their first hash; our frames 5 to 7 give their next
  three (`f3492a12fe07ced6`, `d3b57afe4b8f3fcc`, `7b238cda1f04318f`).
- `vgacap` (`src/frame/`) reconstructs frames from a run-length stream with a timing learner,
  fills missing lines and accepts partial windows. Here a frame that is not entirely present is
  dropped and counted, never filled.

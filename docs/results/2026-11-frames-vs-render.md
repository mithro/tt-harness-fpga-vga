# Our frames against the published render: tt08/tt_um_tinytapeout_logo_screensaver

A note, not a pass/fail. Made with `tthfv reference` (6,000,000 clocks, Verilator 5.053) and the
videos repo's published outputs.

## What was compared

- Ours: `reference.json` says `640x480@60`, 13 complete frames, `hsync_active_low` and
  `vsync_active_low` both false, frame 0 at clock 394,362, frames every 420,000 clocks.
- Published poster: `.../tt08/tt_um_tinytapeout_logo_screensaver/..._poster.png`.
- Videos repo hashes: `data/results/tt08/tt_um_tinytapeout_logo_screensaver/frame_hashes.txt`
  (3,596 hashes, `capture_after_clock: 1703936`; frame 0 starts at the first end of a vsync pulse
  after that clock).

## Poster

- Size: the poster is **640x480**, not 2x scaled as the brief expected, so it was compared at its
  native size. It is lossy (MJPEG source): 5,901 distinct colours against 33 in ours, so exact pixel
  equality is impossible.
- Same picture: a 126x126 pixel logo ring on black. Thresholding the poster (any channel above 60)
  gives a 126x126 bounding box with 7,320 lit pixels; ours is 126x126 with 7,179. After aligning the
  bounding boxes the masks differ in 141 pixels (about 2%), consistent with the compression.
- Different frame of the animation: the logo is at x=506, y=105 in the poster and at x=202, y=199 in
  our frame 0 (it moves one pixel right and one up per frame). A whole-image difference at zero
  offset is about 6% of pixels for every frame, which is just the logo in two places. The best
  whole-pixel offset search is meaningless here for that reason, so the comparison was by bounding
  box instead.

## Hash

The first published hash is `f8a61a32ac42fc84` for the frame that starts at the first end of a vsync
pulse after clock 1,703,936. The frame starts in our dump are 394,362 + k * 420,000, so the first one
after that clock is our frame 4, start clock 2,074,362 (the published capture's matching vsync end is
at about clock 2,073,611, the line start 751 clocks later). Its hash is `566334d7ccd8d819`. **They
differ.** Moreover none of our 13 frame hashes equals any of the 3,596 published hashes.

What can be said:

- Not a size or crop offset: both are 640x480 with the same line and frame periods (800 / 525, 420,000
  clocks per frame, first sync at the same time, within the 1,600-clock vsync pulse). Shifting our
  crop by up to 3 pixels in x and y for frames 3 to 6 against the first 20 published hashes gives no
  match either.
- Our own simulation is deterministic: two simulations of this design gave a byte-identical dump
  (same sha256).
- The testbench stimulus is the same on paper (ena=1, rst_n low for 10 clocks, one eval at each clock
  edge, `ui_in` = `uio_in` = 0), and the pixel and hash definitions are the same on paper (row-major
  RGB888, value * 85, FNV-1a 64, active area only at one clock per pixel).
- So the pictures in the two sets of frames are probably not the same animation state, or the
  published hashes were made with a different build or different design inputs. This was not pinned
  down, and the definition was not changed to try to match.

## Reproduce

    uv run tthfv reference tt08/tt_um_tinytapeout_logo_screensaver

then compare `work/reference/tt08/tt_um_tinytapeout_logo_screensaver/reference.json` with
`frame_hashes.txt`.

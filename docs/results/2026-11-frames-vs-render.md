# Our frames against the published render: tt08/tt_um_tinytapeout_logo_screensaver

Made with `tthfv reference` (6,000,000 clocks, Verilator 5.053) and the videos repo's published outputs.
Result: **the pictures are identical; the published hashes differ only because the other project's hash
function has a typo in its offset.**

## What was compared

- Ours: `reference.json` says `640x480@60`, 13 complete frames, `hsync_active_low` and
  `vsync_active_low` both false, frame 0 at clock 394,362, frames every 420,000 clocks.
- Published poster: `.../tt08/tt_um_tinytapeout_logo_screensaver/..._poster.png`.
- Videos repo hashes: `data/results/tt08/tt_um_tinytapeout_logo_screensaver/frame_hashes.txt`
  (3,596 hashes, `capture_after_clock: 1703936`; frame 0 starts at the first end of a vsync pulse
  after that clock).

## Poster

- Size: the poster is **640x480**, not 2x scaled, so it was compared at its native size. It is lossy (MJPEG
  source): 5,901 distinct colours against 33 in ours, so exact pixel equality is impossible.
- Same picture: a 126x126 pixel logo ring on black. Thresholding the poster (any channel above 60) gives a
  126x126 bounding box with 7,320 lit pixels; ours is 126x126 with 7,179. After aligning the bounding boxes
  the masks differ in 141 pixels (about 2%), consistent with the compression.
- A different frame of the animation: the logo is at x=506, y=105 in the poster and at x=202, y=199 in our
  frame 0 (it moves one pixel right and one up per frame).

## Hash

The first published hash is `f8a61a32ac42fc84`, for the frame that starts at the first end of a vsync pulse
after clock 1,703,936. Our frame starts are 394,362 + k * 420,000, so the first one after that clock is our
frame 4, start clock 2,074,362. Its standard FNV-1a hash is `14e77679a4e8077e`, which is not the published
value. (An earlier version of this note quoted `566334d7ccd8d819`; that is our frame 8.)

The videos repo's `tb.cpp:238` computes FNV-1a with the offset `1469598103934665603` = `0x14650fb0739d0383`,
the real offset `14695981039346656037` with its last digit missing. Re-hashing our frames with that offset
reproduces the published hashes exactly, so the two simulations draw identical pixels and the crop, sampling
and frame alignment agree. Our frames 4 to 7 against the first four lines of `frame_hashes.txt` (output of a
script over our private reference):

    our frame  start_clock  standard offset   videos-repo offset  published (their frame i)  equal
            4      2074362  14e77679a4e8077e  f8a61a32ac42fc84  f8a61a32ac42fc84  True
            5      2494362  35dc316d2fca2dc0  f3492a12fe07ced6  f3492a12fe07ced6  True
            6      2914362  7f88796e49005346  d3b57afe4b8f3fcc  d3b57afe4b8f3fcc  True
            7      3334362  9ab9c4165f1263b9  7b238cda1f04318f  7b238cda1f04318f  True

`frames.frame_hash(rgb888, offset=frames.VIDEOS_REPO_FNV_OFFSET)` makes the check repeatable. Our own hashes
keep the standard offset, so that they are comparable with any other FNV-1a implementation.

## Reproduce

    uv run tthfv reference tt08/tt_um_tinytapeout_logo_screensaver

then hash frames 4 and up with the videos repo offset and compare with `frame_hashes.txt`.

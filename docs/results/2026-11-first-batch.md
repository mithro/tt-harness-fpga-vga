# First batch: 13 designs through fetch, build and reference

Date: 2026-11-23. Tool versions: yosys 0.69+24, nextpnr-ice40 0.11.1-27, Verilator 5.053 (the pinned
oss-cad-suite 2026-09-14). The batch was `tthfv run all --force --jobs 13` over the five `calibration/*`
designs and the eight shared designs; the whole run took 234 s (the 2048 game, with the largest build,
set the pace). Two harness faults were found and fixed: a `.v` file that needs the `-sv` reader (dottee
would not synthesise) and a timing parser that missed the main clock of a two-clock design (see "Failures"
and "Timing parser fault"). The table is from the batch re-run after both fixes.

## Table

`ok` in the build column means: synthesised, placed, routed, and every clock nextpnr reports meets its
timing estimate at the design's clock. It does not mean tested on hardware. `timing-fail` means built but at
least one clock misses; the best bitstream is kept. Each dump was cut into 13 complete frames; the first 8
of them are listed with hashes and PNGs in `reference.json` (the frames column counts the 13).

| id | fetch | build | LUT % | main-clock fmax / clock (MHz) | clocks | reference | mode | frames |
|---|---|---|---|---|---|---|---|---|
| calibration/tt_um_vgacal_bars | ok | ok | 2.8 | 35.86 / 25.175 | 1 | ok | 640x480@60 | 13 |
| calibration/tt_um_vgacal_counter | ok | ok | 4.7 | 32.08 / 25.175 | 1 | ok | 640x480@60 | 13 |
| calibration/tt_um_vgacal_grid | ok | ok | 2.3 | 30.99 / 25.175 | 1 | ok | 640x480@60 | 13 |
| calibration/tt_um_vgacal_modes | ok | ok | 3.8 | 27.21 / 25.175 | 1 | ok | 640x480@60 (ui_in 0) | 13 |
| calibration/tt_um_vgacal_prbs | ok | ok | 4.0 | 32.04 / 25.175 | 1 | ok | 640x480@60 | 13 |
| tt07/tt_um_tinytapeout_dvd_screensaver | ok | **timing-fail** | 14.8 | 23.17 / 25.175 | 1 | ok | 640x480@60 | 13 |
| tt08/tt_um_tinytapeout_logo_screensaver | ok | **timing-fail** | 15.4 | 23.44 / 25.175 | 1 | ok | 640x480@60 | 13 |
| ttihp26a/tt_um_urish_rings | ok | ok | 3.8 | 56.39 / 25.175 | 1 | ok | 640x480@60 | 13 |
| ttihp0p3/tt_um_algofoogle_fun_vga_clock | ok | ok | 22.3 | 29.73 / 25 | 1 | ok | 640x480@60 | 13 |
| ttihp25a/tt_um_algofoogle_vga | ok | ok | 18.9 | 31.31 / 25 | 1 | ok | 640x480@60 | 13 |
| ttsky26a/tt_um_algofoogle_dottee | ok | **timing-fail** | 17.7 | 19.38 / 25.175 | 2 | ok | 640x480@60 | 13 |
| ttsky25a/tt_um_gamepad_pmod_demo | ok | **timing-fail** | 9.3 | 21.32 / 25.175 | 1 | ok | 640x480@60 (static) | 13 |
| ttihp25a/tt_um_2048_vga_game | ok | **timing-fail** | 53.7 | 17.08 / 25.175 | 1 | ok | 640x480@60 (static) | 13 |

Clocks: only dottee has more than one clock in its final nextpnr log. Its second clock is
`uo_out[3]$SB_IO_OUT_$glb_clk` (a clock net derived from the vsync output), 98.59 MHz, PASS; the main clock
`clk$SB_IO_IN_$glb_clk` is 19.38 MHz, FAIL. The other twelve have the single main clock
(`scripts/first_batch/list_clocks.py`).

All 13 fit the UP5K (the largest, the 2048 game, uses 53.7% of the 5280 logic cells). Five of the 13 do not
meet timing at the clock the design declares; their bitstreams exist (the best of eight place-and-route
attempts is kept) but they have not been shown to work at that clock. Per design, sync polarity differs (the
two screensavers, dottee and the gamepad demo are active-high, the others active-low); the cutter detected
each. The frame starts were all at clock 393,561 (calibration) or 394,362 (the others), 420,000 clocks apart.

Of the eight shared designs, three give a bitstream that meets timing (urish_rings, algofoogle_vga,
fun_vga_clock) and five do not: the DVD screensaver, the logo screensaver, the gamepad demo, the 2048 game
and dottee, all `timing-fail`. None fails to fit and none fails to build.

## Failures and their classification

### ttsky26a/tt_um_algofoogle_dottee: synth-fail, a harness fault, fixed

First run: `build` = `synth-fail`, `yosys exit status 1: .../src/audio.v:194: ERROR: syntax error,
unexpected TOK_ID`. yosys had printed `The SystemVerilog keyword '>>=' ... is not recognized unless
read_verilog is called with -sv`. Line 194 is `p2 >>= 1;` in a plain `.v` file. The build stage passed `-sv`
only to files ending in `.sv`, and no `sv2v` is installed in the pinned toolchain, so there was no second
chance. Verilator accepts the file (the reference stage was `ok` for it), and the Tiny Tapeout hardening flow
reads all sources as SystemVerilog, so this is a fault in the harness, not in the design.

Fix: when the first yosys run fails with a syntax error and the sources include `.v` files, run yosys again
with `read_verilog -sv` on every file, and only then try sv2v if it is installed. The reader that worked is
reused for the other synthesis variant, and `build.json` records it as `sv_reader: true`. New tests (a `.v`
file that needs `-sv` builds; the second variant does not repeat the failed plain run; a missing module does
not trigger the retry) failed before the change and pass after. The build stamp version was bumped so an old
cached `synth-fail` is not reused.

After this fix the design built, but was wrongly reported `ok` at 98.59 MHz; see the next section.

## Timing parser fault: a second clock hid the main clock's failure

What was wrong. After the `-sv` fix dottee was reported `ok` with fmax 98.59 MHz. Its nextpnr log reports
two clocks: `uo_out[3]$SB_IO_OUT_$glb_clk` (derived from the vsync output) at 98.59 MHz, PASS, and the main
`clk$SB_IO_IN_$glb_clk` at 18.94 MHz, FAIL (attempt `default`/1, log line 589). nextpnr pads the name
column with extra spaces when it reports several clocks, and the parser's pattern allowed exactly one space
before the quoted name, so the `clk` line was never matched; the parser fell back to the only clock it had
found. Any multi-clock design in the full run was at risk of a hidden timing failure.

How it was found. The independent review of this task read the real log (and I had flagged the 98 MHz as odd
but not investigated it).

The rule now. `parse_nextpnr` matches the clock line with any amount of whitespace and keeps, for every
clock, the final (post-route) Fmax and nextpnr's own PASS/FAIL verdict. `build.json` has
`clocks: {name: {fmax_hz, pass}}`. The top-level `fmax_hz` is the main clock's (named `clk` or starting with
`clk$`; if no clock has that name, the slowest clock). An attempt meets timing only if every reported clock
passes, so a failing derived clock also makes it `timing-fail`, and the reason names the failing clocks and
their frequencies. Among failing attempts the kept one has the highest main-clock fmax. The real two-clock
log is a test fixture (`tests/data/logs/nextpnr-dottee.log`) and tests cover: main fails with a side clock
passing, main passes with a side clock failing, both pass, three padded lines, no `clk` name. The build stamp
was bumped, which invalidates every cached build.

Dottee's real figures over its 8 attempts (main-clock fmax, MHz; the derived clock passes in each): default
seeds 1 to 4: 18.94, 18.39, 18.75, 18.27; nodffe seeds 1 to 4: 19.38, 18.07, 18.69, 18.19. Kept: nodffe
seed 1, 19.38 MHz, 17.7% LUT. Re-running the batch after the fix changed one status: dottee `ok` ->
`timing-fail`. All other 12 designs have one clock and are unchanged.

### The five timing-fail designs: a property of the design, at this clock and with this flow

- tt07 DVD screensaver 23.17 MHz and tt08 logo screensaver 23.44 MHz: the two Tiny Tapeout screensavers,
  known to miss on paper. Best of 8 attempts; the attempts ranged 21.85 to 23.17 and 22.04 to 23.44 MHz.
  Both use about 15% of the device.
- ttsky26a dottee 19.38 MHz main clock (attempts 18.07 to 19.38), 17.7% LUT.
- ttsky25a gamepad demo 21.32 MHz (attempts 19.82 to 21.32), 9.3% LUT.
- ttihp25a 2048 game 17.08 MHz (attempts 16.04 to 17.08), 53.7% LUT, the only large design in the batch.

Evidence: `build.json` of each, `attempts` list (eight `timing-fail` entries each, no `no-fit`, no
`pnr-fail`), and the nextpnr logs in `WORK/build/<id>/nextpnr-<variant>-<seed>.log` ("Max frequency for
clock ...: N MHz (FAIL at 25.17 MHz)"). I did not look at which paths fail, so I cannot say whether it is
logic depth or routing; the harness reports what nextpnr says and the designs' sources are untouched.

Whether a design would still work on a board at a clock a little above its fmax, or at a lower clock,
is a question for the hardware plan; this batch only says the bitstreams were not shown to meet 25.175 MHz.

### tt_um_gamepad_pmod_demo: reference is a property of the design (needs input)

The build and reference stages are ok, but the picture is static (the first 8 frames have one hash) because
the design reads a gamepad Pmod serial stream and our reference drives no gamepad. The videos project ran it
with a scripted gamepad (`override: gamepad`), which this harness does not do yet, and gave it the verdict
`barely-moving`. Cross-check B skips it for that reason. The 2048 game is static for the same reason (it
waits for button input), and nothing else in the batch needs input.

### No failures from the two known gaps

No first-batch design instantiates sky130 standard cells, so the `sky130_polyfill.v` question did not come
up and nothing was imported. No design had a wrong `top_module` (the build stage's top module was the one
declared in every case).

## Timing experiment (by hand, outside the tool)

For each `timing-fail` design (the four first, dottee after the parser fix), nextpnr-ice40 was re-run on the kept attempt's synthesized netlist with the
same flags and seed as the build stage, plus `--opt-timing` or `--tmg-ripup`
(`scripts/first_batch/timing_experiment.py`, which takes the flags from `tthfv.build`; the build stage is unchanged by the experiment). The baseline column reproduces the
kept fmax exactly, so the runs are deterministic. fmax is nextpnr's final post-route "Max frequency".

| design (kept recipe) | build.json | baseline | `--opt-timing` | `--tmg-ripup` |
|---|---|---|---|---|
| tt07 dvd_screensaver (default, seed 1) | 23.17 | 23.17 | 20.45 | 23.15 |
| tt08 logo_screensaver (nodffe, seed 2) | 23.44 | 23.44 | 22.36 | 23.40 |
| ttsky25a gamepad_pmod_demo (default, seed 3) | 21.32 | 21.32 | 18.62 | 21.32 |
| ttihp25a 2048_vga_game (nodffe, seed 2) | 17.08 | 17.08 | 13.26 | 17.06 |
| ttsky26a dottee, main clock (nodffe, seed 1) | 19.38 | 19.38 | 17.79 | not run |

Neither option helps. `--opt-timing` made every one of the five worse (by 0.8 to 3.8 MHz; dottee was run later, with the baseline and `--opt-timing` only, and its derived clock stayed at 98.59 MHz), and
`--tmg-ripup` changed nothing for the other four (within 0.05 MHz) while taking much longer on the big design (about 6 minutes
against 25 seconds for the 2048 game; a few seconds for the others). The result is for one seed per design,
so a different seed could give another number, but the size of the effect says neither deserves to become a
sweep recipe on this evidence. For scale: across the eight attempts of each design, main-clock fmax varied by 1.0
to 1.5 MHz, and the screensavers are about 2 MHz short of the clock.

## Calibration comparison

`scripts/first_batch/compare_calibration.py`. Two references exist for the calibration designs: the Python
reference renderer in mithro/tt-vga-testpatterns (`tools/render.py`; the project ships no PNGs of its own
renders, only this code and the hardware-validated comparison tool) and the frames captured from a real
board in mithro/tt-vga-capture (`docs/results/2026-09-15-calibration-loop-fpga1/*-frame.png`). All sizes are
640x480 except the hardware modes frame (800x600).

| design | our frame vs renderer | our frame vs hardware frame |
|---|---|---|
| bars | frame 0 identical (307,200 pixels) | frame 0 identical |
| grid | frame 0 identical | frame 0 identical |
| counter | all 8 listed frames identical to `render.counter(n)`, n = 1..8 as decoded from each picture | the hardware frame shows counter 7; our frame 6 (counter 7) is identical |
| prbs | all 8 frames identical to `render.prbs(n)`, n = 1..8 | the hardware frame shows counter 7; our frame 6 is identical |
| modes | frame 0 (ui_in 0, 640x480) identical to `render.modes(0)` | not comparable directly: the board ran ui_in 1 (800x600@60, positive syncs) |

For `counter` and `prbs` the picture changes every frame (it draws its own frame number), so a comparison of
frame 0 with a fixed image is meaningless; each of our frames was compared with the renderer for the counter
decoded from that frame, and the hardware frame was matched to the simulated frame carrying the same counter.
The counter starts at 1 in our frame 0; the hardware frame was captured later, at counter 7.

For `modes`, the reference stage drives ui_in 0 only. To compare with the hardware frame I ran the compiled
testbench by hand with `--ui-in 1` (`scripts/first_batch/compare_modes_ui_in.py`, 4,000,000 clocks): our
cutter reports 800x600@60, 1056 clocks per line, 628 lines, both syncs active-high, 5 frames, and frame 0 is
identical to both the hardware frame (480,000 pixels) and `render.modes(1)`. No pixel differed anywhere, so
there is no harness fault to chase.

## Cross-check A: a real board capture through our cutter

Capture: `bars-capture.vgacap.gz` (tt_um_vgacal_bars on a board, 500 kHz project clock, falling-edge
sampling, 4,882,432 samples, 0 overruns). Decompressed with `gzip -d`, then
(`scripts/first_batch/crosscheck_hardware_capture.py`):

1. vgacap's own `vgacap-frames` tool reconstructed 11 frames, all 640x480@60, hsync and vsync active-low.
   Each one is identical to our simulated `frame-000.png`: 11 of 11, 0 differing pixels.
2. `vgacap-dump` does not emit samples (it lists chunks and a checksum only), but the vgacap Python package
   (`vgacap.stream.read_stream`, loaded by path as a library, nothing copied) returns the samples. The signal map
   in the header is `7,3,0,4,1,5,2,6`, which is exactly our `uo_out` pin layout (R1 bit 0, G1 bit 1, B1 bit 2,
   vsync bit 3, R0 bit 4, G0 bit 5, B0 bit 6, hsync bit 7), so the 8-bit samples are our one-byte-per-clock
   dump format unchanged. Our `cut_frames` on those 4,882,432 real samples: mode 640x480@60, 800 clocks per
   line, 525 lines, both polarities active-low (as in the simulation), 11 complete frames, 1 partial frame
   dropped, frame starts at clock 243,679 + k * 420,000. All 11 frame hashes are `abfdb0998fff1d25`, the same as
   the simulated reference's hash (all 13 simulated frames of this design are identical to each other).

So the first real board data to go through our cutter gives the same frame count as vgacap's, the same
mode and polarities, and the same pixels and hash as the simulation. Not covered: only the bars design
was captured as a stream (the other four hardware results are PNGs), and the captured design draws one static
picture, so this checks the crop, the pin map, the polarity detection and the hash on real data, but not
frame-to-frame ordering or animation.

## Cross-check B: published per-frame hashes

The mithro/tinytapeout-vga-videos project published `frame_hashes.txt` for all eight shared designs. Our dump
(6,000,000 clocks, 13 frames cut) was re-hashed from our first frame that starts after `capture_after_clock`
(1,703,936, which is our frame 4, start clock 2,074,362 for all seven) with that project's FNV offset
(`0x14650fb0739d0383`) and compared in order with its first hashes. 6,000,000 clocks was enough: no longer
simulation was needed (`scripts/first_batch/crosscheck_published_hashes.py`).

| design | frames compared | equal | distinct hashes among them |
|---|---|---|---|
| tt07/tt_um_tinytapeout_dvd_screensaver | 9 | 9 | 9 |
| tt08/tt_um_tinytapeout_logo_screensaver | 9 | 9 | 9 |
| ttihp26a/tt_um_urish_rings | 9 | 9 | 9 |
| ttihp0p3/tt_um_algofoogle_fun_vga_clock | 9 | 9 | 9 |
| ttihp25a/tt_um_algofoogle_vga | 9 | 9 | 9 |
| ttsky26a/tt_um_algofoogle_dottee | 9 | 9 | 9 |
| ttihp25a/tt_um_2048_vga_game | 9 | 9 | 1 (static picture) |
| ttsky25a/tt_um_gamepad_pmod_demo | skipped: their run was scripted (gamepad override) | | |

All seven comparable designs have `ui_in` 0, 640x480 and 1.0 clocks per pixel in their header. 63 of 63
compared hashes are equal: the two simulators draw the same pixels, and the crop, sampling point and frame
alignment agree, for six animated designs and one static one. Because the 2048 game's nine hashes are all
the same, that row proves the picture, not the frame alignment. Nothing mismatched, so there was nothing to
trace back to stimulus.

## What this means for the full run

- **Timing is the main risk, not fit.** 5 of the 8 shared designs miss 25.175 MHz with the
  current recipes; none missed on area. Some of the 445 will probably end `timing-fail` too, so decide early
  what the library does with them. `--opt-timing` and `--tmg-ripup`
  do not rescue them (above).
- **Multi-clock designs are a real category.** One of the 13 (dottee) has a derived clock next to `clk`, and
  its first reported fmax was wrong by 5x until the parser was fixed. Every `ok` in the full run now means
  every reported clock passed, but `build.json` `clocks` should be read for any design whose list has more
  than one entry, since a derived clock can be a real timing constraint of its own.
- **A build that works for one design can fail for the next on a language feature.** The `-sv` retry covers
  `>>=`; a design that needs real `sv2v` would still be `synth-fail` because sv2v is not in the pinned
  toolchain. Count those in the full run before deciding to add it.
- **Designs that need input look fine and are wrong.** The gamepad demo and the 2048 game give `ok`
  references that are one static picture; the results table cannot tell. A reference needs a stimulus
  (scripted gamepad, buttons) for those, or the board comparison will pass on a picture that never moves.
- **Runtime scales with design size, not count.** The batch took 234 s with 13 jobs, dominated by the 2048
  build (about 4 minutes for 8 attempts at 53.7% LUT). The 445-design run is bounded by a handful of
  large designs, so run them early.
- **Reference runs are 6,000,000 clocks, so only the first ~13 frames are covered.** Designs whose behaviour
  changes later (after capture_after_clock, 1.7M clocks, or a game-over or demo-loop later) are only validated
  for what happens in the first 0.24 s. Fine for a smoke test; the videos project's hashes go for 3,596
  frames.
- **Real-board data works with our cutter as is**, including a capture that starts mid-frame (one partial
  frame was dropped, as designed) and the sample byte layout from the vgacap project's Tiny VGA map.
- The `modes` design shows the reference stage does not cover non-default `ui_in`; any design whose
  interesting mode needs a pin set will need a per-design stimulus in the manifest (the field exists).

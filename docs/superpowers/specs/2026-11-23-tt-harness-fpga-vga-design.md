# tt-harness-fpga-vga: design

Date: 2026-11-23. Status: draft for the owner's approval.

Lines marked **(owner)** record something the owner said. Lines marked **(call)** are
the author's decisions, open to correction.

## Intent

Build a large library of prebuilt, known-good FPGA bitstreams of good-looking Tiny
Tapeout VGA designs **(owner)**. They are for:

- demos that <https://tinytapeout.fpgas.online/> loads onto its boards **(owner)**;
- people who own the board, to try at home **(owner)**.

"Known-good" means the picture a real board produces was checked against the simulated
render of the same design. The harness is the tool that builds each bitstream and does
that check quickly and repeatably **(owner)**.

The candidates are the 440 projects rendered at
<https://data.wafer.space/big-storage/tinytapeout-vga-videos/>. The bitstreams will
probably be published in the same place as the videos **(owner)**.

## Terms

- **Board**: the Tiny Tapeout ETR demo board (RP2350B "MCU") carrying the FPGA breakout
  (Lattice iCE40UP5K, SG48). The four boards at Welland behind
  tinytapeout.fpgas.online are of this type. Each is attached to a Raspberry Pi that
  runs the `fpgas-tt` daemon, has a Digilent Pmod HAT wired to the board's I/O pins,
  and a camera.
- **Design**: one Tiny Tapeout project, `tt_um_*`, with the standard `ui_in`, `uo_out`,
  `uio_*`, `clk`, `rst_n`, `ena` ports, driving a Tiny VGA Pmod on `uo_out`.
- **Videos repo**: `mithro/tinytapeout-vga-videos`, the finished simulation pipeline
  that produced the renders.

## Goals

1. One command builds a bitstream for a design from the shared manifest and reports
   whether it fits and meets timing.
2. One command validates a built design on a real board and gives a verdict with
   evidence (captured frames, the differences, the timing report).
3. Validation works in two modes **(owner)**:
   - **MCU mode**: the board's MCU clocks the design and drives and reads its pins.
     Nothing needs to be plugged in.
   - **Pmod mode**: the design runs at full speed with the right Pmods plugged into the
     board's connectors.
4. Start with a few simple designs and get them fully working, then fan out to every
   rendered design **(owner)**.
5. Publish the validated bitstreams with an index the `fpgas-tt` daemon can read, and a
   results table that says, for every one of the 440, what happened and why.
6. Share the per-project manifest and the reference frame data with the
   `tt-handheld-harness` session (Game Bub / Arty), which has agreed to consume them.

## Non-goals

- Changing the designs to make them fit or meet timing. A design that needs a patch
  beyond what the videos repo's overrides already carry is recorded as failing.
- Validating audio in the first pass **(call)**. Audio-capable designs are still built
  and their picture validated.
- Analog designs (8 of the 440). They cannot run on an FPGA.
- Replacing `fpgas-online/tinytapeout-fpga-demos`. That repo stays the small,
  hand-curated, CI-built set shipped in the Pi image. This project feeds it candidates.
- Reflashing the MCU. The stock Tiny Tapeout MicroPython SDK is what people have at
  home, so the harness must work on it. MicroPython scripts and PIO programs sent over
  the REPL and run from RAM are in scope and expected; `ttcap` already works this way.
- Replaying timed input (gamepad presses, input scripts) on hardware in the first
  pass **(call)**. See "Stimulus" under Reference.
- Delivering the library onto the Welland Pis. The first pass publishes files in the
  daemon's format; how the Pis come to hold them is a later step agreed with the
  owner (see Publish).

## Anti-goals

- A bitstream in the library that does not work on a stock board at home. Passing only
  on a slowed clock is not enough: a design must also meet timing at its real clock.
- Hogging the Welland boards. They are public and other people use them, people this
  project cannot message **(owner)**.
- A verdict that hides a difference. A one-pixel shift, a wrong colour or a missing
  frame is reported as what it is, never rounded to "pass".
- Blaming the hardware for a mismatch without evidence.

## Constraints

- iCE40UP5K: 5,280 LUTs, 30 block RAMs (4 kbit each), 4 SPRAMs, 8 DSPs, and slow.
  Of the 440 designs, 239 are 1x1 tiles, 125 are 1x2 and 76 are 2x2 or larger. About
  60 declare a clock above 31 MHz. A real share will not fit or will not meet timing.
- The MCU cannot produce exactly 25.175 MHz; it uses the nearest rate its PWM gives.
- Stock-firmware capture through the MCU tops out at about a 750 kHz project clock
  (measured by the capture project on these boards). A 640x480 frame is 420,000
  clocks, so about 0.84 s per frame at 500 kHz.
- The videos repo keeps no lossless reference frames and its per-frame hashes are
  stripped from the committed results.
- Four boards, one of which (fpga-1) another session uses. fpga-2 is not connected.
- The owner's standing conventions: Apache-2.0, `uv` for all Python, small commits,
  no files in `/tmp`, sub-agents each in their own worktree and reviewed before merge.

## Approaches considered

1. **A standalone harness repo (chosen).** `tt-harness-fpga-vga` owns the build, the
   reference simulation, the hardware validation and the published library. It reads
   the videos repo's manifest and overrides, reuses the demos repo's FPGA wrapper and
   pin file, and runs `ttcap` as a program for capture. It emits the daemon's existing
   `index.json` shape. Cost: a second build script to keep compatible with the demos
   repo's wrapper.
2. **Extend `tinytapeout-fpga-demos`.** One build flow and one package, but it turns a
   curated, CI-built repo into a bulk pipeline with hardware in the loop, puts about
   46 MB of bitstreams in a package installed on every Pi, and gives the Game Bub
   session no toolchain-neutral manifest.
3. **Extend `tinytapeout-vga-videos`.** Least new code, since it already resolves
   sources and stimulus. But that project is finished and by design never touches a
   real board; adding hardware would couple two things that fail in different ways.

The owner asked for a repo named as a harness under `mithro`, which is approach 1.

## How it works

Five stages, each a subcommand of one CLI (`tt-harness-fpga-vga`, short `tthfv`
**(call)**), each reading and writing plain files so any stage can be re-run alone.

### 1. Manifest

Input: the videos repo's `data/targets.json` (440 entries: shuttle, macro, repo,
commit, top module, source files, pinout, pmods, clock), each project's
`data/results/.../result.json` (the verdict, the QSPI pin map, and the override that
was applied), and the hand-written source overrides `overrides/<shuttle>/<macro>.yaml`
for the input events.

`data/overrides-compiled/` is *not* an input: it is gitignored in the videos repo and
absent from a clone (found while building the manifest loader). The first input
event derived from the YAML was checked against all 58 locally compiled scripts, with
no mismatch.

Counted on 2026-11-23: 83 projects have a compiled override; 58 of those carry a timed
input script, 19 a gamepad script and 6 a fixed `ui_in`. 35 projects use the QSPI
Pmod. 64 declare a gamepad Pmod. `auto_ui_in` is null in every committed result, so it
is not relied on.

Clock: a design's declared `clock_hz` is used for timing and for the full-speed run.
Where it is 0 (49 projects) the harness uses 25,175,000 Hz, as the videos repo does
(`ttvga/harness/job.py`) **(call)**.

The harness does not copy this data. It pins the videos repo at a commit and reads it
**(call)**. The fetch-and-patch step (clone at commit, apply overrides) is taken from
the videos repo's `ttvga/harness/job.py`, wrapped so the Game Bub session can call it.

Candidate set for the library: the 328 projects whose render verdict is `ok`
**(call)**. The other 112 (static, barely-moving, blank, no-sync, failed, skipped) are
still built and validated where possible, but ranked below and not published as demos.

### 2. Build

Per design: substitute the top module into `tt_fpga_top.v`, then
`yosys synth_ice40` → `nextpnr-ice40 --up5k --package sg48 --freq <clock>` with the
FabricFox pin file → `icepack`. Toolchain: a pinned oss-cad-suite release installed
under the repo's ignored `tools/` directory **(call)**.

Output per design, under `build/<shuttle>/<macro>/`: the `.bin`, the logs, and a
`build.json` with LUT/RAM/DSP use, the achieved maximum frequency against the
required clock, and a status: `ok`, `no-fit`, `timing-fail`, `synth-fail`, `skipped`.

SystemVerilog goes through yosys's own front end first and through `sv2v` if that
fails **(call)**. HardCaml, Chisel, Clash, Spade, VHDL and Wokwi designs are built
from the Verilog the videos pipeline already used; if it has none, they are skipped.

**QSPI Pmod designs (35).** These keep a framebuffer or artwork in the flash and two
PSRAMs of the QSPI Pmod on `uio`, and draw nothing without them. The iCE40UP5K's own
memory is used to emulate the Pmod inside the FPGA **(owner)**. For these designs the
build uses a second wrapper, `tt_fpga_top_qspi.v`, which connects the design's `uio`
bus to an emulated Pmod instead of to the pins:

- The emulation is a Verilog port of the videos repo's behavioural model
  (`ttvga/harness/qspi.h`): the same flash and PSRAM commands, SPI mode 0, and the
  pin map from the project's result (`qspi` field), which is not always the default.
- PSRAM contents live in the four SPRAM blocks: 4 x 256 kbit, 128 KB in total, against
  16 MB per device on the real Pmod. Addresses wrap at the emulated size **(call)**.
  How the 128 KB is split between the two PSRAMs is left to the builder.
- Flash is read-only and no project currently supplies a flash image, so the flash
  returns the videos model's test pattern, computed from the address in logic with no
  storage **(call)**. SPRAM cannot be initialised from the bitstream, so a real flash
  image would have to be loaded by the MCU before reset is released, through the
  physical `uio` pins, which this wrapper leaves free. That loader is a later step.
- The reference simulation runs the *same Verilog* emulation, wrapped the same way, so
  simulation and hardware agree by construction, including the address wrap. The
  model is checked once against `qspi.h` in simulation.
- A design that needs more than the emulated memory will draw a wrapped picture. It
  still matches its reference; whether it is worth publishing is a curation question,
  flagged in the results as `qspi-wrapped` when the design addresses beyond 128 KB.
- These bitstreams are published marked `qspi: emulated`. They work on a board with
  no QSPI Pmod, which suits people at home. A plain build for use with a real Pmod is
  also produced but not validated, since no Welland board has that Pmod.
- The emulation costs logic and all four SPRAMs, so some of the 35 will fit plain but
  not emulated. That is recorded as `no-fit (qspi)`.

**The MCU can emulate the memory too (owner).** The `uio` pins reach the RP2350
(GPIO 25 to 32), so the MCU can answer the QSPI bus itself. The two backends
complement each other, and the harness supports both **(call)**:

| | In the FPGA (SPRAM) | In the MCU (PIO) |
|---|---|---|
| Bitstream | special wrapper | the plain one, as used with a real Pmod |
| Memory | 128 KB, wraps | limited by the RP2350's RAM and flash, larger |
| Flash image | test pattern, or loaded by the MCU | can be served from a file |
| FPGA cost | logic plus all four SPRAMs | none |
| Speed | the design's full clock | bounded by PIO: to be measured |
| At home | nothing extra | needs the harness's script on the board |

- MCU emulation is tried first in MCU mode, because at the slowed 500 kHz clock speed
  is no obstacle and it validates the plain bitstream, the one a Pmod owner would use.
- FPGA emulation is used for the full-speed run where the MCU cannot keep up with the
  design's SCK, and it is what gets published for boards with no Pmod.
- The MCU emulation the owner means is single-bit SPI, not quad **(owner)**. The
  existing code is `MichaelBell/spi-ram-emu` (RP2040, C SDK, commands READ 0x03,
  WRITE 0x02 and FAST READ 0x0B, 12 to 20 MHz at a 125 MHz system clock). It is C, not
  MicroPython, so it is the reference for a PIO program run from the stock firmware,
  not a drop-in.
- So the MCU backend serves designs that only ever use single-bit commands, and the
  FPGA backend serves the ones that use dual or quad commands. The reference
  simulation of each of the 35 designs records which commands it issued, and that
  decides the backend; nobody classifies them by hand.
- The reference simulation models whichever backend the hardware run used, with the
  same memory size and wrap.

### 3. Reference

The harness simulates each design itself with Verilator, with the videos repo's
`tb.cpp` reset sequence: `ena=1`, `ui_in`/`uio_in` at their values, `rst_n` low for
10 clocks, then high.

**Stimulus (call).** The reference is this project's own, so it uses the stimulus the
hardware can reproduce exactly, not the render's:

- `ui_in` and `uio_in` are held at one fixed value for the whole run: the override's
  fixed `ui_in` if there is one, else the first line of its input script, else 0.
- Timed input scripts and gamepad scripts are *not* replayed. A gamepad design is
  validated as it behaves with no buttons pressed.
- Consequence: for the 58 + 19 scripted designs the validated frames are the opening
  frames with idle inputs, which may differ from the published clip. That is
  acceptable for a first pass because 8 frames after reset rarely depend on input. A
  design that shows nothing without input is recorded as `needs-input`, not failed.
- Cycle-accurate replay from the MCU is a later phase.

It writes, per design, `reference/<shuttle>/<macro>.json`:

- clock used, reset stimulus, `ui_in`, `uio_in`;
- the detected mode, width and height;
- `frame0_start_clock`, counted from the first reset clock;
- the ordered FNV-1a 64 hashes of the first N frames, hashed as `tb.cpp` does
  (RGB888, row-major, active area, each 2-bit channel times 85);
- the first N frames themselves as PNG, so a mismatch can be shown, not just counted.

N is 8 frames by default **(call)**. Unlike the renders, the reference starts counting
at reset, not after sync calibration.

**One frame definition (call).** The reference and the hardware capture must cut
frames out of the pin stream the same way, or `exact` means nothing. So the reference
simulation does not reuse `tb.cpp`'s frame cutting. It writes the raw `uo_out` value
per clock, from the first reset clock, one byte per clock, and one small frame cutter
in this repo reconstructs frames from both the simulated dump and the board's capture.
The hash is FNV-1a 64 over the reconstructed frame as RGB888. These hashes are this
project's, not comparable with the videos repo's stripped ones.

**No dependency tree (owner).** "A little bit of code duplication is better than
having a huge complex dependency tree." This repo imports no sibling project's code.
It reads the videos repo's data files from a pinned clone, copies the two FPGA wrapper
files, and carries its own frame cutter and hash (about 150 lines). What is shared
between this project, the capture project (`vgacap`) and the handheld harness
(`tt-harness-handheld-vga`) is *definitions*, written down so each can keep its own
small copy: the frame definition and hash (`docs/frame-definition.md`) and the
reference file's schema. Sibling tools are used only as programs, never as libraries:
plan 2 may run `ttcap` to capture, and reads what it writes.

The raw simulated stream is kept too (compressed), because comparing pin streams clock
by clock is the most direct check there is and needs no frame logic at all.

This file is the contract with the Game Bub session.

### 4. Validate

**MCU mode (the gate).** Built on `ttcap` from `vgacap`, which today samples `uo_out`
on a 500 kHz PWM clock but releases reset through separate REPL calls with the clock
running, and cannot drive `ui_in`. So this needs new MCU-side script work:

1. Upload the bitstream with the daemon's `POST /bitstream` (uploads are capped at 16
   and evict the oldest, which suits a rolling validation run) and enable it.
2. Set `ui_in`/`uio_in` from the MCU to the reference's fixed value.
3. Reset deterministically: stop the project clock, assert `rst_n`, step exactly 10
   clocks by toggling the clock pin from the MCU, release `rst_n`, then start the
   500 kHz clock and the capture together. Hardware clock 0 is then the reference's
   clock 0, give or take a start-up offset of a few clocks that is measured once on
   the calibration designs. If it proves not to be constant, the comparison searches a
   small bounded window (at most ±16 clocks) and records the offset it found.
4. Capture `uo_out` on every clock for N frames plus margin.
5. Compare the pin streams clock by clock, then reconstruct frames with this repo's cutter
   and compare hashes frame by frame.

Verdicts: `exact` (every frame hash matches), `shifted` (identical after a constant
whole-pixel offset, which the playground's sync generator is known to cause; the
offset is recorded), `mismatch` (with a difference image and the count of differing
pixels per frame), `no-sync`, `capture-error`.

Whether `shifted` counts as a pass is decided by the owner's function described under
"Left to the owner" below.

**Full-speed check.** A slowed clock proves the logic, not the speed. So every design
must also (a) pass nextpnr timing at its real clock, and (b) run on the board at its
real clock while the MCU measures the hsync and vsync periods and polarities and
checks them against the reference mode **(call)**. This needs a new PIO edge timer
(`ttcap` has only a sampler); it has no data-rate limit. It does not check pixels.
Both parts are required for entry to the library **(call)**: (a) alone would let in a
design that only works on paper.

**Pmod mode.** This is the author's reading of "just plugging in the correct Pmods"
and needs the owner's confirmation:

- *At home:* a Tiny VGA Pmod and a monitor (and the gamepad or audio Pmod if the
  design uses them). `tthfv show <design>` loads the bitstream at full speed and
  prints the reference poster's location, so a person compares screen with poster.
  `tthfv cycle` steps through the library.
- *At Welland:* the Pmod on the connectors is the Pi's Pmod HAT. The Pi cannot sample
  pixels at 25 MHz, but it can count sync edges, which gives an independent check of
  the full-speed sync timing from outside the MCU. The camera sees the board, not a
  monitor, so there is no picture check here unless a monitor or capture device is
  added. No tool for this exists yet; it is optional in the first pass and comes
  after the fan-out **(call)**.

If the owner meant something else by Pmod mode (a capture Pmod, a second FPGA as a
checker, a self-check built into each bitstream), this section changes and nothing
else does.

A design enters the library when: build `ok`, MCU mode passes, full-speed sync check
passes.

### 5. Publish

- `library/index.json` in the shape the daemon reads (name, title, author,
  description, docs and repo URLs, clock, pinout), plus `<name>.bin` per design.
  Names are `<shuttle>_<macro>` cut to the daemon's `[a-z0-9_]{1,40}` rule with a
  collision check **(call)**.
- `results/index.json` and a generated `docs/results.md`: every one of the 440, its
  build status, resource use, validation verdict, and a link to the render.
- Upload beside the videos on data.wafer.space. The path and the upload mechanism are
  the owner's; nothing is uploaded until the owner says where.
- Getting them onto the Pis: the daemon serves only the packaged demos directory
  (`/usr/share/fpgas-tt/demos`, from the `fpgas-online-tt-demos` deb) and up to 16
  visitor uploads lost at reboot. Roughly 330 bitstreams at about 104 KB is 34 MB.
  Whether that goes into the deb, is synced from data.wafer.space, or stays a download
  for people at home is the owner's decision and outside the first pass.
- A short list of the best validated designs is proposed as a pull request to
  `tinytapeout-fpga-demos`. Not opened without asking.

## Sharing the boards

- Use fpga-3 (sw2 p36), fpga-4 (p35) and `tt-fd1a167bd863a198` (p33). Leave fpga-1
  (p34) to the capture session unless it is asked first. Confirm each board's identity
  by USB serial before first use.
- Before every use: read `https://tinytapeout.fpgas.online/board/<slug>/status.json`
  (the site's view of the daemon's `/health`; it returned `clients` and
  `idle_display.state` for all four boards on 2026-11-23). If `clients` is above zero
  or the idle display is "in use", someone is on it; skip it and try again later.
  Never interrupt. The first hardware task must confirm that a visitor's Run or upload
  shows up in one of those two fields; if it does not, find the field that does before
  any batch runs.
- Also look at what the board is doing: if its serial port is printing output from a
  program this project did not start (fpga-3 was on 2026-11-23), treat it as in use.
- Between Claude sessions: a claim file,
  `~/github/TinyTapeout/tmp/board-claims/<board>-p<port>.json`, with owner, purpose
  and expiry. Expired claims are void. Claims are short (minutes), taken per batch of
  designs, not for the whole run.
- Work in short bursts: validate a few designs, release, re-check. One design takes
  roughly 10 to 20 s of board time, so a full pass over about 330 designs is one to
  two hours of board time spread over three boards and many bursts.
- Leave the board as found: reload the idle-display bitstream when done.
- Use the daemon's HTTP and WebSocket routes for loading and serial **(call)**. An
  earlier rule for the capture project preferred owning the serial device on the Pi;
  the daemon route is chosen here because it is what makes other users visible.

## Order of work

1. **First batch, by hand.** The eight designs the Game Bub session also uses:
   `tt07/tt_um_tinytapeout_dvd_screensaver`, `tt08/tt_um_tinytapeout_logo_screensaver`,
   `ttihp26a/tt_um_urish_rings`, `ttihp0p3/tt_um_algofoogle_fun_vga_clock`,
   `ttihp25a/tt_um_algofoogle_vga`, `ttsky26a/tt_um_algofoogle_dottee`,
   `ttsky25a/tt_um_gamepad_pmod_demo`, `ttihp25a/tt_um_2048_vga_game`. Preceded by
   the five calibration designs from `tt-vga-testpatterns`, which are already known to
   capture pixel-exactly, to prove the harness before blaming any design.
2. **Fan out the build** to all 440 (no board needed, parallel on big-storage).
3. **Fan out the reference simulation** for everything that built.
4. **Fan out validation** across the free boards, most promising designs first.
5. **Triage** failures by category; fix the harness where the harness is at fault.
6. **Publish.**

## Team

The session that wrote this manages; sub-agents on cheaper models implement and test
**(owner)**. Proposed limits **(call)**, pending the owner:

- Every sub-agent works in its own git worktree on its own branch and commits small
  and often **(owner)**.
- Nothing merges to `main` until it has had an independent code review and an
  independent validation, both by an agent other than the one that wrote it
  **(owner)**. Validation means re-running the tests and, for hardware work, the
  hardware check, and reading the output, not trusting the implementer's report.
- At most four sub-agents at once, Sonnet for implementation and debugging, Haiku for
  mechanical triage of logs.
- At most one sub-agent per board at a time.
- Builds and simulations use up to 40 of the host's 88 cores.

## Left to the owner

One piece of logic is a judgement, not a mechanism, and the author will leave a
marked function for the owner to write (about ten lines):

`verdict_passes(verdict, evidence) -> bool` in `tthfv/policy.py`. It decides which
validation outcomes admit a design to the library: whether a constant one-pixel shift
passes, whether a design that matches on 7 of 8 frames passes, whether a tolerance in
differing pixels is ever acceptable.

## Left to the builder

- Module layout, the exact CLI flags, and the JSON field names beyond those above.
- How the capture is driven (extending `ttcap` or calling it).
- The parallel job runner for builds and simulations.
- How long to wait and how often to retry when a board is busy.
- Test strategy: unit tests for hashing, frame reconstruction and index generation
  need no hardware; a hardware-in-the-loop test uses a calibration design.

## Open questions for the owner

1. Is the reading of **Pmod mode** above right?
2. Are the **team limits** right?
3. Where exactly do the bitstreams go on data.wafer.space, and who uploads?
4. How should the library reach the Welland Pis: in the deb, synced from
   data.wafer.space, or not at all for now?

## Calls made after the builder review

A reviewer read this document cold. These were settled by the author and are marked
**(call)** above: MCU scripts over the REPL are in scope; deterministic reset by
stepping the clock; fixed inputs only in the first pass; one frame-cutting library for
both sides; both full-speed checks required; uploads via `POST /bitstream` during
validation; 25.175 MHz where no clock is declared; Pi-side sync counting optional.

## Changes and findings from plan 1

Recorded on 2026-11-23 as plan 1 (build and reference pipeline) was built and reviewed.
Where this section and the text above disagree, this section is current.

- **Timing sweep.** One place-and-route seed under-reports what the FPGA can do, so a
  build tries up to eight recipes (two synthesis variants, `default` and `-nodffe`,
  by seeds 1 to 4), stops at the first that meets timing, and otherwise keeps the
  fastest. `build.json` records the kept `recipe` and every attempt. Nothing that
  moves or merges registers is used; the flip-flop count is the same in both variants.
- **Build statuses** are `ok`, `synth-fail`, `no-fit`, `timing-fail`, `pnr-fail`
  (nextpnr failed for a reason other than capacity) and `pack-fail`.
- **Timing is marginal at 25 MHz.** Both Tiny Tapeout screensavers use about 15% of
  the device and reach 23.2 to 23.4 MHz at best against 25.175 MHz. Static timing is
  conservative, so whether "must meet timing on paper" is the right entry rule is an
  open question for the owner, to be settled with hardware results (plan 2).
- **Sync polarity** is the level with the shorter median run length, ignoring the
  runs cut off at each end of the capture, not the level held for less than half the
  capture. A board capture can begin or end with a long idle stretch.
- **Fewer than two lines per frame is not sync** (a free-running counter otherwise
  looks like a video signal).
- **Vertical phase.** A frame starts at the first line start at or after the end of
  the vsync pulse. Designs using the VGA Playground's sync generator are therefore cut
  one line later than their own row 0, as the videos repo's testbench also does.
- **The videos repo's published frame hashes use a mistyped FNV offset**
  (0x14650fb0739d0383). The pixels agree exactly with this project's simulation for
  the design compared; `frames.frame_hash` takes the offset as a parameter for
  cross-checks. Full definition: `docs/frame-definition.md`.
- **Reference stage statuses** are `ok`, `no-sync`, `verilate-fail` and `sim-fail`
  (which includes a design calling `$finish`).
- **Stale results.** A stage's "already done" check compares everything the result
  depended on. The fetch stage digests every regular file under the design's `src`
  directory once (`sources_sha256`); the build's reuse digest and the reference's `key`
  both use it, together with the commit, the top module used, the flags, the tool
  versions and the arguments (for the reference also `ui_in`, `uio_in`, the clock count
  and the testbench). `tthfv results` marks a record whose commit, `sources_sha256` or
  tool version differs from the current manifest, fetch and tools as `stale`.
- **Untrusted file names.** Source paths come from third-party `info.yaml` files.
  They must resolve inside the project's own repository, and names containing
  characters that could alter a yosys script are rejected.
- **The runner survives a dead worker** (pool rebuilt, one retry, then `error:
  worker died` for that design only) and Ctrl-C (children killed, exit code 130).
- **Not yet mirrored from the videos repo's flow:** its `sky130_polyfill.v` cell
  library. It is handled when a design needs it. (The top-module name correction is
  now done once, in the fetch stage, for both build and reference.)

### Fix wave after the whole-branch review (2026-11-23)

- **One resolution of a design.** The fetch stage decides the top module used (with
  the single-`tt_um_` fallback), the compilation units (headers and non-HDL files only
  add their directory to the include path; VHDL is a fetch failure) and one source
  digest. Build and reference use them and record them, with commit and repo.
- **Records carry what they were made from.** `reference.json` (schema 2) holds the
  full simulation key and lists every complete frame; `results` reports `stale` for
  records that no longer match, and a later errored attempt beats an older `ok`.
- **uio readback.** The reference testbench models the bidirectional pads like the
  FPGA wrapper does, so a design that reads its own uio outputs sees them.
- **Timeouts and hygiene.** Every external call goes through `tthfv/proc.py` with a
  timeout and a process-group kill; a build has a wall-clock budget; `sim.log` is
  bounded; the manifest accepts only a 40-digit commit and an `https://` repository;
  tools come only from the pinned toolchain.

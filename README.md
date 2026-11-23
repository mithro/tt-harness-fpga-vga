# tt-harness-fpga-vga

`tthfv` builds iCE40UP5K FPGA bitstreams of [Tiny Tapeout](https://tinytapeout.com/) VGA designs (the
kind that run in the [VGA Playground](https://github.com/TinyTapeout/vga-playground)) and simulates each
design to produce a reference: a raw per-clock dump of its output pins, the frames cut from it, and
their hashes. The point is a library of bitstreams for the Tiny Tapeout FPGA development board, each one
admitted only once the picture a real board produces matches its simulated reference.

The name follows `tt-harness-<board type>-<design type>`.

## Status

- The build and reference pipeline works and is tested: fetch, build (yosys, nextpnr-ice40, icepack),
  reference simulation (Verilator), frame cutting, results index.
- The first 13 designs are done (8 shuttle designs and 5 calibration designs). See
  [docs/results/2026-11-first-batch.md](docs/results/2026-11-first-batch.md).
- **There is no hardware validation yet.** No bitstream built here has been checked against a real
  board by this project's pipeline, and the simulated references have not been compared with board
  captures. Whether a design that misses 25.175 MHz timing on paper is acceptable is undecided.
- Comparing board captures with the references is the next plan.

The definition of a frame, its hash and the simulation stimulus is in
[docs/frame-definition.md](docs/frame-definition.md).

## Requirements

- Linux (x86-64) and [uv](https://docs.astral.sh/uv/).
- git, and a C++ compiler and make (Verilator compiles each design's model).
- About 4 GB of disk for the toolchain (the pinned oss-cad-suite), plus about 10 MB per design. The
  toolchain download is about 740 MB; it is checked against a pinned SHA-256 digest before it is
  unpacked, and the download is deleted afterwards.
- Network access to GitHub: the toolchain, the designs' repositories and the videos repo.

## Install and run

Read the [SECURITY](#security) section first: building and simulating a design runs third-party code,
and this tool does not sandbox it.

```
uv sync
uv run tthfv tools install                          # the pinned oss-cad-suite into tools/
uv run tthfv manifest sync                          # clone the videos repo into work/
uv run tthfv run all --only tt08/tt_um_tinytapeout_logo_screensaver
uv run tthfv results                                # work/results/index.json and results.md
```

Only after reading SECURITY, and preferably inside a sandbox, run every design the videos repo rendered:

```
uv run tthfv run all --candidates --jobs 40
```

Time: a few seconds to a few minutes per design (a build tries up to eight place-and-route recipes; a
reference simulates 6,000,000 clocks). Finished work is skipped on a second run; a result is reused only
if its inputs, commit and tools are unchanged.

## Commands

- `tthfv tools install` downloads and unpacks the pinned oss-cad-suite (again if the pin changed).
- `tthfv tools versions` prints the installed tool versions.
- `tthfv manifest sync` clones the videos repo at the pinned commit into `TTHFV_VIDEOS_REPO` or `work/videos-repo`.
- `tthfv manifest list [--candidates] [--json]` lists the designs.
- `tthfv manifest show ID` prints one design's record as JSON.
- `tthfv fetch ID...` clones each design's repository at its pinned commit and resolves its sources.
- `tthfv build ID... [--force] [--max-attempts N] [--design-timeout SECONDS]` builds bitstreams.
- `tthfv reference ID... [--clocks N] [--force]` simulates and cuts the reference frames.
- `tthfv run {fetch,build,reference,all} [--only ID] [--candidates] [--jobs N] [--force] [--clocks N]` runs a stage over many designs in parallel.
- `tthfv results [--out-md PATH]` writes the results index and the Markdown table (default
  `work/results/results.md`, beside `index.json`).

`ID` is `<shuttle>/<macro>`, for example `tt08/tt_um_tinytapeout_logo_screensaver`.

## Where things live

- `TTHFV_WORK`: the work directory (default `work/` in the repository; git-ignored). Holds `src/`
  (checkouts and `fetch.json`), `build/` (`build.json`, `design.bin`, logs), `reference/`
  (`reference.json`, gzipped dumps, PNGs), `run/` (what the runner saw), `results/index.json` and `results/results.md`.
- `TTHFV_TOOLS`: where the toolchain is installed (default `tools/`). Tools are only ever taken from
  here, never from the host `PATH`.
- `TTHFV_VIDEOS_REPO`: a checkout of the videos repo to read the design list from, instead of
  `work/videos-repo`.
- `TTHFV_BOARD_CLAIMS`: the claims directory for the public boards (`tthfv board probe`). It must be the
  one directory shared by every project that uses these boards on the host, so a board session refuses to
  start unless it is set; `tthfv board status` needs no claim and works without it.
- `TTHFV_BOARD_BASE_URL` / `TTHFV_BOARD_WS_URL`: the boards' site, default
  `https://tinytapeout.fpgas.online` (tests point these at a fake board).

## Statuses

Fetch:

- `ok`: the checkout exists and its sources resolved (top module found, no VHDL, files inside the checkout).
- `fetch-fail`: git failed, a source is missing or outside the checkout, there is no usable top module, or
  the sources are VHDL (not supported).
- `patch-fail`: the override patch did not apply.

Build:

- `ok`: a bitstream was built and meets timing at the design's clock.
- `timing-fail`: built, but the best of the attempts is below the clock; the bitstream is kept.
- `synth-fail`: yosys failed (or the design could not be read, or a script path was unsafe).
- `no-fit`: the design does not fit the iCE40UP5K.
- `pnr-fail`: nextpnr failed for another reason, timed out, or the design's time budget ran out.
- `pack-fail`: icepack failed or its output is not a bitstream.

Reference:

- `ok`: simulated and at least one complete frame was cut.
- `no-sync`: simulated, but no VGA frames could be cut (no sync, or too few frames).
- `verilate-fail`: Verilator could not build the model.
- `sim-fail`: the simulation failed, timed out, or the design called `$finish`.

In `tthfv results` and the runner, any stage can also be:

- `not-run`: there is no record of the stage (or the latest run did not get to it). The reason
  `record missing (interrupted run?)` means the runner once finished the stage but its record file is gone.
- A status followed by `(timed out)` (for example `pnr-fail (timed out)`): a tool timeout or the design
  budget caused it, so it says nothing about the design itself. Such a build is redone on the next run.
- `error`: the latest attempt raised an exception or its worker died; see `work/run/<id>/error.log`.
- `invalid`: a record file is damaged or has the wrong shape.
- `stale`: the record was made from another commit, other sources or other tool versions than the current
  ones; the reason names the field. Run the stage again.

## SECURITY

Building and simulating a design **executes third-party code**. Verilog system tasks (`$system`,
`$fopen`, `$fwrite`, `$readmemh`, DPI and others) can run commands and read or write files, the
designs' build files are run by `make`, and a patch or `.gitmodules` comes from the same untrusted
sources. Run the pipeline as an unprivileged user, or inside a sandbox (a container, a virtual machine or
a separate account with nothing of value in reach), whenever you use it on designs you do not trust. **This
tool does not sandbox anything for you.** The manifest only accepts a 40-digit commit hash and an
`https://` repository URL, and every external program has a timeout and is killed with its children, but
that limits accidents, not a hostile design.

## Known limitations

- **No sandbox.** The pipeline runs third-party code with your privileges; see SECURITY. Run it as an
  unprivileged user or in a container or virtual machine before pointing it at designs you do not trust.
- Smaller open items: Verilator's `-y <src>` library search has no yosys equivalent (a module found only
  that way gives a reference but a `synth-fail` build); a grandchild process that calls `setsid` escapes the
  timeout kill; the pad-readback settle loop does not report a combinational `uio` loop; host
  `VERILATOR_ROOT` and `VERILATOR_BIN` are passed through; four ttihp25a designs list a
  `sky130_polyfill.v` their repositories do not contain; the frame hasher is pure Python and holds every
  frame in memory (fine for 13 frames, not for a long board capture); and there is no tool yet that aligns
  a board capture to a reference and reports the first differing clock. The list is in the design spec,
  "Open after plan 1".

## Developing

```
uv sync
uv run pytest -q
uv run ruff check .
uv run ruff format --check tthfv tests scripts
```

Tests that need the real toolchain are marked `needs_tools` and are skipped when it is not installed.

## Attribution and licence

Apache-2.0. See `LICENSE`.

`tthfv/harness/tt_fpga_top.v` and `tthfv/harness/tt_fpga_fabricfox.pcf` are copied unchanged from the
`harness/` directory of [fpgas-online/tinytapeout-fpga-demos](https://github.com/fpgas-online/tinytapeout-fpga-demos),
commit `d2991f0b221afa1d937bea27c5dcf9ce007c31fe` (originally from TinyTapeout/tt-fpga-compiler, Apache-2.0). The
SPDX line in `tt_fpga_top.v` reads `Apache 2.0`; that is upstream's text and the file is kept byte-identical
to upstream.

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
- git, and a C++ compiler, make and perl (Verilator compiles each design's model).
- [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) with unprivileged user namespaces, which
  runs every tool that touches a design's code; see [SECURITY](#security). Without it, set
  `TTHFV_SANDBOX=none` and accept the risk.
- About 4 GB of disk for the toolchain (the pinned oss-cad-suite), plus about 10 MB per design. The
  toolchain download is about 740 MB; it is checked against a pinned SHA-256 digest before it is
  unpacked, and the download is deleted afterwards.
- Network access to GitHub: the toolchain, the designs' repositories and the videos repo.

## Install and run

Read the [SECURITY](#security) section first: building and simulating a design runs third-party code,
which `tthfv` confines with bubblewrap (the fetch stage's git is not confined).

```
uv sync
uv run tthfv tools install                          # the pinned oss-cad-suite into tools/
uv run tthfv manifest sync                          # clone the videos repo into work/
uv run tthfv run all --only tt08/tt_um_tinytapeout_logo_screensaver
uv run tthfv results                                # work/results/index.json and results.md
```

Only after reading SECURITY, run every design the videos repo rendered:

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
- `TTHFV_SANDBOX`: `bwrap` (the default) or `none`; see SECURITY.
- `TTHFV_VIDEOS_REPO`: a checkout of the videos repo to read the design list from, instead of
  `work/videos-repo`.

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
sources.

**The sandbox.** yosys, nextpnr-ice40, icepack, sv2v, Verilator (and the `make` and C++ compiler it
starts) and the compiled simulation all run inside [bubblewrap](https://github.com/containers/bubblewrap),
set up in `tthfv/sandbox.py`:

- the whole filesystem is read-only, and only `/usr` (with `/bin`, `/lib*`, and `/etc/alternatives` and
  `/etc/ld.so.cache` from `/etc`) is visible of the system: no `/home`, `/root`, `/srv`, `/var`, `/run`,
  `/opt` or the rest of `/etc`;
- the only other paths are the pinned toolchain, the design's fetched checkout and this repository's
  `tthfv/harness` directory, all read-only (so a design cannot change its own sources, and its relative
  `$readmemh` paths work), and **one** writable directory, the stage's `scratch/` directory;
- `/tmp` and the empty `HOME` are private tmpfs mounts of at most 256 MiB each, and `/proc` and `/dev`
  are minimal;
- no network, new PID, IPC and UTS namespaces (the host name is `sandbox`), a new session, all
  capabilities dropped, and no user namespaces of its own (`--unshare-user --disable-userns`);
- `prlimit` inside the sandbox limits everything in it to 16 GiB of address space, 1 GiB per file,
  no core files and 1024 open files. The number of processes is not limited (the kernel counts it per
  user over the whole machine, so any limit that stops a fork bomb would break the owner's own
  processes); the PID namespace and the timeouts bound it;
- the environment is exactly `PATH` (the pinned toolchain's `bin`, `/usr/bin`, `/bin`), `HOME` and `LANG`;
- with `--die-with-parent` and the PID namespace, a timeout or Ctrl-C kills everything in the sandbox,
  including a process that called `setsid`.

**The host never trusts what the sandbox writes.** Each stage directory (`work/build/<id>/`,
`work/reference/<id>/`) is owned by the host: logs, `*.json` records, the gzipped dumps, the PNGs and the
bitstream live there, and it is never writable from the sandbox. The sandbox writes only `scratch/` below
it (the Verilator build tree and the compiled model, the raw dumps, the netlists, the placed design,
icepack's output). When a tool has finished the host reads back only the specific files it expects, and
only through `tthfv/safefile.py`: the directory is opened without following a symlink, each file with
`O_NOFOLLOW` and without blocking, and it must be a regular file with a single hard link of the expected
size (the dumps are exactly `clocks` and `2 x clocks` bytes; a bitstream is at most 1 MiB). Anything else, a
symlink, a hard link, a FIFO, a device, a directory or a file of the wrong size, fails the stage with a
reason saying the design's output was not a plain file. Logs are opened by the host, which hands the
sandbox only the pipe, and keeps only the first 1 MiB and the last 64 KiB of a log in the file and in
memory (plus the few lines the parsers look for, if they were in the part left out). `scratch/` is deleted
when the stage ends (set `TTHFV_KEEP_SCRATCH=1` to keep it for debugging), by directory descriptors, at any
depth, without following a symlink; if something in it cannot be deleted it is moved to `work/trash/`
and the stage fails with `scratch could not be removed`. Its size is checked, also by descriptors, after each
tool: past 2 GiB the stage fails.

Nothing in a host-owned directory is trusted either, because something else (an older version, a stage that
went wrong) may have left symlinks there. A stage starts by clearing its directory without following anything.
Every file the host writes in a stage, run, fetch or results directory is made by `safefile.create`
(`O_CREAT|O_EXCL|O_NOFOLLOW` under a temporary name in a directory opened without following a symlink, then a
rename by directory descriptor), every record it reads comes back through `safefile.read_file`, and a work
directory below `WORK` that is a symlink is refused (`paths.design_dir`).

The host reads the checkout the same way. The project directory and its `src/` must be real directories (a
repository can commit `src` as a symlink to anywhere, and such a design fails to fetch), the source digest is
computed by walking the tree by directory descriptor (nothing followed, a symlink counts as its link text), and
source files are read only if they are regular files.

`TTHFV_SANDBOX=bwrap` is the default. If `bwrap` is missing or cannot create namespaces, the stage fails
with an error that says so; it never runs unsandboxed by itself. `TTHFV_SANDBOX=none` turns the sandbox
off (for a container or virtual machine you already trust, or a machine without user namespaces) and
prints a warning once at the start of every command that can run design code, also when all results turn
out to be cached; with `none` the `scratch/` split still applies but nothing confines the design.
`build.json` and `reference.json` record `"sandbox": "bwrap" | "none"`; it is not part of what decides
whether a result is reused.

**git runs outside the sandbox** (it needs the network), with a locked-down configuration: the environment
is built from an allow-list (a `PATH`, an empty `HOME`, `LANG` and proxy and CA variables), the system and
global git configuration are not read (`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=/dev/null`, so no
user-configured filters such as git-lfs, `insteadOf` rewrites, `sshCommand`, credential helpers or aliases),
only `https` is allowed (`GIT_ALLOW_PROTOCOL`, `protocol.allow=never`: a submodule URL cannot make git run
ssh with your keys, `ext::` or a local path), there are no hooks and no filesystem monitor, git-lfs does
not download (`GIT_LFS_SKIP_SMUDGE`), and nothing prompts. The manifest only accepts a 40-digit commit hash
and an `https://` repository URL. The same applies to the clone of the videos repo.

**Not covered.** The sandbox has no quota for CPU time, process count or disk (the 2 GiB check on
`scratch/` happens after a tool finishes, not while it runs; a real quota needs privileges); `/usr` and the
toolchain are readable inside; the kernel is shared, so a kernel or bubblewrap vulnerability is not
covered; and git, though locked down, still parses hostile repository data. For designs you do not trust at
all, also run as an unprivileged user, preferably in a virtual machine.

## Known limitations

- **The sandbox needs unprivileged user namespaces.** Where they are off (some CI, some containers) the
  tools do not run until you choose `TTHFV_SANDBOX=none`; see SECURITY.
- Smaller open items: Verilator's `-y <src>` library search has no yosys equivalent (a module found only
  that way gives a reference but a `synth-fail` build); the pad-readback settle loop does not report a
  combinational `uio` loop; four ttihp25a designs list a `sky130_polyfill.v` their repositories do not
  contain; the frame hasher is pure Python and holds every
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

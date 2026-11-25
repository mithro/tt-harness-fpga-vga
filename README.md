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
- systemd with a user manager (`systemd-run --user --scope`) and the `memory`, `pids` and `cpu` cgroup
  controllers delegated to your user (the default on current desktop and server distributions), which
  enforces the resource limits of [SECURITY](#security). Without it, set `TTHFV_LIMITS=none` and accept the
  risk.
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
uv run tthfv run all --candidates
```

Only four designs are built at a time by default (`--jobs 4`, at most 16): each tool of each design may use
up to its whole cgroup (8 GiB of memory, four cores), and the machine is shared with other work.

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
- `tthfv reference ID... [--clocks N] [--force] [--profile ideal|boards/<name>.json] [--sensitivity]` simulates and cuts the reference frames under a stimulus profile (`ideal`, or a board's census file) and stores them under `work/reference/<id>/<profile>/`; `--sensitivity` also records whether the picture depends on power-up state or on the profile's uncertain inputs.
- `tthfv run {fetch,build,reference,all} [--only ID] [--candidates] [--jobs N] [--force-jobs] [--force] [--clocks N]` runs a stage over many designs in parallel. `--jobs` defaults to 4 and is at most 16; above 4 the memory and CPU that `jobs x limits` could commit are printed against the machine's, and `--jobs x TTHFV_LIMIT_MEMORY` above half of physical memory is refused unless `--force-jobs` is given. Run long batches under `tmux` or `nohup`: a closed terminal (SIGHUP) or a `kill` (SIGTERM) stops the run, its
workers, their tools and their scopes, and exits with 128 plus the signal number. Every run starts by emptying `work/trash/` (what earlier stages could not delete) and says how many entries it removed.
- `tthfv results [--out-md PATH]` writes the results index and the Markdown table (default
  `work/results/results.md`, beside `index.json`).
- `tthfv publish --archive DIR --check-cmd CMD [--profile boards/fpga-4.json] [--only ID ...] [--media-root DIR] [--list-skipped] [--allow-removals]`
  writes the built bitstreams and `fpga-library.json` into the video archive (see [Publishing](#publishing)).

`ID` is `<shuttle>/<macro>`, for example `tt08/tt_um_tinytapeout_logo_screensaver`.

## Publishing

`tthfv publish` writes `<archive>/<shuttle>/<macro>/<id>-<sha256[:12]>.bin` and `<archive>/fpga-library.json`
(schema 1) in the format of the
[fpga-library spec](https://github.com/mithro/tinytapeout-vga-videos/blob/main/docs/superpowers/specs/2026-11-26-fpga-library-source-design.md)
(section 1 and section 6). The archive path is never written into the repository or the index. The code is
[tthfv/publish.py](tthfv/publish.py).

| option | meaning |
|---|---|
| `--archive DIR` | the archive directory; it must exist |
| `--check-cmd CMD` | run as `CMD <archive>/fpga-library.json.tmp --bins <archive>`; must exit 0 |
| `--profile FILE` | the board profile whose reference gives the mask; its stem is the profile name |
| `--only ID` | `<shuttle>/<macro>`, repeatable |
| `--media-root DIR` | where the poster, GIF and video are looked for; default the archive |
| `--list-skipped` | print every skipped design with its reason |
| `--allow-removals` | publish even if the new index drops designs the current one lists |

A design is published when all of these hold, otherwise it is listed as skipped with its reason:

| check | field | required |
|---|---|---|
| shuttle | `shuttle` | not `calibration` |
| QSPI | `qspi` of the manifest | null |
| build | `status` | `ok` or `timing-fail` |
| build | bitstream SHA-256 | equal to `bitstream_sha256` |
| build | bitstream size | at most 262144 bytes |
| build | bytes `7E AA 99 7E` | within the first 68 bytes |
| build | `commit` | equal to the manifest's |
| numbers | `fmax_hz` | int from 1 to 1000000000 |
| numbers | `clock_hz` | int from 1 to 200000000 |
| reference (`ideal` and the board profile) | `schema` | 3 |
| reference (`ideal` and the board profile) | `key.commit` | equal to the manifest's |
| reference (`ideal` and the board profile) | `clock_hz` | equal to the build's |
| reference (`ideal` and the board profile) | `frames_complete` | equal to the number of `frames`, at least 1 |
| verdict | render verdict | matches `[a-z-]{1,32}` |
| entry | every field | passes the spec's rule |

A skipped design's reason names the field that failed.

The `id` is `library_id(shuttle, macro)`. Entries are grouped by the shuttle in `SHUTTLE_ORDER` (newest
first; a shuttle not listed fails the run), then sorted by title and id. Duplicate ids, the reserved demo
names of [tinytapeout-fpga-demos](https://github.com/fpgas-online/tinytapeout-fpga-demos) at `da7d10d` and
`idle_display` fail the run.

A poster, GIF or video is recorded only if it is a regular file, reached through no symlink, in the archive
(and, with another `--media-root`, in the media root too). An existing `fpga-library.json` must itself be a
schema 1 index or the run fails.

Writing order: the whole document is decided in memory; every final name is checked first (a `.bin` already
there must be a regular file with the same bytes, other hard links are fine) so a foreseeable collision
writes nothing; every `.bin` is written (never replaced or deleted); `fpga-library.json.tmp` is written and checked; only then is
it renamed onto `fpga-library.json`. A failed check leaves the previous index. Files are mode 0644 and
directories 0755. If the current `fpga-library.json` lists designs that the new one drops (also those that
were skipped this time), the run fails and writes nothing unless `--allow-removals` is given; the removed
ids are then printed. Publishing into `/srv/data.wafer.space` (also through a symlink) is refused until
`TTHFV_PUBLISH_LIVE=approved-by-owner` is set.

## Where things live

- `TTHFV_WORK`: the work directory (default `work/` in the repository; git-ignored). Holds `src/`
  (checkouts and `fetch.json`), `build/` (`build.json`, `design.bin`, logs), `reference/`
  (`<id>/<profile>/reference.json`, gzipped dumps, PNGs), `run/` (what the runner saw), `results/index.json` and `results/results.md`.
- `TTHFV_TOOLS`: where the toolchain is installed (default `tools/`). Tools are only ever taken from
  here, never from the host `PATH`.
- `TTHFV_SANDBOX`: `bwrap` (the default) or `none`; see SECURITY.
- `TTHFV_MIN_FREE` (default `20G`): free space that must be on the filesystem of `TTHFV_WORK` before a build or
  reference stage starts (the stage fails with an error saying so). A run also refuses to start any design (each is
  recorded as `error`) when less than `--jobs` x 2 GiB (the scratch cap) + 10 GiB is free there.
- `TTHFV_LIMITS`: unset (the default: every sandboxed tool runs in a cgroup with hard limits) or `none`;
  `TTHFV_LIMIT_MEMORY` (default `8G`), `TTHFV_LIMIT_TASKS` (default `512`, processes and threads) and
  `TTHFV_LIMIT_CPU` (default `400%`, four cores) set the limits; see SECURITY. The limits in force are
  recorded in `build.json` and `reference.json` as `"limits"`, and are part of what decides whether a
  result is reused.
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

- `ok`: a bitstream was built and every clock in nextpnr's timing report meets the design's clock.
- `timing-fail`: built, but the best of the attempts has a clock below the constraint (the reason names it);
  the bitstream is kept.
- `synth-fail`: yosys failed (or the design could not be read, or a script path was unsafe).
- `no-fit`: the design does not fit the iCE40UP5K.
- `pnr-fail`: nextpnr failed for another reason, timed out, or the design's time budget ran out, or its
  timing report was missing, unreadable or not believable (see "Where the figures come from").
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
- A status followed by `(memory limit)` or `(task limit)`: the tool hit its cgroup limit (the reason says
  which, for example `memory limit (8G) exceeded`). That is deterministic for the design at those limits, so
  the result is kept; it is redone if you raise the limit. `build.json` and `reference.json` have
  `"limited": "memory" | "tasks" | "time" | "disk" | "disk-free" | "unknown" | null`; `disk-free` and `unknown` results are never reused.
- `error`: the latest attempt raised an exception or its worker died; see `work/run/<id>/error.log`.
- `invalid`: a record file is damaged or has the wrong shape.
- `stale`: the record was made from another commit, other sources or other tool versions than the current
  ones; the reason names the field. Run the stage again.

## Where the figures come from

Nothing recorded about a build is read from the text a tool prints: a design can write lines of its choice into
the log of the tools that read it (an escaped Verilog identifier with an apostrophe in a clock name, a
`(* src = "...\nInfo: ..." *)` attribute, an elaboration-time `$display`). nextpnr-ice40 runs with
`--report`, and the host reads that JSON file (through `safefile`, at most 16 MiB): each clock's achieved
frequency and constraint, and the utilisation. A clock meets timing when this code finds `achieved >=
constraint`; no PASS or FAIL word is read. The cell counts come from yosys's `stat -json` written to a file.
The build fails closed: a report that is missing, not valid JSON, lacks `fmax` or `utilization`, holds a
value that is not a finite positive number, has a constraint more than 0.5% from the design's clock, lists no
clock for a design with registers, or lists more than 64 clocks makes the attempt `pnr-fail`, never `ok`.
Clock names are data, cleaned of control characters and cut to 200 characters.

Paths between two clock domains are judged as well, because nextpnr's `fmax` has only one figure per clock and
a path from a register on `clk` to one on a gated or derived copy of it is in neither clock's figure. The
report's `critical_paths` list the worst path for each pair of domains (`posedge A -> posedge B`, `<async>` for
a pin); the sum of a path's segment delays is the "Max delay" nextpnr prints for it. A `clk-to-clk` segment is
clock skew; it may be negative (a path from a gated or derived clock back into `clk` has one of about -1.2 ns),
and a negative one is accepted as input but counts as 0: skew never shortens a path, because a design can
manufacture it (a chain of 32 LUT buffers on a clock gave -41 ns, and a 41.8 ns multiply-accumulate path then
summed to under 1 ns in nextpnr's figure, though in hardware its data arrives after the next capture edge).
`cross_paths` records both figures, `delay_ns` as judged and `nextpnr_delay_ns` as printed. The limit is one
period of the design's clock if launch and capture are on the same kind of edge, and half a period if they are on
different edges (posedge to negedge or the other way), whatever the clocks are called. A path over its limit makes
the build `timing-fail`, and the reason states the judged delay, the limit and why ("one period: the same kind of
edge of two clocks", "half a period: launch and capture are on different edges", "negative clock skew not
credited") and names nextpnr only when nextpnr's own report marks the clock the path lies in as failing
(`"failed_by": "clock"`; otherwise `"path-rule"`: nextpnr does not judge a path between two different clocks at
all). Every judged path is recorded in `build.json` as `"cross_paths": [{"from", "to", "delay_ns",
"nextpnr_delay_ns", "limit_ns", "pass", "failed_by", "basis"}]` (at most 64; more is `pnr-fail`). A design whose
only clock goes through LUTs is one clock domain, judged by nextpnr's fmax, and has no skew segment to hide behind.
`ok` rests on nextpnr's delay figures. What is done with skew is exactly this: negative clock skew is not
credited; positive skew is added as nextpnr reports it. What this does and does not prove: nextpnr
reports only the worst path per pair of domains; two unrelated clocks have no defined phase relation, so
requiring a cross-domain path to fit one period (or half of one) is a conservative rule of thumb, not a proof
(a design that is correct by construction, for example with a clock-domain-crossing synchroniser, may be
reported as failing); paths through pins (`<async>`, for example out through a `uio` pad and back in) are not
judged at all, nor is a clock that logic may invert (nextpnr names the edge it sees, so a clock inverted by
logic can look like a posedge of another clock). A design whose registers only
touch pins has no clock-to-clock path, nextpnr then lists no clock, and the build records `"clocks": {}` with
a `timing_note` saying so. nextpnr's exit status decides
whether it failed; its log is read only after a non-zero exit, to tell a design that does not fit
(`no-fit`) from another failure. The `-sv` retry after a "syntax error" in yosys's log stays a log heuristic:
its only effect is one more attempt.

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
  no core files and 1024 open files;
- **resource limits by cgroup.** Every sandboxed tool runs in its own transient systemd user scope
  (`systemd-run --user --scope --collect`) with `MemoryMax` (default 8G) and `MemorySwapMax=0`, `TasksMax`
  (default 512) and `CPUQuota` (default 400%), and a `RuntimeMaxSec` a little above the tool's own timeout as
  a backstop. A wrapper in the scope (outside the jail) reads the scope's counters before systemd removes it
  (the files of a removed cgroup cannot be read afterwards, not even through descriptors opened earlier), and
  the host also reads them from outside every 0.2 s and once more just before killing a program on its
  timeout. A kill by the memory limit (`memory.events` `oom_kill`) or a refused fork at the task limit
  (`pids.events` `max`) makes the stage fail with `memory limit (8G) exceeded` or `task limit (512) reached`
  and the record says `"limited"`. The tool runs with `oom_score_adj` 1000 so that the kernel prefers it to
  the wrapper as its victim (without that, a design that keeps its memory in tmpfs killed the wrapper in 5 of 5
  tries here). The report is still not guaranteed: if the wrapper is killed anyway and the host's own
  readings show no limit hit, the record says `"limited": "unknown"` with the reason `resource counters
  missing: the limit wrapper was killed, most likely by the memory limit`, never a bare exit code, and such a
  result is not reused. On a timeout the process group is killed and the scope is also stopped by name; nothing
  is left behind. Nothing outlives the run either: every scope of a run is in one slice,
  `tthfv_run_<pid>_<start time>.slice`; each scope's wrapper is started with `setpriv --pdeathsig KILL`, so when the
  worker (or the standalone `build` or `reference` command) that started it dies, even by SIGKILL, the wrapper dies,
  then bubblewrap (`--die-with-parent`) and the jail with it; SIGHUP and SIGTERM unwind like Ctrl-C and stop the
  slice; the slice is stopped when the run ends; and every `run`, `build` and `reference` starts by stopping the
  slices whose owner pid is dead or has another start time (the name carries both, so the check is exact; a slice
  of a living owner is never touched; a line names what was stopped); `/dev` in the jail is read-only after its device nodes are made (bubblewrap's `--dev`
  cannot be given a size), with a 64 MiB tmpfs on `/dev/shm`; If scopes cannot be made (no user manager, controllers not delegated) the stage fails with
  an error saying how to proceed, like an unusable bwrap; `TTHFV_LIMITS=none` turns the limits off and prints
  a warning once per run (records then say `"limits": null`). The limits apply to what runs in the
  sandbox: with `TTHFV_SANDBOX=none` there are none;
- the environment is exactly `PATH` (the pinned toolchain's `bin`, `/usr/bin`, `/bin`), `HOME` and `LANG`;
- with `--die-with-parent` and the PID namespace, a timeout or Ctrl-C kills everything in the sandbox,
  including a process that called `setsid` (not if bwrap itself is killed within the first few milliseconds of
  its start-up; see Known limitations for that window and what covers it).

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
tool: past 2 GiB the stage fails. While a tool runs, a watchdog thread of the host walks `scratch/` by
descriptors every 2 seconds (every four walks' duration if a walk takes over 0.5 s) and kills the tool and its
scope at once past the same caps; the record then says `"limited": "disk"` and `scratch limit (2 GiB) exceeded
during the run`.

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

Paths the sandbox builds itself (`/usr`, `/bin`, `/lib*`, `/etc`, `/proc`, `/dev`, `/sys`, `/sandbox-home`),
the root and `/tmp` itself cannot be bound: a `TTHFV_WORK`, `TTHFV_TOOLS` or checkout there is a `UserError`.
A work directory under the host's `/tmp` works: the jail's own tmpfs on `/tmp` is mounted first and the
stage's directories on top of it.

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

**Not covered.** There is no kernel quota on disk. cgroups do not limit it: `scratch/` stays a plain on-disk
directory (the host reads its outputs after the jail has gone) and each file is limited to 1 GiB by `prlimit`. While a
tool runs, a watchdog of the host stops its scope (`"limited": "disk"`) on any of three things, each with its own
reason: the scratch directory passes 2 GiB or 200,000 entries (counting files, directories and links; a walk of it
runs in a thread of its own every 2 s, the next one at most 4 s after the last ended, and stops the moment a cap is
passed; it skips what vanishes under it, and three walks in a row that could not finish are a kill, "scratch could
not be measured (the tree kept changing)"); or, checked every second whatever the walk is doing, the free space of
the work filesystem has fallen by more than the cap plus 1 GiB since the tool started (this is what sees data the
walk cannot, such as a file unlinked while it is still open and written; other jobs writing to the same filesystem
at the same time count too, which is the conservative direction), or is below half of `TTHFV_MIN_FREE`. The latency
is the time of the walk in progress (up to a few seconds at the entry cap) plus at most 4 s plus a 1 s tick, and
the overshoot is what the writer writes in that time. Measured against a 250 MiB cap, writer about 100 MiB/s, on a
loaded machine: overshoot 270 MiB alone, 10 MiB behind 60,000 empty directories, 30 MiB behind 195,000 (their own
blocks passed the cap), 371 MiB behind a chain 30,000 directories deep, 170 MiB behind 199,000 files; a full walk
at the entry cap takes 3.8 s (60,000 directories), 8.2 s (195,000), 3.9 s (the chain), 0.6 s (199,000 files). A file
unlinked while open is caught only by the free-space rule: it needs the fall to pass the cap plus 1 GiB, so at the
default 2 GiB cap a tool can put about 3 GiB of invisible data on the work filesystem before the rule fires, per
job, and the hard floor of half of `TTHFV_MIN_FREE` is all that protects the filesystem beyond that. There is no
kernel quota. The free-space rule watches the whole filesystem, so a busy shared disk can make it kill a healthy
tool: such a kill is recorded as `"limited": "disk-free"` (the scratch rules stay `"disk"`), is never cached and is
simply retried on the next run, whereas a `disk` kill is a verdict on the design and is cached. If the free space
cannot be read (`statvfs` fails) the rule is off, one warning is printed and the record says
`"free_space_rule": "off: <why>"`; nothing is killed because of it. The io cgroup controller
(`IOWriteBandwidthMax`) is not delegated to user managers here (`cgroup.controllers` of the user slice is
`cpu memory pids`), so a bandwidth limit cannot be set and is not pretended. (A size-limited tmpfs for scratch was evaluated and rejected: the tools run as separate jails that hand each
other files, the yosys netlist to nextpnr to icepack and the Verilator model to the simulation, which a
tmpfs that dies with its jail cannot carry.) `/usr` and the toolchain are readable inside; the kernel is
shared, so a kernel or bubblewrap vulnerability is not covered; the limits are per tool invocation, so
`--jobs` times the limits is what a run could commit; and git, though locked down, still parses hostile repository data. For designs you do not trust at
all, also run as an unprivileged user, preferably in a virtual machine.

## Known limitations

- The sv2v retry (a last resort when yosys cannot read a design's sources) runs `sv2v --write=FILE ...`. sv2v is
  not in the pinned toolchain, so this usage is **not verified against a real sv2v**; it is tested only with a
  fake that honours the flag.

- **Bubblewrap's `--die-with-parent` does not cover the first milliseconds of a jail's start-up.** It takes the whole
  jail down when bwrap's parent dies, but the jail's own init (the inner bwrap, pid 1 of the new PID namespace) arms
  it only after its set-up has begun: a bwrap SIGKILLed in its first
  few milliseconds (3-240 ms observed on a loaded machine, most between 5 and 20 ms; about 6% of kills at random
  moments between 0 and 300 ms) leaves the inner bwrap, its shell and the payload running, adopted by the user manager. That is
  a property of bubblewrap itself (bwrap 0.12.0), and there is no test of it alone because it depends on timing.
  In real runs the scope's **guard** covers it: the wrapper that starts each tool in its systemd scope also starts,
  before the jail, a small process in its own session that waits for end-of-file on a pipe whose only write end the
  worker holds. The kernel closes that end however the worker dies, SIGKILL included, and the guard then writes `1`
  to the scope's `cgroup.kill`, which kills everything in the scope within milliseconds (tests/test_nothing_outlives.py
  kills workers 0-300 ms after they start a tool and requires nothing and no scope to be left after 5 s). No pid
  is involved, so pid reuse cannot fool it; when the tool ends by itself the wrapper reports the counters and then
  stops the guard. The pipe is held by the worker process; a child of the worker that inherits it (a plain `fork`
  without `exec`) would delay the kill until that child ends too, and the next run's sweep and `RuntimeMaxSec` remain
  as backstops. With `TTHFV_LIMITS=none` there is no scope and so no guard: only `--die-with-parent` applies.
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

`uv run pytest --with-limits tests/test_sandbox_stages.py ...` leaves the cgroup limits on for the tests that use the
sandbox (the suite turns them off otherwise; the limit tests always use them).

Tests that need the real toolchain are marked `needs_tools` and are skipped when it is not installed.

## Attribution and licence

Apache-2.0. See `LICENSE`.

`tthfv/harness/tt_fpga_top.v` and `tthfv/harness/tt_fpga_fabricfox.pcf` are copied unchanged from the
`harness/` directory of [fpgas-online/tinytapeout-fpga-demos](https://github.com/fpgas-online/tinytapeout-fpga-demos),
commit `d2991f0b221afa1d937bea27c5dcf9ce007c31fe` (originally from TinyTapeout/tt-fpga-compiler, Apache-2.0). The
SPDX line in `tt_fpga_top.v` reads `Apache 2.0`; that is upstream's text and the file is kept byte-identical
to upstream.

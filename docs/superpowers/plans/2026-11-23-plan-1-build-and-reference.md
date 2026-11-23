# Plan 1: Build and Reference Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From the shared manifest, build an iCE40UP5K bitstream for any Tiny Tapeout VGA design and produce its lossless reference (pin stream, frames, hashes), with no board involved.

**Architecture:** A Python package `tthfv` with one CLI. Each stage (manifest, fetch, build, reference, report) reads and writes plain files under `work/`, so any stage re-runs alone. The reference simulation writes raw `uo_out` per clock; one small frame cutter in this repo turns a per-clock pin dump into frames, and plan 2 feeds the board's capture through the same cutter. The repo depends on no sibling project's code: it reads the videos repo's data files from a pinned clone and copies the two FPGA wrapper files.

**Tech Stack:** Python 3.12, `uv`, `pytest`, `ruff`; `numpy`, `Pillow`; oss-cad-suite 2026-09-14 (yosys, nextpnr-ice40, icepack, verilator).

**Spec:** `docs/superpowers/specs/2026-11-23-tt-harness-fpga-vga-design.md`

This is plan 1 of 3. Plan 2 is hardware validation (MCU mode, full-speed check, board sharing). Plan 3 is the fan-out, QSPI emulation and publishing. Neither is written yet.

## Global Constraints

- Licence Apache-2.0; every source file starts `# SPDX-License-Identifier: Apache-2.0` (or the `//` form).
- Python only through `uv` (`uv run`, `uv sync`). Never bare `python` or `pip`.
- No files under `/tmp`. Scratch goes in the repo's ignored `work/` or `tmp/`.
- Never redirect stderr to `/dev/null`. Long commands write their full output to a log file; read the log, do not pipe through `grep`/`tail`.
- Each task is done by one sub-agent in its own git worktree on its own branch, with small commits. Nothing merges to `main` without an independent code review and an independent validation by a different agent.
- Commit trailer: `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>` (use the model that actually did the work).
- No task in this plan touches a board or any host other than this one.
- FPGA: iCE40UP5K, package SG48. Build commands: `yosys -p "synth_ice40 -top tt_fpga_top -json out.json"`, `nextpnr-ice40 --up5k --package sg48 --pcf tt_fpga_fabricfox.pcf --pcf-allow-unconstrained --freq <MHz> --json out.json --asc out.asc`, `icepack out.asc <name>.bin`.
- A design with `clock_hz` 0 uses 25,175,000 Hz.
- Reset stimulus: `ena=1`; `ui_in`, `uio_in` at their fixed values; `rst_n=0` for 10 clocks; then `rst_n=1`. Clock 0 is the first reset clock.
- Fixed input rule: the override's fixed `ui_in` if present, else the first line of its `.inputs` script, else 0. Timed scripts and gamepad scripts are not replayed.
- No code dependency on sibling projects (owner: a little duplication beats a dependency tree). Do not add `vgacap`, `ttvga` or any sibling repo to `pyproject.toml`, and do not import from them. Read their code to learn behaviour; copy small pieces with attribution where needed.
- Videos repo pinned at one commit, recorded in one place (`tthfv/pins.py`).
- Parallel jobs default to 40 and never exceed 80.

## Review Focus

- A project whose repo or commit can no longer be fetched: reported as `fetch-fail` for that design; the run continues.
- A design that never produces sync (blank, no-sync renders exist): the reference is written with `mode: null` and zero frames, not a crash.
- A design whose top module name differs from its macro name, or whose sources include a file the override replaces: built from the overridden list.
- A partial frame at the end of the simulated dump: dropped and counted, never hashed.
- Re-running a stage after a crash halfway: finished designs are skipped unless `--force`, half-written output is never mistaken for finished (write to a temporary name, then rename).

---

## File Structure

```
pyproject.toml            package metadata, console script `tthfv`
tthfv/
  cli.py                  argparse entry point, one subcommand per stage
  pins.py                 pinned versions: videos repo commit, oss-cad-suite
  paths.py                where things live under work/ and tools/
  tools.py                install and locate the toolchain
  manifest.py             Design record; load from the videos repo and extras
  stimulus.py             the fixed-input rule
  fetch.py                clone a project at its commit, apply overrides
  build.py                yosys / nextpnr / icepack for one design; build.json
  reports.py              parse yosys and nextpnr logs (utilisation, fmax)
  reference.py            Verilator run for one design; reference.json
  frames.py               per-clock pin dump -> frames -> PNG + hashes (self-contained)
  runner.py               run one stage over many designs in parallel
  results.py              results index and docs/results.md
  harness/
    tt_fpga_top.v         from tinytapeout-fpga-demos (attribution in README)
    tt_fpga_fabricfox.pcf from tinytapeout-fpga-demos
    ref_tb.cpp            reference testbench: reset, fixed inputs, dump uo_out
designs/extra.json        designs not in the videos manifest (calibration patterns)
tests/                    one test file per module; tests/data/ for fixtures
docs/results/             generated and hand-written result notes
```

Work directories (ignored): `work/videos-repo/`, `work/src/<shuttle>/<macro>/`, `work/build/<shuttle>/<macro>/`, `work/reference/<shuttle>/<macro>/`, `tools/`.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `tthfv/__init__.py`, `tthfv/cli.py`, `tthfv/paths.py`, `tthfv/pins.py`, `.gitignore`, `.github/workflows/ci.yml`, `tests/test_cli.py`, `tests/test_paths.py`
- Modify: `README.md` (add a "Developing" section: `uv sync`, `uv run pytest -q`, `uv run ruff check .`)

**Interfaces:**
- Produces: console script `tthfv`; `tthfv.cli.main(argv: list[str] | None = None) -> int`, with subcommands registered through `add_parser(sub)` functions in each stage module (later tasks add theirs).
- Produces: `tthfv.paths.REPO_ROOT: Path`, `WORK: Path` (`REPO_ROOT/"work"`, overridable by env `TTHFV_WORK`), `TOOLS: Path` (`REPO_ROOT/"tools"`, overridable by env `TTHFV_TOOLS`), and `design_dir(stage: str, design_id: str) -> Path` returning `WORK/stage/<shuttle>/<macro>`.
- Produces: `tthfv.pins.VIDEOS_REPO_URL = "https://github.com/mithro/tinytapeout-vga-videos"`, `VIDEOS_REPO_COMMIT` (the current `main` commit of TinyTapeout/tinytapeout-vga-videos, written as a 40-character hash), `OSS_CAD_SUITE_VERSION = "2026-09-14"`, `OSS_CAD_SUITE_URL = "https://github.com/YosysHQ/oss-cad-suite-build/releases/download/2026-09-14/oss-cad-suite-linux-x64-20260914.tgz"`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cli.py
from tthfv.cli import main

def test_help_exits_zero(capsys):
    import pytest
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    assert "tthfv" in capsys.readouterr().out

def test_no_subcommand_is_an_error():
    import pytest
    with pytest.raises(SystemExit) as e:
        main([])
    assert e.value.code == 2
```

```python
# tests/test_paths.py
from pathlib import Path
import importlib

def test_design_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TTHFV_WORK", str(tmp_path))
    import tthfv.paths as p
    importlib.reload(p)
    assert p.design_dir("build", "tt08/tt_um_x") == tmp_path / "build" / "tt08" / "tt_um_x"

def test_design_dir_rejects_traversal():
    import pytest, tthfv.paths as p
    with pytest.raises(ValueError):
        p.design_dir("build", "../etc/passwd")
```

- [ ] **Step 2: Run `uv run pytest -q`; expect import errors.**
- [ ] **Step 3: Create `pyproject.toml`** (hatchling build backend, `requires-python = ">=3.12"`, script `tthfv = "tthfv.cli:main"`, dev group with `pytest` and `ruff`, ruff line length 110) and the modules above. A design id must match `^[a-z0-9]+/[A-Za-z0-9_]+$`.
- [ ] **Step 4: `.gitignore`** with `work/`, `tools/`, `tmp/`, `.venv/`, `__pycache__/`, `.worktrees/`, `.claude/`.
- [ ] **Step 5: CI workflow** `ci.yml`: on push and pull request, `astral-sh/setup-uv` pinned by commit hash, then `uv sync`, `uv run ruff check .`, `uv run pytest -q`.
- [ ] **Step 6: Run `uv run pytest -q` and `uv run ruff check .`; expect all pass. Commit after each of steps 3, 4, 5.**

### Task 2: Toolchain install

**Files:**
- Create: `tthfv/tools.py`, `tests/test_tools.py`

**Interfaces:**
- Consumes: `paths.TOOLS`, `pins.OSS_CAD_SUITE_*`.
- Produces: `tools.tool(name: str) -> Path` for `yosys`, `nextpnr-ice40`, `icepack`, `verilator`; raises `ToolMissing` with the message `run: uv run tthfv tools install` when absent. `tools.env() -> dict[str, str]` returning `os.environ` with `tools/oss-cad-suite/bin` first on `PATH`. `tools.versions() -> dict[str, str]`. Subcommands `tthfv tools install` and `tthfv tools versions`.

- [ ] **Step 1: Write failing tests**: `tool("yosys")` under an empty `TTHFV_TOOLS` raises `ToolMissing` whose text contains `tthfv tools install`; with a fake executable at `<TOOLS>/oss-cad-suite/bin/yosys` it returns that path; `tool("rm")` raises `ValueError` (only the four names are allowed).
- [ ] **Step 2: Run; expect fail.**
- [ ] **Step 3: Implement.** `install` downloads the tarball to `tools/` (to a `.part` name, then renames), extracts it, and checks `tools/oss-cad-suite/bin/verilator` exists. It is a no-op when the check file exists. No checksum is published upstream, so record the tarball's SHA-256 in `tools/oss-cad-suite.sha256` on first install and print it.
- [ ] **Step 4: Run tests; expect pass. Commit.**
- [ ] **Step 5: Run `uv run tthfv tools install > work/tools-install.log 2>&1`, then `uv run tthfv tools versions`.** Expected: four version lines, yosys reporting a 2026 build. Commit nothing (tools are ignored); paste the versions into the task report.

### Task 3: Manifest and stimulus

**Files:**
- Create: `tthfv/manifest.py`, `tthfv/stimulus.py`, `designs/extra.json`, `tests/test_manifest.py`, `tests/test_stimulus.py`, `tests/data/videos-mini/` (a cut-down copy of the videos repo's `data/` with three targets: one plain, one with a fixed `ui_in` override, one with an `.inputs` script)

**Interfaces:**
- Consumes: `pins.VIDEOS_REPO_URL`, `pins.VIDEOS_REPO_COMMIT`, `paths.WORK`.
- Produces:

```python
@dataclass(frozen=True)
class Design:
    id: str                 # "<shuttle>/<macro>"
    shuttle: str
    macro: str
    title: str
    author: str
    repo: str
    commit: str
    top_module: str
    source_files: tuple[str, ...]
    clock_hz: int           # never 0: 25_175_000 substituted
    clock_declared: bool    # False when the manifest said 0
    pinout: dict            # {"ui": [...8], "uo": [...8], "uio": [...8]}
    pmods: tuple[str, ...]
    tiles: str
    render_verdict: str | None   # videos repo verdict; None for extras
    override: dict          # compiled override json, {} if none
    patch: Path | None      # override patch file, if any
    qspi: str | None        # pin map string from the render result, if any
    ui_in: int              # fixed value by the stimulus rule
    uio_in: int
    scripted: bool          # True if an .inputs or .gamepad script exists
    candidate: bool         # render_verdict == "ok"

def sync_videos_repo() -> Path            # clone/fetch to work/videos-repo at the pin
def load(videos_repo: Path | None = None, extras: Path | None = None) -> list[Design]
def by_id(designs: list[Design], id: str) -> Design     # KeyError names close matches
```

- `stimulus.fixed_inputs(override: dict, inputs_script: str | None) -> tuple[int, int]` returns `(ui_in, uio_in)`.
- Subcommands: `tthfv manifest sync`, `tthfv manifest list [--candidates] [--json]`, `tthfv manifest show <id>`.

- [ ] **Step 1: Read** `ttvga/overrides.py` (docstring and `compile_one`) and `ttvga/harness/job.py:42-90,162-182` in the TinyTapeout/tinytapeout-vga-videos repository to learn the override keys. The override may replace `source_files`, `top_module` and `clock_hz`; apply those to the `Design`.
- [ ] **Step 2: Write failing tests**

```python
def test_fixed_ui_in_from_override():
    assert fixed_inputs({"ui_in": 5}, None) == (5, 0)

def test_first_line_of_inputs_script():
    assert fixed_inputs({}, "0 3 128\n500000 1 0\n") == (3, 128)

def test_default_is_zero():
    assert fixed_inputs({}, None) == (0, 0)

def test_load_mini(mini_repo):
    ds = load(videos_repo=mini_repo, extras=None)
    assert [d.id for d in ds] == sorted(d.id for d in ds)
    assert len(ds) == 3

def test_zero_clock_becomes_25175000(mini_repo):
    d = by_id(load(videos_repo=mini_repo, extras=None), "tt05/tt_um_noclock")
    assert (d.clock_hz, d.clock_declared) == (25_175_000, False)

def test_candidate_only_when_render_ok(mini_repo): ...
def test_override_replaces_sources_and_top(mini_repo): ...
```

- [ ] **Step 3: Run; expect fail. Implement. Run; expect pass. Commit.**
- [ ] **Step 4: `designs/extra.json`**: the five calibration designs from `mithro/tt-vga-testpatterns`, each with `id` (`calibration/<top_module>`), `repo`, `commit` (current `main`), `top_module`, `source_files`, `clock_hz`. Take the names and file lists from that repo's README and `info.yaml` files, do not guess them. Extras load with `render_verdict=None`, `candidate=False`. Add a test that the file parses and every entry has the six fields. Commit.
- [ ] **Step 5: Run against the real data**: `uv run tthfv manifest sync`, then `uv run tthfv manifest list --json > work/manifest.json`. Expected: 445 designs (440 + 5), 328 candidates, 49 with `clock_declared` false, 35 with `qspi` set. If a count differs, stop and report the difference; do not adjust the code to hit the number.

### Task 4: Fetch

**Files:**
- Create: `tthfv/fetch.py`, `tests/test_fetch.py`

**Interfaces:**
- Consumes: `Design`, `paths.design_dir("src", id)`.
- Produces: `fetch.fetch(design: Design, force: bool = False) -> FetchResult` where `FetchResult` has `status: Literal["ok", "fetch-fail", "patch-fail"]`, `src_dir: Path`, `sources: tuple[Path, ...]` (absolute, existing), `include_dirs: tuple[Path, ...]`, `log: Path`, `reason: str`. Subcommand `tthfv fetch <id>... [--force]`.

- [ ] **Step 1: Read** `ttvga/harness/job.py:54-160` in the videos repo. Mirror its behaviour for: shallow fetch of exactly `commit`, submodules, applying the override `patch`, and how `source_files` paths are resolved (relative to `src/`). Do not import from the videos repo.
- [ ] **Step 2: Write failing tests** using a local bare git repo created in `tmp_path` as the "project": fetch at a commit gives the files of that commit, not of `HEAD`; a missing commit gives `fetch-fail` with the git error in `reason` and no exception; a patch that does not apply gives `patch-fail`; a source file listed but absent gives `fetch-fail` naming the file; a second call without `force` does no network work (assert by pointing the repo URL at a now-deleted path).
- [ ] **Step 3: Run; expect fail. Implement. Run; expect pass. Commit.**
- [ ] **Step 4: Real check**: `uv run tthfv fetch tt08/tt_um_tinytapeout_logo_screensaver ttihp25a/tt_um_algofoogle_vga > work/fetch-check.log 2>&1`. Expected: both `ok`. Report the two source lists.

### Task 5: Build

**Files:**
- Create: `tthfv/build.py`, `tthfv/reports.py`, `tthfv/harness/tt_fpga_top.v`, `tthfv/harness/tt_fpga_fabricfox.pcf`, `tests/test_build.py`, `tests/test_reports.py`, `tests/data/logs/` (one real yosys log and one real nextpnr log, captured in step 5)
- Modify: `README.md` (attribution for the two harness files: from `fpgas-online/tinytapeout-fpga-demos`, originally TinyTapeout/tt-fpga-compiler, Apache-2.0)

**Interfaces:**
- Consumes: `Design`, `FetchResult`, `tools.tool`, `tools.env`.
- Produces: `build.build(design: Design, fetched: FetchResult, force: bool = False) -> BuildResult`; `BuildResult` is also what `work/build/<id>/build.json` holds:

```json
{"id": "...", "status": "ok|synth-fail|no-fit|timing-fail|pack-fail",
 "reason": "...", "bitstream": "design.bin", "bitstream_sha256": "...",
 "clock_hz": 25175000, "fmax_hz": 31250000,
 "cells": {"SB_LUT4": 0, "SB_DFF": 0, "SB_RAM40_4K": 0, "SB_SPRAM256KA": 0, "SB_MAC16": 0},
 "lut_percent": 0.0, "tools": {"yosys": "...", "nextpnr-ice40": "..."},
 "seconds": 0.0}
```

- `reports.parse_nextpnr(log_text: str) -> NextpnrReport` (`fmax_hz: int | None`, `utilisation: dict[str, tuple[int, int]]`, `fit: bool`).
- Subcommand `tthfv build <id>... [--force]`.

- [ ] **Step 1: Copy** `harness/tt_fpga_top.v` and `harness/tt_fpga_fabricfox.pcf` from fpgas-online/tinytapeout-fpga-demos unchanged. The wrapper's placeholder module name is `__tt_um_placeholder`. Commit.
- [ ] **Step 2: Write failing tests for `reports`** from the two captured logs (see step 5 for capturing them first with a hand-run of the three commands on one fetched design): `fmax_hz` equals the "Max frequency for clock" figure of the log's last such line; utilisation of `ICESTORM_LC` is parsed as `(used, 5280)`; a log containing nextpnr's "Unable to place" or over-capacity error gives `fit=False`.
- [ ] **Step 3: Write failing tests for `build`** with the tools replaced by fake executables on `PATH` (shell scripts in `tmp_path` that write canned output): yosys exit 1 gives `synth-fail`; nextpnr reporting no fit gives `no-fit`; `fmax_hz < clock_hz` gives `timing-fail` and still keeps the bitstream; a bitstream not starting `7E AA 99 7E` (searching the first 64 bytes, as icepack emits a comment header first) gives `pack-fail`; `build.json` is written by rename, never left half-written.
- [ ] **Step 4: Implement.** SystemVerilog sources (`.sv`) are passed to yosys with `read_verilog -sv`; if synthesis fails and `sv2v` is on `PATH`, retry through `sv2v` once and record `"sv2v": true`. `--freq` is `clock_hz / 1e6`. Run nextpnr with `--seed 1` so results repeat.
- [ ] **Step 5: Real check**: `uv run tthfv build tt08/tt_um_tinytapeout_logo_screensaver > work/build-check.log 2>&1`. Expected: `status: ok`, a `.bin` of 104,090 bytes (the UP5K's fixed size; if the size differs, report it rather than changing the test). Save that run's yosys and nextpnr logs as the fixtures for step 2. Commit.

### Task 6: Reference testbench

**Files:**
- Create: `tthfv/harness/ref_tb.cpp`, `tthfv/reference.py`, `tests/test_reference.py`, `tests/data/tt_um_counter_vga.v` (a 20-line design: an 8-bit counter on `uo_out`, reset to 0)

**Interfaces:**
- Consumes: `Design`, `FetchResult`, `tools.tool("verilator")`.
- Produces: `reference.simulate(design: Design, fetched: FetchResult, clocks: int = 6_000_000, force: bool = False) -> SimResult` with `status: Literal["ok", "verilate-fail", "sim-fail"]`, `dump: Path` (`work/reference/<id>/uo_out.bin`, one byte per clock, byte 0 is the value after the first reset clock's rising edge), `uio: Path` (`uio.bin`, two bytes per clock: `uio_out`, `uio_oe`), `clocks: int`, `log: Path`.
- `ref_tb` command line: `ref_tb --clocks N --ui-in V --uio-in V --out DIR`.

- [ ] **Step 1: Read** `ttvga/harness/tb.cpp:330-355` and `ttvga/harness/job.py:90-160` (the verilator flags, including per-project `verilator_flags` overrides and the warning suppressions) in the videos repo. Use the same flags so the same designs verilate.
- [ ] **Step 2: Write failing tests** on the counter design: `uo_out.bin` has exactly `clocks` bytes; bytes 0 to 9 are 0 (reset held for 10 clocks); byte 10 is 1, byte 11 is 2 (the counter runs from the eleventh clock); with `--ui-in 7` and a design variant that copies `ui_in` to `uo_out`, every byte is 7; a design with a syntax error gives `verilate-fail` with the verilator message in the log and no exception.
- [ ] **Step 3: Implement `ref_tb.cpp`**: `ena=1`, inputs set, `rst_n=0`, `clk=0`, eval; then per clock: `clk=0` eval, `clk=1` eval, record; `rst_n=1` after 10 clocks. Buffered writes. No sync detection, no frame logic, no QSPI model (plan 3).
- [ ] **Step 4: Implement `reference.simulate`.** Verilated model and `ref_tb.cpp` are compiled per design under `work/reference/<id>/obj/` with `-O2`.
- [ ] **Step 5: Run tests; expect pass. Commit.**
- [ ] **Step 6: Real check**: simulate `tt08/tt_um_tinytapeout_logo_screensaver` for the default clocks. Expected: 6,000,000 bytes, and bit 7 (hsync) toggling with a period of 800 clocks (check with a ten-line script, not by eye). Report the measured hsync and vsync periods.

### Task 7: Frames and hashes

**Files:**
- Create: `tthfv/frames.py`, `tests/test_frames.py`, `tests/data/bars.bin.gz` (a simulated dump of one calibration design, 3 frames long, made with task 6)
- Modify: `pyproject.toml` (add `numpy`, `Pillow`), `tthfv/cli.py`

**Interfaces:**
- Consumes: `SimResult.dump` (one byte per clock).
- Produces:

```python
def frame_hash(rgb888: bytes) -> int        # FNV-1a 64: offset 0xcbf29ce484222325, prime 0x100000001b3
@dataclass(frozen=True)
class Frame:
    index: int; start_clock: int; width: int; height: int
    rgb888: bytes; hash: int
@dataclass(frozen=True)
class FrameSet:
    mode: str | None          # e.g. "640x480@60"; None when not in the table
    line_clocks: int | None; lines: int | None
    hsync_active_low: bool | None; vsync_active_low: bool | None
    width: int; height: int
    frames: list[Frame]       # complete frames only
    partial_frames_dropped: int
def cut_frames(samples: "numpy.ndarray", max_frames: int | None = None) -> FrameSet
def write_reference(design: Design, sim: SimResult, frames: FrameSet, n: int = 8) -> Path
```

**The frame definition** (this is the contract; plan 2's board capture and the handheld harness use it too, so write it into `docs/frame-definition.md` as part of this task):

- Pin map (Tiny VGA Pmod): `uo_out[0]`=R1, `[1]`=G1, `[2]`=B1, `[3]`=vsync, `[4]`=R0, `[5]`=G0, `[6]`=B0, `[7]`=hsync. A channel's 2-bit value is `(hi << 1) | lo`, stored in RGB888 as `value * 85`.
- Sync polarity: for each sync bit, the level it holds for less than half of the dump is the active level.
- A line starts on the clock after an hsync pulse ends. `line_clocks` is the most common distance between line starts. A frame starts on the first line start after a vsync pulse ends. `lines` is the most common number of line starts between frame starts.
- Mode table, matched on `(line_clocks, lines)` exactly, one clock per pixel:

  | mode | line_clocks | lines | first active pixel after line start | first active line after frame start | width | height |
  |---|---|---|---|---|---|---|
  | 640x480@60 | 800 | 525 | 48 | 33 | 640 | 480 |
  | 800x600@60 | 1056 | 628 | 88 | 23 | 800 | 600 |
  | 1024x768@60 | 1344 | 806 | 160 | 29 | 1024 | 768 |

- A design not in the table (odd line counts, two or a fractional number of clocks per pixel) has `mode = None` and is cut as the full raster: width `line_clocks`, height `lines`, no cropping, sync periods included. It is still compared exactly, just not cropped.
- A frame is complete when it has exactly `lines` lines each exactly `line_clocks` long and lies wholly inside the dump. Anything else is dropped and counted, never hashed.
- No sync found (fewer than three frame starts): `frames = []`, `line_clocks`/`lines` whatever was found or `None`.

`write_reference` writes `work/reference/<id>/reference.json` and gzips the dump to `uo_out.bin.gz` (removing the uncompressed file):

```json
{"schema": 1, "id": "...", "clock_hz": 25175000, "clock_declared": true,
 "reset_clocks": 10, "ui_in": 0, "uio_in": 0, "clocks": 6000000,
 "dump": "uo_out.bin.gz", "dump_sha256": "<of the uncompressed bytes>",
 "mode": "640x480@60", "line_clocks": 800, "lines": 525,
 "hsync_active_low": true, "vsync_active_low": true, "width": 640, "height": 480,
 "frames": [{"index": 0, "start_clock": 0, "hash": "0123456789abcdef", "png": "frame-000.png"}],
 "partial_frames_dropped": 1,
 "tools": {"verilator": "..."}}
```

  `hash` is 16 lowercase hex digits. `start_clock` counts from the first reset clock. Only the first `n` complete frames are listed.
- Subcommand `tthfv reference <id>... [--clocks N] [--force]` (runs tasks 6 and 7 for a design).

- [ ] **Step 1: Read, do not import**, `vgacap/src/frame/` and `tinytapeout-vga-videos/ttvga/harness/tb.cpp:237-260,360-460` for how others cut frames. Where this task's definition differs from theirs (line start edge, cropping of unknown modes), the definition above wins; note the differences in `docs/frame-definition.md`.
- [ ] **Step 2: Write failing tests.** `frame_hash(b"")` is `0xcbf29ce484222325`; `frame_hash(b"a")` is `0xaf63dc4c8601ec8c` (the published FNV-1a 64 vector). A synthetic 640x480 dump built in the test (three frames of a known gradient, active-low syncs, 96-clock hsync, 2-line vsync) cuts to `mode == "640x480@60"`, `len(frames) == 2` or more, pixel (0,0) and pixel (639,479) of frame 0 equal the gradient's values, and consecutive `start_clock`s differ by 420,000. The same dump with both syncs inverted gives identical frames and `hsync_active_low == False`. A dump cut off mid-frame drops the last frame and counts it. An all-zero dump gives `frames == []`. A synthetic 800-clock line with 506 lines gives `mode is None`, width 800, height 506. On `bars.bin.gz` the first two frames have equal hashes.
- [ ] **Step 3: Implement** with numpy over the whole dump (edge detection by comparing shifted arrays), not a per-clock Python loop: 6,000,000 samples must cut in under 5 s. Add a test asserting that bound on a 6,000,000-sample synthetic dump.
- [ ] **Step 4: Run; expect pass. Commit after each of: hash, cutter, reference writer, `docs/frame-definition.md`.**
- [ ] **Step 5: Cross-check against the published render.** For `tt08/tt_um_tinytapeout_logo_screensaver`, write the reference, then compare `frame-000.png` with the videos repo's published poster for the same design (`/srv/data.wafer.space/tinytapeout-vga-videos/tt08/<macro>/..._poster.png`, which is 2x scaled and from a later frame): report image sizes and, after downscaling the poster by 2, the fraction of differing pixels and the best whole-pixel offset. The expected outcome is "same picture, possibly shifted by a pixel or a line, different frame of the animation". Write the finding to `docs/results/2026-11-frames-vs-render.md`. It is a note, not a pass/fail. Commit.

### Task 8: Runner and results

**Files:**
- Create: `tthfv/runner.py`, `tthfv/results.py`, `tests/test_runner.py`, `tests/test_results.py`

**Interfaces:**
- Consumes: `fetch.fetch`, `build.build`, `reference.simulate`, `frames.*`, `manifest.load`.
- Produces: `tthfv run <stage> [--only ID]... [--candidates] [--jobs N] [--force]` for stages `fetch`, `build`, `reference`, `all`; a log at `work/run-<stage>-<UTC timestamp>.log` with one line per finished design, `<done>/<total> <id> <status> <seconds>s eta <HH:MM>`; `tthfv results` writing `work/results/index.json` (one record per design joining manifest, `build.json`, `reference.json` summary) and `docs/results.md` (a table: id, title, tiles, build status, LUT %, fmax against clock, mode, frames).

- [ ] **Step 1: Write failing tests** with stage functions replaced by fakes: one design raising an exception is recorded as `error` with the traceback in its log and the run continues with exit code 0 and a non-zero count in the summary line; `--jobs 200` is clamped to 80; finished designs are skipped on a second run and done again with `--force`; the progress line format matches the regular expression `^\d+/\d+ \S+ \S+ \d+(\.\d+)?s eta \d\d:\d\d$`.
- [ ] **Step 2: Implement** with `concurrent.futures.ProcessPoolExecutor`. One design's failure never stops the pool.
- [ ] **Step 3: Write failing tests for `results`**: a design with no `build.json` appears with build status `not-run`; the Markdown table is sorted by id and is byte-identical on a second run (no timestamps inside the table).
- [ ] **Step 4: Implement. Run; expect pass. Commit after each of runner and results.**

### Task 9: First batch

No new code unless a bug is found; a bug is fixed in the module that owns it, with a test, on this task's branch.

**Designs:** the five `calibration/*` designs, then `tt07/tt_um_tinytapeout_dvd_screensaver`, `tt08/tt_um_tinytapeout_logo_screensaver`, `ttihp26a/tt_um_urish_rings`, `ttihp0p3/tt_um_algofoogle_fun_vga_clock`, `ttihp25a/tt_um_algofoogle_vga`, `ttsky26a/tt_um_algofoogle_dottee`, `ttsky25a/tt_um_gamepad_pmod_demo`, `ttihp25a/tt_um_2048_vga_game`.

- [ ] **Step 1: Run** `uv run tthfv run all --only <each id> --jobs 13 > work/first-batch.log 2>&1`, then `uv run tthfv results`.
- [ ] **Step 2: For every design that is not `ok` at every stage**, read its logs and classify: a harness fault (fix it, with a test), or a property of the design (too big, too slow, needs input). Do not change a design's sources.
- [ ] **Step 3: Check the calibration designs against their own references.** `tt-vga-testpatterns` ships reference renders; each calibration design's `frame-000.png` must equal its reference render pixel for pixel. Any difference is a harness fault until shown otherwise.
- [ ] **Step 4: Write** `docs/results/2026-11-first-batch.md`: a table of the 13 designs with build status, LUT use, fmax against clock, detected mode, number of frames, and one sentence on anything unexpected. State plainly which designs do not fit or do not meet timing. Commit.
- [ ] **Step 5: Tell the `tt vga demos on game bub` session** (by session message) where `reference.json` is, its schema, and which of the eight shared designs failed to build for the UP5K.

---

## After this plan

Plan 2 (hardware validation) starts from: 13 bitstreams, 13 references, and the `reference.json` schema. Its first task is the board-sharing check (status, claims), before any bitstream is loaded.

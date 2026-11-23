# Plan 2: Hardware Bring-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get one calibration design from this project's own bitstream onto a real board and show its captured pins equal the simulated reference clock for clock, without disturbing anyone else using the boards.

**Architecture:** Host-side Python in `tthfv/board/` talks to one board through the Raspberry Pi daemon's serial WebSocket and sends MicroPython scripts to the stock firmware's raw REPL. The harness loads the FPGA itself with the clock held low; one PIO program generates the clock and samples `uo_out`; samples stream back with flow control; a comparator checks them against the reference dump.

**Tech Stack:** Python 3.12, `uv`, `websockets`; MicroPython on the RP2350 (stock Tiny Tapeout SDK, nothing flashed); the existing `tthfv` package.

**Spec:** `docs/superpowers/specs/2026-11-23-hardware-validation-design.md`

This plan covers that document's steps 1 to 3 and the software they need. Steps 4 to 6 (more designs, full-speed windows, a second board) are planned after these results are in, because the results may change them.

## Global Constraints

- Everything in plan 1's Global Constraints still applies (Apache-2.0 SPDX headers, `uv` only, nothing under `/tmp`, full output to log files, small commits, own worktree and branch, independent review and validation before merge, no code dependency on sibling projects, public repo: no private paths, user names or host names in committed files).
- The boards are public and shared. No lock exists. Before every use of a board: its status must show `clients` 0 and idle display `loaded`. Take the claim file `<workspace>/tmp/board-claims/<board>-<usb serial>.json` (owner, purpose, expiry at most 30 minutes ahead; an expired claim is void). After connecting, `clients` must be exactly 1. Re-read status between steps; if `clients` exceeds 1, stop at once, clean up, release.
- A board is identified by USB serial only: fpga-3 `8c46329b33590ecb`, fpga-4 `a2961e5cac65b25f`, tt-fd1a167bd863a198 `fd1a167bd863a198`. fpga-1 (`4df39a7a6856f86f`) belongs to another session: never touch it.
- At most one agent on a board at a time, and at most one board in use by this project at a time during this plan. Board work happens in bursts of at most 10 minutes of connected time, then release.
- Never call the daemon's `POST /bitstream` or `POST /designs/<name>/enable`. Never write to the board's filesystem. Never flash firmware.
- Never reconfigure the project clock pad (GPIO16) with `machine.Pin` while the SDK's PWM is running. Stop the PWM with the SDK's own call first.
- The MCU never drives any `uio` pin. It drives a `ui_in` pin only where the pin census shows nothing but pull resistors on that net.
- Clean-up is mandatory, including after an error or Ctrl-C: remove PIO programs, release pins to inputs, leave the raw REPL (Ctrl-B), then Ctrl-D from the friendly REPL so the SDK restarts; disconnect; about 75 s later confirm the status is `loaded` again. If it is not, stop all board work and report.
- Never power-cycle a board or a Pi without the controller's explicit go-ahead for that instance.
- Report what happened, not what was expected. A capture that does not match is a result to report with its evidence.

## Review Focus

- The board is taken by a visitor in the middle of a capture: the session aborts within one status poll, cleans up, and reports `capture-error: board taken`; no partial result is recorded as a comparison.
- The link stalls for seconds: the capture slows down and stays exact; it never drops or duplicates a sample silently.
- A script raises on the board: the host sees the traceback, cleans up, and the board is left in the SDK's start state.
- The bitstream is not the one the reference describes: the comparator refuses to compare when `sources_sha256` or commit differ between `build.json` and `reference.json`.
- A capture shorter or longer than the reference: compared over the common length, with both lengths reported.

---

## File Structure

```
tthfv/board/__init__.py
tthfv/board/status.py     read a board's public status; the "may I use it" rule
tthfv/board/claims.py     claim files between sessions
tthfv/board/link.py       WebSocket serial link; raw REPL exec; clean-up sequence
tthfv/board/session.py    context manager: check, claim, connect, verify, clean up
tthfv/board/mp/           MicroPython sources sent to the board (census, load, capture)
tthfv/board/census.py     run the pin census; write a board profile
tthfv/board/load.py       stream a bitstream to RAM and configure the FPGA, clock held low
tthfv/board/capture.py    PIO clock-and-sample capture with flow control
tthfv/compare.py          byte-for-byte comparison of a capture with a reference dump
tthfv/sandbox.py          run build and simulation tools under bubblewrap
boards/<name>.json        committed board profiles (pin census results)
docs/results/             experiment write-ups
```

---

### Task 1: Sandbox for third-party code (no board)

**Files:** Create `tthfv/sandbox.py`, `tests/test_sandbox.py`. Modify `tthfv/proc.py`, `tthfv/build.py`, `tthfv/reference.py`, `README.md`.

**Interfaces:**
- Produces: `sandbox.wrap(argv: list[str], *, cwd: Path, rw: list[Path], ro: list[Path]) -> list[str]`, returning a `bwrap` command line; `sandbox.available() -> bool`; env `TTHFV_SANDBOX` = `bwrap` (default when available) | `none`.
- `proc.run`/`run_logged` gain `sandbox: Sandbox | None`.

Requirements:
- yosys, nextpnr-ice40, icepack, sv2v, Verilator, the generated makefile build and the compiled simulation all run inside `bwrap` with: no network (`--unshare-net`); a new PID namespace and `--die-with-parent`; the whole filesystem read-only except the one directory the stage writes to (the design's build or reference directory) and a private tmpfs for `/tmp`; the user's home directory not visible at all except, read-only, the toolchain directory, the design's fetched sources and the repository's `tthfv/harness`; no environment variables passed through except `PATH`, `HOME` (pointing at an empty tmpfs) and `LANG`.
- git (fetch stage) is not sandboxed: it needs the network and runs no project code. State that in the README.
- If `TTHFV_SANDBOX=bwrap` and `bwrap` is missing or cannot create namespaces, the stage fails with a `UserError` telling the user how to proceed; it never silently runs unsandboxed. `TTHFV_SANDBOX=none` prints one warning line per run.
- `build.json` and `reference.json` record `"sandbox": "bwrap" | "none"`. It is not part of the reuse digest.

- [ ] **Step 1: Failing tests** with real `bwrap` (skip with a clear reason where it is unavailable, as CI may be): a wrapped `sh -c` cannot read a file created in the test's home-like directory outside the allowed paths; cannot write outside the `rw` directory; cannot resolve or reach the network (`getent hosts github.com` or opening a socket fails); can read the `ro` paths and write the `rw` path; a child that outlives its parent is gone after the parent is killed.
- [ ] **Step 2: Failing tests** through the real stages with the real toolchain (`needs_tools`): a tiny design whose Verilog calls `$system("touch <a path outside the reference directory>")` and `$fopen` on such a path, simulated through `reference.simulate` → the outside file is not created, and the simulation's status and log say what happened; a design with `$readmemh` of an absolute path under the test's fake home → yosys and Verilator cannot read it.
- [ ] **Step 3: Implement. Run. Commit per piece (sandbox module; proc integration; build; reference; README).**
- [ ] **Step 4: Real check:** the 13 first-batch designs through `run all --jobs 13` in a private work directory with the sandbox on. Every `bitstream_sha256` and every frame-0 hash must equal the unsandboxed values in `docs/results/2026-11-first-batch.md` / a prior run. Report wall time against the unsandboxed 234 s.

### Task 2: Comparator and long-capture frames (no board)

**Files:** Create `tthfv/compare.py`, `tests/test_compare.py`. Modify `tthfv/frames.py`.

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Comparison:
    verdict: Literal["exact", "mismatch", "refused"]
    reason: str                      # for "refused"
    compared_clocks: int
    capture_clocks: int
    reference_clocks: int
    first_diff_clock: int | None
    diff_clocks: int                 # how many clocks differ
    diff_bits: int                   # OR of all differing bits, as a mask over uo_out
    per_bit_first_diff: dict[int, int]   # bit -> first clock at which it differs
def compare(capture: "numpy.ndarray", reference_dir: Path, *, build_json: Path | None,
            offset: int = 0) -> Comparison
def find_offset(capture, reference, max_offset: int = 64) -> int | None
```

- `compare` refuses (`refused`) when `build_json` is given and its `sources_sha256`, `commit` or `top_module_used` differ from the reference's, or when the reference's `key.stimulus` is not the profile named by the caller.
- `find_offset` exists for diagnosis only: it reports the constant shift (if any) at which a mismatching capture becomes exact. A capture that is exact only at a non-zero offset is still a `mismatch`; the offset goes in the report.
- `frames`: `iter_frames(samples) ` yields frames one at a time, and `frame_hash` is vectorised (process the FNV-1a multiply in numpy over 64-bit lanes is not possible bytewise; use a chunked implementation with `int.from_bytes` tables or accept a C-speed alternative from the standard library — the implementer measures and picks; the hash VALUE must not change). Bound: cutting and hashing a 60-frame 640x480 capture (25.2 million samples) in under 30 s and under 1 GB of memory, with a test.

- [ ] **Step 1: Failing tests:** identical arrays → `exact`; one flipped bit at clock 1,234,567 → `mismatch`, that clock, that bit; capture shifted by 3 clocks → `mismatch`, and `find_offset` returns 3; capture longer and shorter than the reference; `refused` on a differing `sources_sha256`; the real first-batch reference of `calibration/tt_um_vgacal_bars` against its own dump → `exact`.
- [ ] **Step 2: Failing tests for `iter_frames`:** same frames and hashes as `cut_frames` on the fixtures; the time and memory bound.
- [ ] **Step 3: Implement. Run. Commit per piece.**

### Task 3: Board link, status rule and claims (first board contact; fpga-4)

**Files:** Create `tthfv/board/{__init__,status,claims,link,session}.py`, `tests/test_board_status.py`, `tests/test_board_claims.py`, `tests/test_board_link.py`, `docs/results/2026-11-board-link.md`.

**Interfaces:**
- `status.read(slug: str) -> BoardStatus` (`usb_serial`, `clients`, `idle_state`, `reachable`, raw JSON); `status.usable(s: BoardStatus) -> tuple[bool, str]` implementing the rule in Global Constraints.
- `claims.take(board, usb_serial, purpose, minutes) -> Claim`, `claims.release(claim)`, `claims.current(board) -> Claim | None` (expired = none); atomic creation (exclusive create); the directory comes from env `TTHFV_BOARD_CLAIMS` with a default the controller supplies at dispatch.
- `link.Link`: `open(slug)`, `exec_raw(source: str, timeout_s) -> bytes` (raises `BoardError` with the board's traceback), `read_exact(n)`, `write(b)`, `cleanup()` (the Ctrl-B, Ctrl-D sequence), `close()`.
- `session.board_session(slug, purpose)` — context manager doing: status check, claim, connect, verify `clients == 1` and the USB serial, yield a `Link`, and on ANY exit path: cleanup, close, release claim, then poll status until `loaded` (up to 120 s) and raise if it never is.
- CLI: `tthfv board status [<slug>...]`, `tthfv board probe <slug>`.

- [ ] **Step 1: Read**, without importing, how the raw REPL is driven in `mithro/vgacap` `python/ttcap/repl.py` and what its notes say about the raw REPL's output framing (an unescaped 0x04 terminates output, so binary data needs a length-prefixed framing of our own), and the daemon's bridge (`fpgas-online/fpgas.online-tt` `bridge.py`, `server.py`) for the WebSocket's framing. The public URL form is `wss://tinytapeout.fpgas.online/ws/board/<slug>/serial`; status is `https://tinytapeout.fpgas.online/board/<slug>/status.json`.
- [ ] **Step 2: Failing unit tests** with a fake WebSocket server in-process (no board): status rule for each idle state and client count; claim taken twice → second fails; expired claim is replaced; `exec_raw` returns output and raises on a traceback; `board_session` runs cleanup and releases the claim when the body raises and when it is interrupted; it refuses when status is not usable; it aborts when `clients` rises above 1.
- [ ] **Step 3: Implement. Run. Commit per module.**
- [ ] **Step 4: First contact, fpga-4, read-only on the board.** `tthfv board probe fpga-4`: inside a session, run one script that only READS: `sys.implementation`, `os.uname()`, the SDK version, `machine.freq()`, free heap, the RP2350 chip revision if the firmware exposes it, the name of the enabled design, and whether the project clock PWM is running. No pin is reconfigured. Then leave. Record: connect time, script round-trip time, whether `clients` went to 1 and back to 0, and how long the status took to return to `loaded`.
- [ ] **Step 5: Write `docs/results/2026-11-board-link.md`** with exactly what was observed, including anything that differed from the design document.

### Task 4: Pin census (fpga-4)

**Files:** Create `tthfv/board/census.py`, `tthfv/board/mp/census.py`, `boards/fpga-4.json`, `tests/test_census.py`, `docs/results/2026-11-pin-census-fpga-4.md`.

**Interfaces:**
- Produces a board profile, `boards/<name>.json`: `usb_serial`, `date`, per Tiny Tapeout net (`ui_in[0..7]`, `uio[0..7]`, `uo_out[0..7]`, `clk`, `rst_n`): `read_pulldown`, `read_pullup`, `class` (`floating` = follows both pulls; `pulled-high`/`pulled-low` = follows one; `driven-high`/`driven-low` = follows neither), plus `ties` (pairs of nets that follow each other), `mcu_may_drive` (list of `ui_in` bits), and `stimulus` = `{"ui_in": int, "uio_in": int, "ui_in_mask": int}` — the values the board will present with the MCU driving what it may and leaving the rest.
- CLI: `tthfv board census <slug> [--out boards/<name>.json]`.

- [ ] **Step 1: Failing unit tests** for the classification and for deriving `ties`, `mcu_may_drive` and `stimulus` from raw readings (pure functions, no board).
- [ ] **Step 2: Implement** the MicroPython script: with the SDK's clock PWM left exactly as found and NO output driven, for each net set the MCU pin as input with pull-down, wait 2 ms, read; pull-up, wait, read; restore the pin's original mode and pull as the SDK had it (read it first). For tie detection, drive nothing: instead, for each pair candidate named in the design document (`ui_in[k]`/`uio[k]`, k = 1..3), apply pull-up on one and pull-down on the other and see which wins and whether they read equal; that is evidence, recorded as such, not proof.
- [ ] **Step 3: Pi side, read-only.** If the Pi attached to fpga-4 can be reached over SSH with the access the controller describes at dispatch, record `pinctrl get` for the HAT's GPIOs and `lsmod` filtered for SPI modules into the write-up. If it cannot be reached, say so; do not go looking for other access.
- [ ] **Step 4: Run on fpga-4** inside a board session, with the idle display's design loaded (as found). Then run it a second time and confirm the two profiles agree.
- [ ] **Step 5: Write up**: the table, what it means for which inputs can be set, how it compares with the design document's expectations (`ui_in[0]` reading 1; `ui_in[1:3]` tied to `uio[1:3]`), and what stimulus profile follows. Commit profile and write-up.

### Task 5: Reference for a board profile (no board)

**Files:** Modify `tthfv/reference.py`, `tthfv/harness/ref_tb.cpp`, `tthfv/frames.py`, `docs/frame-definition.md`. Create `tests/test_reference_profile.py`.

**Interfaces:**
- `tthfv reference <id> --profile <ideal|boards/<name>.json>`; references are stored per profile under `work/reference/<id>/<profile name>/`; `ideal` keeps today's behaviour and location semantics (migrate: `ideal` is the default profile name).
- The testbench takes: fixed `ui_in`; per-`uio`-bit level for undriven nets; a list of ties `ui_in[k] == uio[k]` meaning one net: when the design enables `uio[k]` as an output the net takes the design's value and `ui_in[k]` reads it; otherwise both read the fixed level. It also writes `uo_out_low.bin` (the value in the low phase of each clock) and reference.json gains `"phase_sensitive": bool` (any clock where low-phase and high-phase values differ).
- `tthfv reference <id> --sensitivity`: simulates twice more with randomised initial state (two seeds) and records `"reset_determined": bool` (output identical from clock 10 on) in reference.json.
- The reference key includes the whole profile.

- [ ] **Step 1: Failing tests** with small designs: a design copying `ui_in` to `uo_out` under a profile with `ui_in` = 0x01; a tie where the design drives `uio[1]` and reads `ui_in[1]`; a design with a negative-edge register → `phase_sensitive` true; a counter without reset → `reset_determined` false, with reset → true; `ideal` reproduces the existing first-batch hash for `calibration/tt_um_vgacal_bars`.
- [ ] **Step 2: Implement. Run. Commit per piece. Update the frame definition's stimulus section.**
- [ ] **Step 3:** Generate references for the five calibration designs under the fpga-4 profile and report which differ from `ideal` and why (the `modes` design reads `ui_in`).

### Task 6: Load and clock-exact capture (fpga-4)

**Files:** Create `tthfv/board/load.py`, `tthfv/board/capture.py`, `tthfv/board/mp/load.py`, `tthfv/board/mp/capture.py`, `tests/test_board_capture.py`, `docs/results/2026-11-first-hardware-capture.md`.

**Interfaces:**
- `load.configure(link, bitstream: bytes) -> None`: clock stopped and held low, `rst_n` low, bitstream streamed to a RAM buffer on the board with a checksum verified on the board, FPGA configured through the SDK's loader function without the project-enable path.
- `capture.run(link, *, clocks: int, reset_clocks: int = 10, ui_in: int, ui_in_mask: int, clock_hz: int = 500_000) -> numpy.ndarray`: one PIO program generating the clock and sampling `uo_out` in the high phase; `rst_n` released between edge 9 and edge 10; samples streamed with credit-based flow control in length-prefixed, checksummed blocks; returns exactly `clocks` samples or raises.
- CLI: `tthfv board validate <slug> <design id> [--clocks N]` → loads the design's built bitstream, captures, compares with the board-profile reference, prints the `Comparison`, writes `work/validate/<id>/<board>/result.json` and, on mismatch, difference images of the first differing frame.

This task is exploratory. Work in this order and stop to report at the first step that fails in a way you cannot explain:

- [ ] **Step 1: Read**, without importing, `mithro/vgacap` `python/ttcap/mp/capture_rp2.py` (PIO on this board: PIO block 1, `remove_program` then `gpio_base(16)`, absolute pin numbers, pad initialisation, DMA use, slot leak) and `mithro/tt-vga-capture` `docs/research/2026-09-15-rp2350-micropython-pio-findings.md`; the SDK's `ttboard/fpga/fabricfoxv2.py` loader and how the daemon calls it (`fpgas-online/fpgas.online-tt` `designs.py`); the SDK's clock and reset calls in `ttboard/demoboard.py`.
- [ ] **Step 2: Host-side unit tests** (no board) for the block framing, checksum, credit accounting and reassembly, against a fake board that stalls, duplicates a block, corrupts a block and disconnects.
- [ ] **Step 3: Recoverability first.** On fpga-4, in a session: stop the PWM with the SDK call, take the clock pin as a PIO-driven output held low, hold `rst_n` low, do nothing else, then clean up. Confirm the board returns to `loaded`. Only then go on.
- [ ] **Step 4: Load** `calibration/tt_um_vgacal_bars` (this project's bitstream) with the clock held low. Evidence that it configured: step a few thousand clocks by PIO and see hsync toggle on `uo_out[7]` with a period of 800 clocks.
- [ ] **Step 5: Capture** 1,000,000 clocks of `tt_um_vgacal_bars` from clock 0 and compare with the reference. Then `tt_um_vgacal_counter` and `tt_um_vgacal_prbs` (animated; 3,400,000 clocks each). For any mismatch report the first differing clock, the bit mask, and what `find_offset` says.
- [ ] **Step 6: Repeat** each capture a second time: two captures of the same design must be byte-identical to each other. If they are identical to each other but not to the reference, the difference is systematic and the write-up says where.
- [ ] **Step 7: Deliberately stall the link** for 3 s in the middle of a capture (stop reading on the host): the capture must still be exact.
- [ ] **Step 8: Write up** `docs/results/2026-11-first-hardware-capture.md`: what was loaded (bitstream sha256), on which board, the comparisons, the measured throughput and wall time per design, the board time used, every surprise, and what the result means for steps 4 to 6 of the design.

---

## After this plan

With one design exact on one board, the next plan covers the remaining designs, the full-speed windows and frequency sweep, a second board, and the library entry rule (`verdict_passes()`, which the owner writes).

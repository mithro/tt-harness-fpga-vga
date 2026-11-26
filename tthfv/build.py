# SPDX-License-Identifier: Apache-2.0
"""Build stage: turn a fetched design into an iCE40UP5K bitstream with yosys, nextpnr and icepack.

A failed build is a result, not an exception: build() returns a BuildResult whose status says what
went wrong, and the same record is written to WORK/build/<id>/build.json (by rename, so the file is
either absent or complete).

status is one of ok, synth-fail (yosys), no-fit (design too big for the device), pnr-fail (nextpnr
failed or timed out for another reason), pack-fail (icepack failed or its output is not a bitstream)
and timing-fail (built, but fmax is below the clock; the best bitstream is kept).

Timing closure is a bounded sweep over recipes (a synthesis variant and a nextpnr seed). Synthesis
runs once per variant, place-and-route once per recipe, and the sweep stops at the first recipe whose
fmax meets the clock. No variant moves or merges registers, so reset behaviour is the design's own.

Sources are read as the file extension says (.sv with -sv). If yosys fails, every file is read again
with -sv (what the Tiny Tapeout hardening flow does; build.json records sv_reader), and only then, if
sv2v is installed, through sv2v.

Files in WORK/build/<id>/:

    scratch/                         the only directory the tools can write (netlists, placed designs, the
                                     bitstream as icepack wrote it); the host reads back only the files it
                                     expects, through tthfv.safefile, and deletes it afterwards (unless
                                     TTHFV_KEEP_SCRATCH=1). The rest of this directory is bound read-only.
    tt_fpga_top.v                    the harness wrapper with the design's top module substituted in
    yosys-<synth>.ys, .log, out-<synth>.json    synthesis script, output and netlist
    nextpnr-<synth>-<seed>.log       one per attempt
    out.asc, design.bin              the kept attempt's placed-and-routed design and bitstream
    build.json                       written last
"""

import argparse
import dataclasses
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from tthfv import fetch, macros, manifest, paths, pdkcells, proc, reports, safefile, sandbox, tools
from tthfv.errors import UserError
from tthfv.fetch import FetchResult
from tthfv.manifest import Design
from tthfv.text import strip_control

Status = Literal["ok", "synth-fail", "no-fit", "pnr-fail", "timing-fail", "pack-fail"]

HARNESS = Path(__file__).parent / "harness"
PLACEHOLDER = "__tt_um_placeholder"
PREAMBLE = b"\x7e\xaa\x99\x7e"
BITSTREAM = "design.bin"
SCRATCH = "scratch"  # under the build directory: the one directory the sandbox may write
MAX_REPORT = 16 << 20  # bytes of nextpnr's --report JSON and of yosys's stat -json the host reads
_STAMP_VERSION = (
    6  # 6: clock-to-clock paths judged too; 5: figures from nextpnr's JSON report and yosys's stat -json
)

# Default timeouts, in seconds. A tool that runs over is killed together with its child processes.
YOSYS_TIMEOUT = 900
NEXTPNR_TIMEOUT = 900  # for one place-and-route attempt
ICEPACK_TIMEOUT = 120
SV2V_TIMEOUT = 300
DEFAULT_DESIGN_TIMEOUT = 3600  # for the whole build of one design; see build(design_timeout=...)

# Synthesis variants: extra synth_ice40 flags. In the pinned yosys, abc9 is already the default
# mapper (synth_ice40 has no -abc9 option), so the second variant maps flip-flops without clock
# enables instead (-nodffe), which changes the logic structure but not any register's behaviour.
SYNTH_FLAGS: dict[str, tuple[str, ...]] = {"default": (), "nodffe": ("-nodffe",)}
SEEDS = (1, 2, 3, 4)
RECIPES: tuple[dict, ...] = tuple({"synth": s, "seed": n} for s in SYNTH_FLAGS for n in SEEDS)
DEFAULT_MAX_ATTEMPTS = len(RECIPES)

# The synthesis ladder's version, recorded in build.json. 1: the SystemVerilog-mode retry and the PDK cell models.
# A synth-fail recorded by an older ladder is not reused (a newer rung may get further); any other result is: a
# design whose first synthesis worked never reaches the new rungs, so its result is the same.
LADDER = 1
# yosys errors that mean "this construct needs SystemVerilog mode": a .v file failing with one is read with -sv,
# as the Tiny Tapeout hardening flow reads every file. The first three say so; the last three are what Verilog
# mode says of constructs legal only in SystemVerilog (an input port declared as a variable, a declaration in an
# unnamed block or function body, which Verilog mode then reports as undeclared or of unknown width).
SV_MODE_ERRORS = (
    "only supported in SystemVerilog mode",
    "can only be omitted in SystemVerilog mode",
    "In pure Verilog (not SystemVerilog)",
    "is declared as register",
    "is implicitly declared and `default_nettype is set to none",
    "Failed to detect width for identifier",
)
# For a design read with PDK models only: before synthesis, on a copy of the design, fail if a combinational loop
# runs through a model (a ring oscillator or delay line: the cell's analog delay is the design's function).
PDK_LOOP_SET = pdkcells.LOOP_SET
# Latch cells become LUT loops instead of failing synthesis. Like PDK_LATCH_PNR_FLAGS this applies to the whole
# design, so it also lets through latches and loops the design's own RTL infers. That is acceptable only because
# the publisher marks every build with pdk_latches true with `pdk_latches: true`, which the readers show as a warning.
PDK_LATCH_FLAGS = ("-latches", "warn")
# yosys keeps file-scope declarations of the design's files (a `parameter Q` outside any module, a `define) in its
# Verilog global context, and synth_ice40 then reads the toolchain's ice40/cells_sim.v in that context, where they
# collide with the cell library's own names. A design whose synthesis fails with an error located in a file of
# the toolchain is synthesised again with this command between its sources and synth_ice40.
RESET_VLOG = "design -reset-vlog"


@dataclass(frozen=True)
class BuildResult:
    id: str
    status: Status
    reason: str = ""
    bitstream: str | None = None  # file name inside the build directory
    bitstream_sha256: str | None = None
    clock_hz: int = 0
    fmax_hz: int | None = None  # the main clock's (clk, clk$...; else the slowest) in the kept attempt
    clocks: dict[str, dict] = field(default_factory=dict)  # kept attempt: clock -> {"fmax_hz", "pass"}
    cells: dict[str, int] = field(default_factory=dict)
    lut_percent: float | None = None  # logic-cell (ICESTORM_LC) utilisation of the 5280 on the UP5K
    tools: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0
    sv2v: bool = False  # True if synthesis only worked after converting the sources with sv2v
    sv_reader: bool = False  # True if synthesis only worked reading every file with read_verilog -sv
    inputs_sha256: str = ""  # digest of everything that decides the result; used to reuse a build
    recipe: dict | None = None  # {"synth", "seed"} of the kept attempt
    attempts: list[dict] = field(default_factory=list)  # {"synth", "seed", "status", "fmax_hz"} each
    # What was built, so a record can be matched against the current manifest and fetch.
    top_module_used: str = ""
    sources_sha256: str = ""  # fetch.sources_sha256 of the sources it was built from
    commit: str = ""
    repo: str = ""
    verilator_flags: list[str] = field(default_factory=list)  # override flags; the -D ones reach yosys too
    # True if a tool timeout or the design budget ended the build or cut the sweep short. The inputs
    # digest does not include the timeouts, so such a result is never reused: the next run redoes it.
    timed_out: bool = False
    # "bwrap" or "none": whether the tools ran in the sandbox. Not part of inputs_sha256, so a result is
    # reused whichever way it was made.
    sandbox: str = ""
    # The cgroup limits the tools ran under ({"memory", "tasks", "cpu"}), or None (no sandbox, or
    # TTHFV_LIMITS=none). Part of inputs_sha256: raising a limit redoes a build.
    limits: dict | None = None
    # "memory", "tasks", "disk" or "time" if a resource limit ended the build (status and reason say which);
    # "unknown" if the wrapper that reports on the limits was itself killed; else None.
    limited: str | None = None
    free_space_rule: str = (
        ""  # "off: <why>" when the disk watchdog's free-space check could not run in some tool
    )
    # The first tool's note that free space fell by more than the scratch cap plus the margin while it ran but its
    # own writes did not account for it (other processes on the filesystem): not a limit, the tool went on.
    free_space_note: str = ""
    # True if the result says nothing lasting about the design and is never reused: a tool timeout or the design
    # budget (timed_out, the more specific flag, is also set), or a scratch directory that could not be removed.
    transient: bool = False
    # Paths between two different clock domains (or the two edges of one clock) that nextpnr's per-clock fmax does
    # not judge: [{"from", "to", "delay_ns", "limit_ns", "pass"}]; see tthfv.reports. A failing one is a timing-fail.
    cross_paths: list[dict] = field(default_factory=list)
    timing_note: str = ""  # how the figures were judged, when that needs saying (for example: no clock paths)
    peak: dict | None = (
        None  # largest {"memory" bytes, "tasks"} of any tool, from its cgroup; None: no limits
    )
    # PDK cell models (tthfv.pdkcells) the build read, because yosys failed without them; [] for nearly every
    # design. pdk_latches: some of them are latch cells (LUT loops here; the publisher skips such a design).
    pdk_cells: list[str] = field(default_factory=list)
    pdk_latches: bool = False
    pdk_note: str = ""  # what the FPGA cannot reproduce of those cells, when there is something
    sv_reader_rule: str = ""  # the yosys error line that started the retry reading every file with -sv
    ladder: int = 0  # LADDER of the code that made the record; 0: before the coverage rules
    reset_vlog: bool = False  # True if synthesis only worked with RESET_VLOG before synth_ice40
    # Set on an earlier record that was kept because a later rebuild of the same inputs was cut short and ended worse
    # (no bitstream, or a slower main clock): when, how it was cut short, and why this record was kept. See _settle.
    rebuild_note: str = ""


class _Budget:
    """The wall-clock budget of one design's build. Tool timeouts are cut down to what is left of it."""

    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds
        self.timed_out = False  # a tool run through this budget was killed on its timeout
        self.sandbox: sandbox.Sandbox | None = None  # what every tool of this build runs in; None: none
        self.limited: str | None = None  # the first resource limit a tool hit: "memory", "tasks", "disk", ...
        self.free_space_rule = ""  # "off: <why>" if the free-space check of the disk watchdog could not run
        self.free_space_note = ""  # see BuildResult.free_space_note
        self.peak = {"memory": 0, "tasks": 0}
        self.scratch: Path | None = None  # the directory the tools write; checked after each tool

    @property
    def left(self) -> float:
        return self.deadline - time.monotonic()

    @property
    def expired(self) -> bool:
        return self.left <= 0

    def limit(self, default: float) -> float:
        return max(1.0, min(default, self.left))


_DESIGN_TIMEOUT = "design timeout"
LIMIT_RC = -2  # what _run returns for a tool that hit a cgroup limit; its text is the reason


def _sv2v() -> str | None:
    found = tools.sv2v()
    return str(found) if found else None


def _tool_versions() -> dict[str, str]:
    out = {name: tools.version_line(tools.tool(name)) for name in ("yosys", "nextpnr-ice40")}
    out["icepack"] = tools.icepack_version()
    if sv2v := tools.sv2v():
        out["sv2v"] = tools.version_line(sv2v)
    return out


def _synth_command(synth: str, latches: bool = False) -> list[str]:
    return ["synth_ice40", "-top", "tt_fpga_top", *(PDK_LATCH_FLAGS if latches else ()), *SYNTH_FLAGS[synth]]


def _pdk_loop_check(top: str, cells_file: Path) -> list[str]:
    return pdkcells.loop_check(top, cells_file)


def _pnr_flags(clock_hz: int, seed: int) -> list[str]:
    """nextpnr arguments that do not name files."""
    return [
        "--up5k", "--package", "sg48", "--pcf-allow-unconstrained",
        "--freq", f"{clock_hz / 1e6:g}", "--timing-allow-fail", "--seed", str(seed),
    ]  # fmt: skip


# For a design with PDK latch cells only: a LUT latch is a combinational loop, which nextpnr's timing analysis
# otherwise refuses ("Timing analysis failed due to combinational loops"). Design-wide: it also hides the design's
# own combinational loops from the timing analysis, acceptable only because such a build is never published.
PDK_LATCH_PNR_FLAGS = ("--ignore-loops",)
# nextpnr's message for a cell yosys left unmapped: a module the design declares empty (yosys makes an empty
# module a blackbox) is a placeholder for a hard macro, whose function is not in the sources.
_UNSUPPORTED_CELL = re.compile(r"ERROR: cell type '([A-Za-z_][\w$]*)' is unsupported")


def _hard_macro_reason(text: str, fetched: FetchResult) -> str:
    """'needs analog/hard macro <name>' if nextpnr rejected a cell that is a module of the design's own, else ''."""
    m = _UNSUPPORTED_CELL.search(text)
    if m and m.group(1) in fetch.declared_modules(fetched.synth_sources):
        return f"needs analog/hard macro {m.group(1)}"
    return ""


def defines(flags: list[str]) -> list[str]:
    """The macro definitions among Verilator flags, as "NAME" or "NAME=VALUE": -DNAME[=VALUE] and
    +define+NAME[=VALUE][+NAME...]. Yosys must see the same ones."""
    out: list[str] = []
    for f in flags:
        if f.startswith("+define+"):
            out += [d for d in f[len("+define+") :].split("+") if d]
        elif f.startswith("-D") and len(f) > 2:
            out.append(f[2:])
    return out


def _inputs_digest(
    design: Design,
    fetched: FetchResult,
    wrapper: str,
    versions: dict[str, str],
    max_attempts: int,
    limits: dict | None = None,
    pdk: pdkcells.Plan | None = None,
    reset_vlog: bool = False,
) -> str:
    """Everything that decides the result: the design's sources (one digest of the whole src tree,
    shared with the reference stage), the wrapper, the pcf, tool versions, and every command line
    (yosys synthesis commands, nextpnr flags, recipes). pdk: the PDK cell models the build reads, for the few
    designs that need them (their names, the model file's digest, the synthesis flags and the loop check);
    without it the digest is exactly the one of the code before the models existed."""
    h = hashlib.sha256()
    meta = {
        "v": _STAMP_VERSION,
        "top": design.top_module,
        "top_module_used": fetched.top_module_used,
        "repo": design.repo,
        "commit": design.commit,
        "sources_sha256": fetched.sources_sha256,
        "synth_sources": [_name(p, fetched) for p in fetched.synth_sources],
        "verilator_flags": _verilator_flags(design),
        "clock_hz": design.clock_hz,
        "tools": versions,
        "max_attempts": max_attempts,
        "yosys": {s: _synth_command(s) for s in SYNTH_FLAGS},
        "nextpnr": {f"{r['synth']}/{r['seed']}": _pnr_flags(design.clock_hz, r["seed"]) for r in RECIPES},
        "report": ["nextpnr --report", "yosys stat -json"],
        "recipes": list(RECIPES),
        "limits": limits,
    }
    if pdk is not None:
        meta["pdk"] = {
            **pdk.key_fields(),
            "yosys": {s: _synth_command(s, pdk.latches) for s in SYNTH_FLAGS},
            "nextpnr": list(PDK_LATCH_PNR_FLAGS) if pdk.latches else [],
            "loop_check": _pdk_loop_check(fetched.top_module_used, Path(pdkcells.FILE_NAME)),
        }
    if reset_vlog:  # only for a design whose file-scope declarations broke the toolchain's cell library
        meta["reset_vlog"] = RESET_VLOG
    h.update(json.dumps(meta, sort_keys=True).encode())
    h.update(wrapper.encode())
    h.update((HARNESS / "tt_fpga_fabricfox.pcf").read_bytes())
    return h.hexdigest()


def _name(path: Path, fetched: FetchResult) -> str:
    return str(path.relative_to(fetched.src_dir) if path.is_relative_to(fetched.src_dir) else path)


def _verilator_flags(design: Design) -> list[str]:
    return [str(f) for f in design.override.get("verilator_flags") or []]


def _run(args: list[str], log: Path, cwd: Path, budget: _Budget, timeout: float) -> tuple[int, str]:
    """Run a tool (in the sandbox, if there is one) with the pinned toolchain on PATH, writing its output
    to log. Returns (rc, output);
    rc is -1 if the tool timed out (it and its child processes are killed), which is noted on budget.
    timeout is the tool's own limit, cut down to what is left of the budget."""
    scratch = budget.scratch
    done = proc.run_logged(
        args, budget.limit(timeout), log, cwd=cwd, env=tools.env(), append=False, header=False,
        watch=("syntax error", "error"), sandbox=budget.sandbox,
        scratch_check=(lambda: safefile.scratch_exceeded(scratch)) if scratch is not None else None,
        scratch_dir=scratch,
    )  # fmt: skip
    budget.peak["memory"] = max(budget.peak["memory"], done.peak_memory or 0)
    budget.peak["tasks"] = max(budget.peak["tasks"], done.peak_tasks or 0)
    if done.timed_out and done.limited in (None, "time"):
        budget.timed_out = True
        return -1, done.text
    if done.free_space_rule.startswith("off"):
        budget.free_space_rule = budget.free_space_rule or done.free_space_rule
    budget.free_space_note = budget.free_space_note or done.free_space_note
    if done.limited in ("disk", "disk-free"):
        budget.limited = budget.limited or done.limited
        return LIMIT_RC, f"ERROR: {done.detail}"
    if (
        done.limited in ("memory", "tasks", "unknown")
        and budget.sandbox is not None
        and budget.sandbox.limits
    ):
        # the kernel killed something in the scope, or refused it a process, or the wrapper that reports on the
        # scope was itself killed: whatever the exit code says, the tool did not run to its end within bounds
        budget.limited = budget.limited or done.limited
        return LIMIT_RC, f"ERROR: {budget.sandbox.limits.reason(done.limited)}"
    if budget.scratch is not None and (over := safefile.scratch_overrun(budget.scratch)):
        return 1, f"{done.text}\nERROR: {over}"
    return done.rc, done.text


def _why(output: str, rc: int, tool: str) -> str:
    """The most telling line of a failed tool's output: the last ERROR line, else the last line."""
    if rc == -1:
        return f"{tool} timed out"
    if rc == LIMIT_RC:
        return f"{tool}: {output.removeprefix('ERROR: ')}"
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    errors = [ln for ln in lines if "ERROR" in ln]
    detail = (errors or lines or ["no output"])[-1]
    return f"{tool} exit status {rc}: {detail}"


_UNSAFE = re.compile(r'["\\;#`\n\r]')
_DEFINE = re.compile(r"^[A-Za-z_]\w*(=[^\s\"\\;#`]*)?$")


def _unsafe_paths(*groups) -> str:
    """Return a reason naming the first path that cannot be put safely in a yosys script, or ''.
    File names come from third-party repositories, and yosys reads quotes, ';', '#' and backticks."""
    for group in groups:
        for p in group:
            if _UNSAFE.search(str(p)):
                return f'unsafe character in path {str(p)!r}: script paths may not contain " \\ ; # ` or newlines'
    return ""


def _bad_defines(defs: list[str]) -> str:
    """A reason naming the first macro definition that cannot be put safely in a yosys script, or ''."""
    for d in defs:
        if not _DEFINE.match(d):
            return f'unsafe macro definition {d!r}: want NAME or NAME=VALUE without spaces or " \\ ; # `'
    return ""


def _yosys_script(
    files: list[Path],
    include_dirs: list[Path],
    out_json: Path,
    sv: bool,
    synth: str,
    all_sv: bool = False,
    defs: tuple[str, ...] = (),
    pdk: tuple[Path, str, bool] | None = None,
    reset_vlog: bool = False,
) -> str:
    """sv: read .sv files with -sv. all_sv: read every file with -sv, as the Tiny Tapeout hardening
    flow does (some designs use SystemVerilog constructs such as `>>=` in plain .v files). defs are
    the design's macro definitions (-D). pdk: (the PDK model file, the design's top module, latches) for a
    design that needs the models: the file is read last, the loop check runs before synthesis, and latch
    cells are allowed to become LUT loops. reset_vlog: clear the Verilog global context after reading
    (RESET_VLOG)."""
    inc = " ".join([*(f"-D {d}" for d in defs), *(f'-I "{d}"' for d in include_dirs)])
    reads = [
        f'read_verilog {"-sv " if all_sv or (sv and f.suffix == ".sv") else ""}{inc} "{f}"'.replace("  ", " ")
        for f in [*files, *([pdk[0]] if pdk else [])]
    ]
    checks = [RESET_VLOG] if reset_vlog else []
    checks += _pdk_loop_check(pdk[1], pdk[0]) if pdk else []
    command = _synth_command(synth, latches=True) if pdk and pdk[2] else _synth_command(synth)
    synth_line = " ".join(command) + f' -json "{out_json}"'
    # The cell counts come from this file, not from the log, which the design can write into.
    stat_line = f'tee -q -o "{stat_path(out_json)}" stat -json'
    return "\n".join([*reads, *checks, synth_line, stat_line]) + "\n"


def stat_path(out_json: Path) -> Path:
    """Where yosys writes its statistics for the netlist out_json (out-<synth>.json -> stat-<synth>.json)."""
    return out_json.with_name(out_json.name.replace("out-", "stat-", 1))


def _run_yosys(script: str, out: Path, synth: str, cwd: Path, budget: _Budget) -> tuple[int, str]:
    script_path = out / f"yosys-{synth}.ys"
    safefile.write_file(script_path, script)
    return _run(
        [str(tools.tool("yosys")), "-s", str(script_path)],
        out / f"yosys-{synth}.log", cwd, budget, YOSYS_TIMEOUT,
    )  # fmt: skip


MAX_SV2V = 64 << 20  # bytes of converted Verilog the host accepts from sv2v


def _convert_with_sv2v(
    sv2v: str, fetched: FetchResult, out: Path, cwd: Path, defs: tuple[str, ...], budget: _Budget
) -> str:
    """Convert all sources to plain Verilog in out/sv2v.v. Returns '' or the failure reason. sv2v writes its
    result to a file in scratch (--write=FILE) and what it says goes through the capped log sink; the host
    takes the file only if it is a plain file of at most MAX_SV2V bytes, and copies it to out/sv2v.v."""
    incs = [f"-I{d}" for d in fetched.include_dirs]
    # NOT verified against a real sv2v (it is not in the pinned toolchain): `--write=FILE` is as sv2v's usage text
    # documents it, and the tests use a fake that honours it. If a real sv2v spells it differently the retry
    # fails with a clear synth-fail rather than silently.
    produced = out / SCRATCH / "sv2v.v"
    args = [sv2v, *incs, *(f"-D{d}" for d in defs), f"--write={produced}", *map(str, fetched.synth_sources)]
    rc, text = _run(args, out / "sv2v.log", cwd, budget, SV2V_TIMEOUT)
    if rc != 0:
        return _why(text, rc, "sv2v")
    try:
        fd = safefile.open_dir(out / SCRATCH)
        try:
            safefile.copy_plain(fd, "sv2v.v", out / "sv2v.v", max_size=MAX_SV2V)
        finally:
            os.close(fd)
    except safefile.NotPlainFile as e:
        return f"sv2v's output is not usable: {e}"
    return ""


def _synthesise(
    fetched: FetchResult,
    out: Path,
    wrapper_path: Path,
    synth: str,
    reader: str,
    defs: tuple[str, ...],
    budget: _Budget,
    pdk: tuple[Path, str, bool] | None = None,
    reset_vlog: bool = False,
) -> tuple[bool, str, str, str]:
    """Run yosys for one synthesis variant. The reader is how the sources are read: "" (.v plain,
    .sv with -sv), "sv" (every file with -sv) or "sv2v" (converted). If the first run fails with a syntax
    error or a SystemVerilog-mode error (SV_MODE_ERRORS), retry once with every file read with -sv; then, if
    sv2v is installed, once through sv2v. A reader that worked is kept for later variants. pdk, reset_vlog: see
    _yosys_script. Returns (succeeded, reason, reader, the log line that started the -sv retry or '')."""
    cwd = fetched.include_dirs[0]
    incs = list(fetched.include_dirs)
    out_json = out / SCRATCH / f"out-{synth}.json"
    converted = out / "sv2v.v"

    def attempt(how: str) -> tuple[int, str]:
        if how == "sv2v":
            script = _yosys_script(
                [wrapper_path, converted],
                incs,
                out_json,
                sv=False,
                synth=synth,
                defs=defs,
                pdk=pdk,
                reset_vlog=reset_vlog,
            )
        else:
            script = _yosys_script(
                [wrapper_path, *fetched.synth_sources],
                incs, out_json, sv=True, synth=synth, all_sv=how == "sv", defs=defs, pdk=pdk,
                reset_vlog=reset_vlog,
            )  # fmt: skip
        return _run_yosys(script, out, synth, cwd, budget)

    if reader:
        rc, text = attempt(reader)
        return rc == 0, "" if rc == 0 else _why(text, rc, "yosys"), reader, ""
    rc, text = attempt("")
    if rc == 0:
        return True, "", "", ""
    reason = _why(text, rc, "yosys")
    if rc == LIMIT_RC:  # no second try after a limit: it would hit the same one
        return False, reason, "", ""
    # The log text decides only whether to try once more with -sv: a design that prints "syntax error" or a
    # SystemVerilog-mode error in its log gets one extra attempt, which fails or succeeds on its own merits.
    rule = _sv_rule(text) if any(f.suffix == ".v" for f in fetched.synth_sources) else ""
    if rule:
        rc, text = attempt("sv")
        if rc == 0:
            return True, "", "sv", rule
        reason = f"{reason}; with -sv: {_why(text, rc, 'yosys')}"
        if rc == LIMIT_RC:
            return False, reason, "", rule
    sv2v = _sv2v()
    if not sv2v:
        return False, reason, "", rule
    if err := _convert_with_sv2v(sv2v, fetched, out, cwd, defs, budget):
        return False, f"{reason}; sv2v retry failed: {err}", "", rule
    rc, text = attempt("sv2v")
    if rc == 0:
        return True, "", "sv2v", rule
    return False, f"{reason}; after sv2v: {_why(text, rc, 'yosys')}", "", rule


_LATCH_CHECK = re.compile(r"ERROR: Found (\d+) problems in 'check -assert'")
_LATCH_CELL = re.compile(r"Warning: Cell \S+ is a latch of type \S+")


def _latch_reason(reason: str, log: Path, fetched: FetchResult | None = None) -> str:
    """For synth_ice40's latch check (-latches error: the iCE40 has no latch cell and the design infers latches of
    its own), 'design error under yosys: ...' naming the count and the first latch; else ''. When the design
    defines PDK cells itself (a polyfill such as sky130_polyfill.v), the reason says the latches come from it."""
    m = _LATCH_CHECK.search(reason)
    if not m or reason.startswith(pdkcells.DELAY_AS_FUNCTION):
        return ""
    try:
        first = _LATCH_CELL.search(safefile.read_text(log))
    except (OSError, ValueError, safefile.NotPlainFile):
        first = None
    if not first:
        return ""
    where = ""
    for unit in fetched.synth_sources if fetched is not None else ():
        if any(pdkcells.is_pdk_name(n) for n in fetch.declared_modules([unit])):
            where = f" from the design's own PDK polyfill ({unit.name})"
            break
    return (
        f"design error under yosys: {m.group(1)} latches{where}, which the iCE40 has no cell for "
        f"(first: {strip_control(first.group(0))}): {reason}"
    )


def _toolchain_error(reason: str) -> bool:
    """True if a yosys error in reason is located in a file of the pinned toolchain (`<suite>/...:<line>: ERROR`)."""
    suite = tools.suite_dir()
    for path in _ERROR_AT.findall(reason):
        try:
            if Path(os.path.normpath(path)).resolve().is_relative_to(suite):
                return True
        except (OSError, ValueError):
            continue
    return False


_ERROR_AT = re.compile(r"(/[^\s:]+):\d+: ERROR")


def _sv_rule(text: str) -> str:
    """The first log line with a syntax error or a SystemVerilog-mode error (SV_MODE_ERRORS), or ''."""
    for line in text.splitlines():
        if "syntax error" in line.lower() or any(e in line for e in SV_MODE_ERRORS):
            return strip_control(line.strip())[:300]
    return ""


def _write_result(out: Path, result: BuildResult) -> BuildResult:
    safefile.write_file(out / "build.json", json.dumps(asdict(result), indent=2) + "\n")
    return result


def _cached(out: Path, digest: str, expected=None) -> BuildResult | None:
    """The finished build.json if it was made from these inputs, else None. digest is the digest of a build that
    needed no extra rung; expected(record) gives the one a record that took the PDK models or RESET_VLOG must
    carry (with the models the design gets today). A synth-fail from an older ladder (LADDER) is not reused."""
    result = _matching(out, digest, expected)
    if result is None or result.timed_out or result.transient:
        return None
    if result.status == "synth-fail" and (not isinstance(result.ladder, int) or result.ladder < LADDER):
        return None
    return result


def _matching(out: Path, digest: str, expected=None) -> BuildResult | None:
    """out/build.json if it was made from these inputs (see _cached for digest and expected) and its bitstream, if
    it names one, is intact; also a record that was cut short. None otherwise."""
    path = out / "build.json"
    try:
        result = BuildResult(**json.loads(safefile.read_text(path)))
    except (OSError, ValueError, TypeError, safefile.NotPlainFile):
        return None
    extra = result.pdk_cells or result.reset_vlog
    want = expected(result) if extra and expected is not None else digest
    if result.inputs_sha256 != want:
        return None
    if result.bitstream:
        try:
            data = safefile.read_file(out / result.bitstream, safefile.MAX_BITSTREAM)
        except (OSError, safefile.NotPlainFile):
            return None
        if hashlib.sha256(data).hexdigest() != result.bitstream_sha256:
            return None
    return result


# While a design is rebuilt, the files of its previous build of the same inputs wait here, inside the build
# directory, so that a rebuild that is cut short and ends worse does not destroy a better record (see _settle).
HELD = ".previous"
COMPLETED = ("ok", "timing-fail", "no-fit")  # statuses of a place-and-route attempt that ran to its verdict


def _hold(out: Path, digest: str, expected) -> BuildResult | None:
    """Move the previous build's files into out/HELD if its record is of these inputs (_matching), and return that
    record; else None. A HELD directory that is already there was left by a run that died: it holds the record from
    before that run, and is used (or discarded, if it is not of these inputs) instead."""
    hold = out / HELD
    if os.path.lexists(hold):
        if (prev := _matching(hold, digest, expected)) is None:
            safefile.discard(hold, paths.WORK / "trash")
        return prev
    if (prev := _matching(out, digest, expected)) is None:
        return None
    hold.mkdir()
    names = sorted(e.name for e in os.scandir(out) if e.name != HELD and e.is_file(follow_symlinks=False))
    for name in sorted(
        names, key=lambda n: n != "build.json"
    ):  # the record first: a half-done move is no match
        os.rename(out / name, hold / name)
    return prev


def _put_back(out: Path, trash: Path) -> None:
    """Replace the files of out with those held in out/HELD, and remove HELD. The record goes back last."""
    hold = out / HELD
    for entry in list(os.scandir(out)):
        if entry.name != HELD:
            safefile.discard(Path(entry.path), trash)
    names = sorted(e.name for e in os.scandir(hold))
    for name in sorted(names, key=lambda n: n == "build.json"):
        os.rename(hold / name, out / name)
    hold.rmdir()


def _worse(prev: BuildResult, new: BuildResult) -> str:
    """Why new, a result that was cut short, is worse than prev, the earlier record of the same inputs; "" if it is
    not (then it replaces prev, as any new result does). Worse: prev has a bitstream and new has none, or prev's
    main clock is faster with at least as many completed attempts (and a bitstream if new has one)."""
    if not (new.transient or new.timed_out):
        return ""
    if prev.bitstream and not new.bitstream:
        return "the earlier record has a bitstream and the new result has none"

    def completed(r: BuildResult) -> int:
        return sum(1 for a in r.attempts if isinstance(a, dict) and a.get("status") in COMPLETED)

    def mhz(hz: int | None) -> str:
        return "none" if hz is None else f"{hz / 1e6:.2f} MHz"

    if (
        prev.fmax_hz is not None
        and (new.fmax_hz is None or new.fmax_hz < prev.fmax_hz)
        and completed(prev) >= completed(new)
        and (prev.bitstream or not new.bitstream)
    ):
        return (
            f"the earlier record's main-clock fmax ({mhz(prev.fmax_hz)}, {completed(prev)} completed attempts) is "
            f"higher than the new result's ({mhz(new.fmax_hz)}, {completed(new)} completed attempts)"
        )
    return ""


def _settle(out: Path, trash: Path, result: BuildResult, prev: BuildResult | None) -> BuildResult:
    """After a rebuild: keep the earlier record and its files (held by _hold) if the new result was cut short and is
    worse (_worse), with a rebuild_note saying so; else drop them and keep the new result."""
    hold = out / HELD
    why = _worse(prev, result) if prev is not None else ""
    if not why:
        if os.path.lexists(hold):
            safefile.discard(hold, trash)
        return result
    _put_back(out, trash)
    if result.timed_out:
        cut = "a tool timeout or the design budget"
    else:
        cut = f"the {result.limited} limit" if result.limited else "a transient failure"
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    note = (
        f"a rebuild at {when} was cut short ({cut}) and ended {result.status}"
        f"{': ' + result.reason if result.reason else ''}; the earlier record is kept: {why}"
    )
    return _write_result(out, dataclasses.replace(prev, rebuild_note=note))


def _place_and_route(
    design: Design, out: Path, synth: str, seed: int, budget: _Budget, registers: bool, latches: bool = False
) -> tuple[int, str, reports.NextpnrReport | None, str]:
    """One nextpnr run; leaves the placed design in scratch/attempt.asc. Returns (exit code, log text, report,
    problem). Only a run that exited 0 has a report: it is read from nextpnr's JSON file (never from the log),
    and `problem` says why it could not be used ("" if it could); then report is None. latches: the design
    reads PDK latch cell models (PDK_LATCH_PNR_FLAGS)."""
    asc = out / SCRATCH / "attempt.asc"
    asc.unlink(missing_ok=True)
    report_name = f"report-{synth}-{seed}.json"
    cmd = [
        str(tools.tool("nextpnr-ice40")),
        *_pnr_flags(design.clock_hz, seed),
        *(PDK_LATCH_PNR_FLAGS if latches else ()),
        "--pcf", str(HARNESS / "tt_fpga_fabricfox.pcf"),
        "--json", str(out / SCRATCH / f"out-{synth}.json"),
        "--asc", str(asc),
        "--report", str(out / SCRATCH / report_name),
    ]  # fmt: skip
    rc, text = _run(cmd, out / f"nextpnr-{synth}-{seed}.log", out / SCRATCH, budget, NEXTPNR_TIMEOUT)
    if rc != 0:
        return rc, text, None, ""
    try:
        data = _read_scratch(out / SCRATCH, report_name)
        return rc, text, reports.parse_report(data, clock_hz=design.clock_hz, has_registers=registers), ""
    except (safefile.NotPlainFile, reports.ReportError) as e:
        return rc, text, None, f"nextpnr's timing report is unusable, so the attempt is not trusted: {e}"


def _read_scratch(scratch: Path, name: str) -> bytes:
    fd = safefile.open_dir(scratch)
    try:
        return safefile.read_plain(fd, name, max_size=MAX_REPORT)
    finally:
        os.close(fd)


def _plain(scratch: Path, name: str) -> bool:
    """True if scratch/name is a regular file with one link; a symlink there is not followed."""
    fd = safefile.open_dir(scratch)
    try:
        return safefile.exists_plain(fd, name)
    finally:
        os.close(fd)


def _lut_percent(report: reports.NextpnrReport) -> float | None:
    lc = report.utilisation.get(reports.LC_KEY)
    return round(100 * lc[0] / lc[1], 1) if lc else None


def build(
    design: Design,
    fetched: FetchResult,
    force: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    design_timeout: float = DEFAULT_DESIGN_TIMEOUT,
) -> BuildResult:
    """Synthesise, place, route and pack design into WORK/build/<id>/design.bin; see _build. The tools run
    with only WORK/build/<id>/scratch writable, and it is deleted when the build is over."""
    out = paths.design_dir("build", design.id).resolve()
    trash = paths.WORK / "trash"
    held: dict[str, BuildResult | None] = {}
    try:
        result = _build(design, fetched, force, max_attempts, design_timeout, held)
    except BaseException:
        if os.environ.get("TTHFV_KEEP_SCRATCH") != "1":
            safefile.discard(out / SCRATCH, trash)
        if held.get("previous") is not None:
            try:  # an interrupted rebuild leaves the earlier record as it was; if this fails, the next run uses HELD
                _put_back(out, trash)
            except OSError:
                pass
        raise
    if os.environ.get("TTHFV_KEEP_SCRATCH") != "1" and (note := safefile.discard(out / SCRATCH, trash)):
        # a design that leaves something that cannot be deleted is misbehaving: the stage failed
        (out / BITSTREAM).unlink(missing_ok=True)
        result = dataclasses.replace(
            result, status="pack-fail", reason=note, transient=True, bitstream=None, bitstream_sha256=None
        )
        _write_result(out, result)
    return _settle(out, trash, result, held.get("previous"))


def _build(
    design: Design,
    fetched: FetchResult,
    force: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    design_timeout: float = DEFAULT_DESIGN_TIMEOUT,
    held: dict | None = None,
) -> BuildResult:
    """Synthesise, place, route and pack design into WORK/build/<id>/design.bin.

    Place-and-route is attempted with up to max_attempts recipes, stopping at the first that meets
    the clock. A no-fit ends the seeds of that synthesis variant, but the next variant is tried.
    Without force, a finished build of identical inputs and settings is reused, unless a tool timeout or
    the design budget cut it short (build.json has timed_out true): the timeouts are not part of the
    inputs, so such a result is redone on the next run.

    design_timeout is the wall-clock budget in seconds for the whole build of this design. Once it is
    used up no further attempt is started (a running tool is killed at the deadline); the result is the
    best attempt so far, or pnr-fail with the reason "design timeout" if there is none. Either way
    the result has timed_out set.

    held: filled with {"previous": the earlier record of the same inputs, or None}; its files are then in
    out/HELD until build() settles which record stays (_settle).
    """
    if fetched.status != "ok":
        raise UserError(f"{design.id}: not fetched ({fetched.reason or fetched.status})")
    if max_attempts < 1:
        raise UserError("max attempts must be at least 1")
    if not fetched.top_module_used or not fetched.synth_sources:
        raise UserError(f"{design.id}: the fetch result has no resolved top module or compilation units")
    started = time.monotonic()
    budget = _Budget(design_timeout)
    out = paths.design_dir("build", design.id).resolve()
    out.mkdir(parents=True, exist_ok=True)
    wrapper, n = re.subn(
        rf"\b{PLACEHOLDER}\b", fetched.top_module_used, (HARNESS / "tt_fpga_top.v").read_text()
    )
    if n != 1:
        raise UserError(f"harness tt_fpga_top.v must name {PLACEHOLDER} exactly once, found {n}")
    versions = _tool_versions()
    limits_now = sandbox.limits_record()
    digest = _inputs_digest(design, fetched, wrapper, versions, max_attempts, limits_now)

    def digest_with(plan: pdkcells.Plan | None, reset_vlog: bool) -> str:
        return _inputs_digest(design, fetched, wrapper, versions, max_attempts, limits_now, plan, reset_vlog)

    def expected(record: BuildResult) -> str:
        plan = pdkcells.plan_for(fetched.synth_sources) if record.pdk_cells else None
        return digest_with(plan, record.reset_vlog is True)

    if not force and (hit := _cached(out, digest, expected)):
        return hit
    paths.require_free(paths.WORK, paths.min_free(), " (TTHFV_MIN_FREE)")
    scratch = out / SCRATCH
    # The tools see this directory read-only (their scripts and the wrapper are in it) and write only scratch.
    budget.sandbox = sandbox.for_stage(
        rw=scratch,
        ro=[tools.suite_dir(), fetched.checkout or fetched.src_dir, HARNESS, out],
        path=[tools.suite_dir() / "bin"],
    )
    previous = _hold(out, digest, expected)
    if held is not None:
        held["previous"] = previous
    (out / "build.json").unlink(missing_ok=True)  # a crash from here on must not leave a stale ok
    for stale in out.iterdir():  # files of older layouts or runs: the evidence must be of this run only
        if stale.name == HELD:
            continue
        if (note := safefile.discard(stale, paths.WORK / "trash")) and os.path.lexists(stale):
            raise UserError(f"{stale}: cannot be cleared: {note}")
    scratch.mkdir()
    budget.scratch = scratch
    wrapper_path = out / "tt_fpga_top.v"
    safefile.write_file(wrapper_path, wrapper)

    base = {
        "id": design.id,
        "clock_hz": design.clock_hz,
        "tools": versions,
        "inputs_sha256": digest,
        "top_module_used": fetched.top_module_used,
        "sources_sha256": fetched.sources_sha256,
        "commit": design.commit,
        "repo": design.repo,
        "verilator_flags": _verilator_flags(design),
        "sandbox": sandbox.name(),
        "limits": limits_now,
        "ladder": LADDER,
    }
    defs = tuple(defines(_verilator_flags(design)))
    attempts: list[dict] = []
    pdk: tuple[Path, str, bool] | None = None  # set once yosys has failed for want of PDK cell models
    reset_vlog = False  # set once yosys has failed in a file of the toolchain (RESET_VLOG)

    timed_out = False  # the sweep or the build ended on a tool timeout or the design budget

    def finish(status: Status, reason: str = "", **kw) -> BuildResult:
        seconds = round(time.monotonic() - started, 1)
        result = BuildResult(
            status=status,
            reason=reason,
            seconds=seconds,
            attempts=attempts,
            timed_out=timed_out,
            # "unknown" and "disk-free" say nothing lasting about the design (a killed wrapper; a filesystem that other
            # processes were filling): never reused, the next run does the build again
            transient=timed_out or budget.limited in ("unknown", "disk-free"),
            free_space_rule=budget.free_space_rule,
            free_space_note=budget.free_space_note,
            limited=budget.limited or ("time" if timed_out else None),
            peak=dict(budget.peak) if limits_now else None,
            **base,
            **kw,
        )
        return _write_result(out, result)

    unsafe = _unsafe_paths(
        [wrapper_path, out / "sv2v.v", out / pdkcells.FILE_NAME], fetched.synth_sources, fetched.include_dirs
    ) or _bad_defines(list(defs))
    if unsafe:
        return finish("synth-fail", unsafe)

    cells_by_synth: dict[str, dict[str, int]] = {}
    reader = ""
    skip: set[str] = set()  # synthesis variants that did not fit
    best: dict | None = None  # the kept attempt so far
    passed = False
    stop: tuple[Status, str] | None = None  # why the sweep ended early
    last_no_fit: dict | None = None

    for recipe in RECIPES:
        synth, seed = recipe["synth"], recipe["seed"]
        if len(attempts) >= max_attempts:
            break
        if synth in skip:
            continue
        if budget.expired:
            stop = ("pnr-fail", _DESIGN_TIMEOUT)
            timed_out = True
            break
        if synth not in cells_by_synth:
            ok, reason, reader, rule = _synthesise(
                fetched, out, wrapper_path, synth, reader, defs, budget, pdk, reset_vlog
            )
            retry = not ok and not budget.limited and not budget.expired
            if retry and pdk is None and pdkcells.missing_cell(reason):
                plan = pdkcells.plan_for(fetched.synth_sources)
                if why := pdkcells.retry_reason(reason, plan):
                    reason = f"{why}: {reason}"
                else:  # the rule of tthfv.pdkcells: once more, with the models of every cell the design references
                    safefile.write_file(out / pdkcells.FILE_NAME, plan.text)
                    pdk = (out / pdkcells.FILE_NAME, fetched.top_module_used, plan.latches)
                    base.update(
                        inputs_sha256=digest_with(plan, reset_vlog), pdk_cells=list(plan.cells),
                        pdk_latches=plan.latches, pdk_note=plan.note(),
                    )  # fmt: skip
                    first = reason
                    ok, reason, reader, rule = _synthesise(
                        fetched, out, wrapper_path, synth, "", defs, budget, pdk, reset_vlog
                    )
                    if not ok and f"@{PDK_LOOP_SET}" in reason:
                        reason = f"{pdkcells.loop_reason(plan)}; without them: {first}"
                    elif not ok and (why := pdkcells.retry_reason(reason, plan)).startswith("needs"):
                        reason = f"{why}: {reason}"
            retry = not ok and not budget.limited and not budget.expired
            if (
                retry and not reset_vlog and _toolchain_error(reason)
            ):  # RESET_VLOG: once more, without the globals
                reset_vlog = True
                plan = pdkcells.plan_named(base.get("pdk_cells") or []) if pdk else None
                base.update(inputs_sha256=digest_with(plan, True), reset_vlog=True)
                ok, reason, reader, rule = _synthesise(
                    fetched, out, wrapper_path, synth, "", defs, budget, pdk, reset_vlog
                )
            if rule and not base.get("sv_reader_rule"):
                base["sv_reader_rule"] = rule
            if not ok:
                reason = _latch_reason(reason, out / f"yosys-{synth}.log", fetched) or macros.restate(
                    reason, reason, fetched
                )
                timed_out = budget.timed_out or budget.expired
                stop = (
                    ("pnr-fail", _DESIGN_TIMEOUT)
                    if budget.expired and "timed out" in reason
                    else ("synth-fail", reason)
                )
                break
            try:
                stat = _read_scratch(scratch, stat_path(out / SCRATCH / f"out-{synth}.json").name)
                cells_by_synth[synth] = reports.parse_stat(stat)
            except (safefile.NotPlainFile, reports.ReportError) as e:
                stop = ("synth-fail", f"yosys's statistics are unusable: {e}")
                break
        rc, text, report, problem = _place_and_route(
            design, out, synth, seed, budget, reports.has_registers(cells_by_synth[synth]),
            latches=bool(pdk and pdk[2]),
        )  # fmt: skip
        attempt = {"synth": synth, "seed": seed, "status": "", "fmax_hz": report.fmax_hz if report else None}
        attempts.append(attempt)
        if rc > 0 and reports.capacity_failure(text):  # exit status first; the log only names the failure
            attempt["status"] = "no-fit"
            skip.add(synth)
            last_no_fit = {
                "reason": _why(text, rc, "nextpnr"),
                "cells": cells_by_synth[synth],
                "lut_percent": reports.log_lc_percent(text),
                "recipe": recipe,
            }
            continue
        if rc != 0 or report is None or not _plain(scratch, "attempt.asc"):
            attempt["status"] = "pnr-fail"
            timed_out = rc == -1
            why = problem or ("nextpnr wrote no placed design" if rc == 0 else _why(text, rc, "nextpnr"))
            if rc > 0 and (macro := _hard_macro_reason(text, fetched)):
                why = f"{macro}: {why}"
            stop = ("pnr-fail", _DESIGN_TIMEOUT if rc == -1 and budget.expired else why)
            break
        passed = report.met  # every clock nextpnr reported passes its constraint
        attempt["status"] = "ok" if passed else "timing-fail"
        if passed or best is None or (report.fmax_hz or -1) > (best["fmax_hz"] or -1):
            (scratch / "attempt.asc").replace(scratch / "out.asc")
            best = {
                "fmax_hz": report.fmax_hz,
                "clocks": {
                    n: {"fmax_hz": c.fmax_hz, "constraint_hz": c.constraint_hz, "pass": c.passed}
                    for n, c in report.clocks.items()
                },
                "failing": [(n, report.clocks[n].fmax_hz) for n in report.failing],
                "failing_cross": [c.explain() for c in report.failing_cross],
                "cross_paths": [c.record() for c in report.cross_paths],
                "note": report.note,
                "recipe": recipe,
                "cells": cells_by_synth[synth],
                "lut_percent": _lut_percent(report),
            }
        else:
            (scratch / "attempt.asc").unlink()
        if passed:
            break

    if best is None:
        if stop:
            return finish(stop[0], stop[1], sv2v=reader == "sv2v", sv_reader=reader == "sv")
        if last_no_fit:
            return finish(
                "no-fit",
                last_no_fit["reason"],
                cells=last_no_fit["cells"],
                lut_percent=last_no_fit["lut_percent"],
                recipe=last_no_fit["recipe"],
                sv2v=reader == "sv2v",
                sv_reader=reader == "sv",
            )
        return finish(
            "pnr-fail", "no place-and-route attempt ran", sv2v=reader == "sv2v", sv_reader=reader == "sv"
        )

    known = {
        "cells": best["cells"],
        "lut_percent": best["lut_percent"],
        "fmax_hz": best["fmax_hz"],
        "clocks": best["clocks"],
        "cross_paths": best["cross_paths"],
        "timing_note": best["note"],
        "recipe": best["recipe"],
        "sv2v": reader == "sv2v",
        "sv_reader": reader == "sv",
    }
    rc, text = _run(
        [str(tools.tool("icepack")), str(scratch / "out.asc"), str(scratch / BITSTREAM)],
        out / "icepack.log", scratch, budget, ICEPACK_TIMEOUT,
    )  # fmt: skip
    if rc != 0:
        timed_out = rc == -1
        return finish("pack-fail", _why(text, rc, "icepack"), **known)
    try:  # what the host takes from the tools' directory: only plain files, within bounds
        fd = safefile.open_dir(scratch)
        try:
            data = safefile.read_plain(fd, BITSTREAM, max_size=safefile.MAX_BITSTREAM)
            safefile.copy_plain(fd, "out.asc", out / "out.asc", max_size=safefile.MAX_ASC)
            for synth in cells_by_synth:
                name = f"out-{synth}.json"
                safefile.copy_plain(fd, name, out / name, max_size=safefile.MAX_JSON)
        finally:
            os.close(fd)
    except safefile.NotPlainFile as e:
        return finish(
            "pack-fail", f"the tools' output was not a plain file of an expected size: {e}", **known
        )
    safefile.write_file(out / BITSTREAM, data)
    if PREAMBLE not in data[:64]:
        (out / BITSTREAM).unlink()
        return finish(
            "pack-fail", "bitstream has no iCE40 preamble 7E AA 99 7E in its first 64 bytes", **known
        )
    known |= {"bitstream": BITSTREAM, "bitstream_sha256": hashlib.sha256(data).hexdigest()}
    if passed:
        return finish("ok", **known)
    parts = []
    if best["failing"]:
        shown = ", ".join(f"{n} {hz / 1e6:.2f} MHz" for n, hz in best["failing"][:5])
        if len(best["failing"]) > 5:
            shown += f" and {len(best['failing']) - 5} more"
        parts.append(f"clock(s) below the {design.clock_hz / 1e6:.3f} MHz constraint: {shown}")
    if best["failing_cross"]:
        shown = "; ".join(best["failing_cross"][:3])
        if len(best["failing_cross"]) > 3:
            shown += f" and {len(best['failing_cross']) - 3} more"
        parts.append(f"path(s) between clock domains too slow: {shown}")
    reason = "; ".join(parts) + f" (best of {len(attempts)} attempts, by main-clock fmax)"
    if stop:
        reason += f"; sweep stopped: {stop[1]}"
    return finish("timing-fail", reason, **known)


def _line(design: Design, r: BuildResult) -> str:
    lut = "-" if r.lut_percent is None else f"{r.lut_percent:.1f}"
    fmax = "-" if r.fmax_hz is None else f"{r.fmax_hz / 1e6:.2f}"
    line = f"{design.id} {r.status} {lut}% fmax {fmax}/{design.clock_hz / 1e6:.3f} {r.reason}"
    return strip_control(line).rstrip()


def _cmd_build(args: argparse.Namespace) -> int:
    return sandbox.command(lambda: _build_command(args))


def _build_command(args: argparse.Namespace) -> int:
    sandbox.announce()
    safefile.empty_trash_and_say(paths.WORK / "trash")
    designs = manifest.load(extras=manifest.EXTRAS_DEFAULT)
    chosen = [manifest.by_id(designs, i) for i in args.ids]
    failed = 0
    for d in chosen:
        fetched = fetch.fetch(d)
        if fetched.status != "ok":
            print(strip_control(f"{d.id} {fetched.status} -% fmax -/{d.clock_hz / 1e6:.3f} {fetched.reason}"))
            failed += 1
            continue
        r = build(
            d, fetched, force=args.force, max_attempts=args.max_attempts, design_timeout=args.design_timeout
        )
        print(_line(d, r), flush=True)
        failed += r.status != "ok"
    return 1 if failed else 0


def add_parser(sub) -> None:
    p = sub.add_parser("build", help="build bitstreams with yosys, nextpnr-ice40 and icepack")
    p.add_argument("ids", nargs="+", metavar="id", help="<shuttle>/<macro>; fetched first if needed")
    p.add_argument("--force", action="store_true", help="build again even if inputs are unchanged")
    p.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        metavar="N",
        help=f"most place-and-route attempts per design (default {DEFAULT_MAX_ATTEMPTS}; 1 tries one seed)",
    )
    p.add_argument(
        "--design-timeout",
        type=float,
        default=DEFAULT_DESIGN_TIMEOUT,
        metavar="SECONDS",
        help=f"wall-clock budget for the whole build of one design (default {DEFAULT_DESIGN_TIMEOUT})",
    )
    p.set_defaults(func=_cmd_build)

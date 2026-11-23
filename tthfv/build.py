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

from tthfv import fetch, manifest, paths, proc, reports, safefile, sandbox, tools
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
LC_TOTAL_KEY = "ICESTORM_LC"
_STAMP_VERSION = 4

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


class _Budget:
    """The wall-clock budget of one design's build. Tool timeouts are cut down to what is left of it."""

    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds
        self.timed_out = False  # a tool run through this budget was killed on its timeout
        self.sandbox: sandbox.Sandbox | None = None  # what every tool of this build runs in; None: none
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


def _sv2v() -> str | None:
    found = tools.sv2v()
    return str(found) if found else None


def _tool_versions() -> dict[str, str]:
    out = {name: tools.version_line(tools.tool(name)) for name in ("yosys", "nextpnr-ice40")}
    out["icepack"] = tools.icepack_version()
    if sv2v := tools.sv2v():
        out["sv2v"] = tools.version_line(sv2v)
    return out


def _synth_command(synth: str) -> list[str]:
    return ["synth_ice40", "-top", "tt_fpga_top", *SYNTH_FLAGS[synth]]


def _pnr_flags(clock_hz: int, seed: int) -> list[str]:
    """nextpnr arguments that do not name files."""
    return [
        "--up5k", "--package", "sg48", "--pcf-allow-unconstrained",
        "--freq", f"{clock_hz / 1e6:g}", "--timing-allow-fail", "--seed", str(seed),
    ]  # fmt: skip


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
    design: Design, fetched: FetchResult, wrapper: str, versions: dict[str, str], max_attempts: int
) -> str:
    """Everything that decides the result: the design's sources (one digest of the whole src tree,
    shared with the reference stage), the wrapper, the pcf, tool versions, and every command line
    (yosys synthesis commands, nextpnr flags, recipes)."""
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
        "recipes": list(RECIPES),
    }
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
    done = proc.run_logged(
        args, budget.limit(timeout), log, cwd=cwd, env=tools.env(), append=False, header=False,
        watch=("syntax error", "error"), sandbox=budget.sandbox,
    )  # fmt: skip
    if done.rc == proc.TIMEOUT_RC:
        budget.timed_out = True
        return -1, done.text
    if budget.scratch is not None and (used := safefile.tree_size(budget.scratch)) > safefile.MAX_SCRATCH:
        why = f"ERROR: the tool filled its scratch directory: {used} bytes, at most {safefile.MAX_SCRATCH} allowed"
        return 1, f"{done.text}\n{why}"
    return done.rc, done.text


def _why(output: str, rc: int, tool: str) -> str:
    """The most telling line of a failed tool's output: the last ERROR line, else the last line."""
    if rc == -1:
        return f"{tool} timed out"
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
) -> str:
    """sv: read .sv files with -sv. all_sv: read every file with -sv, as the Tiny Tapeout hardening
    flow does (some designs use SystemVerilog constructs such as `>>=` in plain .v files). defs are
    the design's macro definitions (-D)."""
    inc = " ".join([*(f"-D {d}" for d in defs), *(f'-I "{d}"' for d in include_dirs)])
    reads = [
        f'read_verilog {"-sv " if all_sv or (sv and f.suffix == ".sv") else ""}{inc} "{f}"'.replace("  ", " ")
        for f in files
    ]
    synth_line = " ".join(_synth_command(synth)) + f' -json "{out_json}"'
    return "\n".join([*reads, synth_line]) + "\n"


def _run_yosys(script: str, out: Path, synth: str, cwd: Path, budget: _Budget) -> tuple[int, str]:
    script_path = out / f"yosys-{synth}.ys"
    safefile.write_file(script_path, script)
    return _run(
        [str(tools.tool("yosys")), "-s", str(script_path)],
        out / f"yosys-{synth}.log", cwd, budget, YOSYS_TIMEOUT,
    )  # fmt: skip


def _convert_with_sv2v(
    sv2v: str, fetched: FetchResult, out: Path, cwd: Path, defs: tuple[str, ...], budget: _Budget
) -> str:
    """Convert all sources to plain Verilog in out/sv2v.v. Returns '' or the failure reason."""
    incs = [f"-I{d}" for d in fetched.include_dirs]
    args = [sv2v, *incs, *(f"-D{d}" for d in defs), *map(str, fetched.synth_sources)]
    with (
        safefile.open_log(out / "sv2v.v", append=False) as f,
        safefile.open_log(out / "sv2v.log", append=False) as errf,
    ):
        rc = proc.run(
            args, budget.limit(SV2V_TIMEOUT), cwd=cwd, env=tools.env(), stdout=f, stderr=errf,
            sandbox=budget.sandbox,
        )  # fmt: skip
    budget.timed_out |= rc == proc.TIMEOUT_RC
    if rc != 0:
        return _why(
            safefile.read_text(out / "sv2v.log", errors="replace"),
            -1 if rc == proc.TIMEOUT_RC else rc,
            "sv2v",
        )
    return ""


def _synthesise(
    fetched: FetchResult,
    out: Path,
    wrapper_path: Path,
    synth: str,
    reader: str,
    defs: tuple[str, ...],
    budget: _Budget,
) -> tuple[bool, str, str]:
    """Run yosys for one synthesis variant. The reader is how the sources are read: "" (.v plain,
    .sv with -sv), "sv" (every file with -sv) or "sv2v" (converted). If the first run fails, retry
    once with every file read with -sv, then, if sv2v is installed, once through sv2v. A reader that
    worked is kept for later variants. Returns (succeeded, reason, reader)."""
    cwd = fetched.include_dirs[0]
    incs = list(fetched.include_dirs)
    out_json = out / SCRATCH / f"out-{synth}.json"
    converted = out / "sv2v.v"

    def attempt(how: str) -> tuple[int, str]:
        if how == "sv2v":
            script = _yosys_script(
                [wrapper_path, converted], incs, out_json, sv=False, synth=synth, defs=defs
            )
        else:
            script = _yosys_script(
                [wrapper_path, *fetched.synth_sources],
                incs, out_json, sv=True, synth=synth, all_sv=how == "sv", defs=defs,
            )  # fmt: skip
        return _run_yosys(script, out, synth, cwd, budget)

    if reader:
        rc, text = attempt(reader)
        return rc == 0, "" if rc == 0 else _why(text, rc, "yosys"), reader
    rc, text = attempt("")
    if rc == 0:
        return True, "", ""
    reason = _why(text, rc, "yosys")
    if any(f.suffix == ".v" for f in fetched.synth_sources) and "syntax error" in text.lower():
        rc, text = attempt("sv")
        if rc == 0:
            return True, "", "sv"
        reason = f"{reason}; with -sv: {_why(text, rc, 'yosys')}"
    sv2v = _sv2v()
    if not sv2v:
        return False, reason, ""
    if err := _convert_with_sv2v(sv2v, fetched, out, cwd, defs, budget):
        return False, f"{reason}; sv2v retry failed: {err}", ""
    rc, text = attempt("sv2v")
    if rc == 0:
        return True, "", "sv2v"
    return False, f"{reason}; after sv2v: {_why(text, rc, 'yosys')}", ""


def _write_result(out: Path, result: BuildResult) -> BuildResult:
    safefile.write_file(out / "build.json", json.dumps(asdict(result), indent=2) + "\n")
    return result


def _cached(out: Path, digest: str) -> BuildResult | None:
    path = out / "build.json"
    try:
        result = BuildResult(**json.loads(safefile.read_text(path)))
    except (OSError, ValueError, TypeError, safefile.NotPlainFile):
        return None
    if result.inputs_sha256 != digest or result.timed_out:
        return None
    if result.bitstream:
        try:
            data = safefile.read_file(out / result.bitstream, safefile.MAX_BITSTREAM)
        except (OSError, safefile.NotPlainFile):
            return None
        if hashlib.sha256(data).hexdigest() != result.bitstream_sha256:
            return None
    return result


def _place_and_route(
    design: Design, out: Path, synth: str, seed: int, budget: _Budget
) -> tuple[int, str, reports.NextpnrReport]:
    """One nextpnr run; leaves the placed design in out/attempt.asc."""
    asc = out / SCRATCH / "attempt.asc"
    asc.unlink(missing_ok=True)
    cmd = [
        str(tools.tool("nextpnr-ice40")),
        *_pnr_flags(design.clock_hz, seed),
        "--pcf", str(HARNESS / "tt_fpga_fabricfox.pcf"),
        "--json", str(out / SCRATCH / f"out-{synth}.json"),
        "--asc", str(asc),
    ]  # fmt: skip
    rc, text = _run(cmd, out / f"nextpnr-{synth}-{seed}.log", out / SCRATCH, budget, NEXTPNR_TIMEOUT)
    return rc, text, reports.parse_nextpnr(text)


def _plain(scratch: Path, name: str) -> bool:
    """True if scratch/name is a regular file with one link; a symlink there is not followed."""
    fd = safefile.open_dir(scratch)
    try:
        return safefile.exists_plain(fd, name)
    finally:
        os.close(fd)


def _lut_percent(report: reports.NextpnrReport) -> float | None:
    lc = report.utilisation.get(LC_TOTAL_KEY)
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
    try:
        result = _build(design, fetched, force, max_attempts, design_timeout)
    except BaseException:
        if os.environ.get("TTHFV_KEEP_SCRATCH") != "1":
            safefile.discard(out / SCRATCH, trash)
        raise
    if os.environ.get("TTHFV_KEEP_SCRATCH") != "1" and (note := safefile.discard(out / SCRATCH, trash)):
        # a design that leaves something that cannot be deleted is misbehaving: the stage failed
        result = dataclasses.replace(
            result, status="pack-fail", reason=f"scratch could not be removed: {note}"
        )
        _write_result(out, result)
    return result


def _build(
    design: Design,
    fetched: FetchResult,
    force: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    design_timeout: float = DEFAULT_DESIGN_TIMEOUT,
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
    digest = _inputs_digest(design, fetched, wrapper, versions, max_attempts)
    if not force and (hit := _cached(out, digest)):
        return hit
    scratch = out / SCRATCH
    # The tools see this directory read-only (their scripts and the wrapper are in it) and write only scratch.
    budget.sandbox = sandbox.for_stage(
        rw=scratch,
        ro=[tools.suite_dir(), fetched.checkout or fetched.src_dir, HARNESS, out],
        path=[tools.suite_dir() / "bin"],
    )
    (out / "build.json").unlink(missing_ok=True)  # a crash from here on must not leave a stale ok
    for stale in out.iterdir():  # files of older layouts or runs: the evidence must be of this run only
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
    }
    defs = tuple(defines(_verilator_flags(design)))
    attempts: list[dict] = []

    timed_out = False  # the sweep or the build ended on a tool timeout or the design budget

    def finish(status: Status, reason: str = "", **kw) -> BuildResult:
        seconds = round(time.monotonic() - started, 1)
        result = BuildResult(
            status=status,
            reason=reason,
            seconds=seconds,
            attempts=attempts,
            timed_out=timed_out,
            **base,
            **kw,
        )
        return _write_result(out, result)

    unsafe = _unsafe_paths(
        [wrapper_path, out / "sv2v.v"], fetched.synth_sources, fetched.include_dirs
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
            ok, reason, reader = _synthesise(fetched, out, wrapper_path, synth, reader, defs, budget)
            if not ok:
                timed_out = budget.timed_out or budget.expired
                stop = (
                    ("pnr-fail", _DESIGN_TIMEOUT)
                    if budget.expired and "timed out" in reason
                    else ("synth-fail", reason)
                )
                break
            log = safefile.read_text(out / f"yosys-{synth}.log", errors="replace")
            cells_by_synth[synth] = reports.parse_yosys(log)
        rc, text, report = _place_and_route(design, out, synth, seed, budget)
        attempt = {"synth": synth, "seed": seed, "status": "", "fmax_hz": report.fmax_hz}
        attempts.append(attempt)
        if not report.fit:
            attempt["status"] = "no-fit"
            skip.add(synth)
            last_no_fit = {
                "reason": _why(text, rc, "nextpnr"),
                "cells": cells_by_synth[synth],
                "lut_percent": _lut_percent(report),
                "recipe": recipe,
            }
            continue
        if rc != 0 or not _plain(scratch, "attempt.asc"):
            attempt["status"] = "pnr-fail"
            timed_out = rc == -1
            stop = ("pnr-fail", _DESIGN_TIMEOUT if rc == -1 and budget.expired else _why(text, rc, "nextpnr"))
            break
        passed = report.met  # every clock nextpnr reported passes its constraint
        attempt["status"] = "ok" if passed else "timing-fail"
        if passed or best is None or (report.fmax_hz or -1) > (best["fmax_hz"] or -1):
            (scratch / "attempt.asc").replace(scratch / "out.asc")
            best = {
                "fmax_hz": report.fmax_hz,
                "clocks": {n: {"fmax_hz": c.fmax_hz, "pass": c.passed} for n, c in report.clocks.items()},
                "failing": [(n, report.clocks[n].fmax_hz) for n in report.failing],
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
    if best["fmax_hz"] is None:
        reason = "nextpnr reported no timing for clk"
    else:
        failing = ", ".join(f"{n} {hz / 1e6:.2f} MHz" for n, hz in best["failing"])
        reason = (
            f"clock(s) below the {design.clock_hz / 1e6:.3f} MHz constraint: {failing}"
            f" (best of {len(attempts)} attempts, by main-clock fmax)"
        )
    if stop:
        reason += f"; sweep stopped: {stop[1]}"
    return finish("timing-fail", reason, **known)


def _line(design: Design, r: BuildResult) -> str:
    lut = "-" if r.lut_percent is None else f"{r.lut_percent:.1f}"
    fmax = "-" if r.fmax_hz is None else f"{r.fmax_hz / 1e6:.2f}"
    line = f"{design.id} {r.status} {lut}% fmax {fmax}/{design.clock_hz / 1e6:.3f} {r.reason}"
    return strip_control(line).rstrip()


def _cmd_build(args: argparse.Namespace) -> int:
    sandbox.announce()
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

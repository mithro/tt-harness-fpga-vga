# SPDX-License-Identifier: Apache-2.0
"""Reference simulation: run a design under Verilator and dump its output pins on every clock.

Layout under WORK/reference/<shuttle>/<macro>/<profile>/, where <profile> is `ideal` or the stem of a board
profile file (boards/<name>.json):

    scratch/      the only directory the sandbox can write: the Verilated model and the compiled testbench
                  (scratch/obj/Vtop) and the testbench's raw dumps. The host reads back only the dumps,
                  through tthfv.safefile, and deletes the directory when the simulation is over (unless
                  TTHFV_KEEP_SCRATCH=1). Nothing else here is ever bound into the sandbox.
    uo_out.bin    one byte per clock: uo_out after that clock's rising edge (clock 0 = first reset clock)
    uo_out_low.bin  one byte per clock: uo_out in the low phase of that clock (after the falling edge)
    uio.bin       two bytes per clock: uio_out, uio_oe
    sim.log       verilator, make and simulation output
    sim.json      written last, by rename; its presence marks a finished, successful simulation

Plan 1 kept these directly under WORK/reference/<shuttle>/<macro>/ (the `ideal` profile, schema 2); see
_adopt_legacy for how such a reference is carried over, and when it is not.

Reset stimulus: ena=1, ui_in and uio_in fixed (the manifest's under `ideal`, the board profile's under a
board), rst_n low for clocks 0-9 and high from clock 10. A failed verilation or simulation is a result
(SimResult.status), not an exception.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from tthfv import fetch, frames, macros, manifest, paths, pdkcells, proc, safefile, sandbox, tools
from tthfv.board import census
from tthfv.errors import UserError
from tthfv.fetch import FetchResult
from tthfv.manifest import Design
from tthfv.text import strip_control

Status = Literal["ok", "verilate-fail", "sim-fail", "sim-unsettled"]

DEFAULT_CLOCKS = 6_000_000
# Bump when the testbench's stimulus changes in a way the key cannot see. 2: the pad readback model. (The
# board profile, with its ties, is in the key; the testbench hash covers the rest.)
STIMULUS_VERSION = 3
SCHEMA = 3  # reference.json: 3 adds the profile, the low-phase dump and the flags that come with it
IDEAL_NAME = "ideal"
SENS_SEEDS = (1, 2)  # the two random initial states of --sensitivity
TB = Path(__file__).resolve().parent / "harness" / "ref_tb.cpp"
SCRATCH = "scratch"  # under the reference directory: the one directory the sandbox may write
VERILATE_TIMEOUT = 900  # seconds, for the Verilator run including the C++ build
SIM_TIMEOUT = 600  # seconds, for the simulation itself

# Same flags as the VGA videos harness, so the same designs verilate.
_BASE_FLAGS = [
    "--cc", "--exe", "--build", "-j", "4", "--prefix", "Vtop",
    "-O3", "--x-assign", "fast", "--x-initial", "fast",
    "-Wno-fatal", "-Wno-lint", "-Wno-style", "-Wno-MULTIDRIVEN", "-Wno-UNOPTFLAT",
    "-CFLAGS", "-std=c++17", "-MAKEFLAGS", "OPT_FAST=-O2",
    "--relative-includes",
]  # fmt: skip


@dataclass(frozen=True)
class SimResult:
    status: Status
    dump: Path  # uo_out.bin
    uio: Path  # uio.bin
    clocks: int
    log: Path
    reason: str = ""  # why a failed result failed; empty when ok
    timed_out: bool = False  # the failure is a Verilator or simulation timeout, which a rerun may not hit
    sandbox: str = ""  # "bwrap" or "none": whether Verilator and the simulation ran in the sandbox
    limited: str | None = None  # "memory", "tasks" or "time": a resource limit ended the stage
    limits: dict | None = None  # the cgroup limits it ran under, or None
    peak: dict | None = None  # largest {"memory" bytes, "tasks"} of any program, from its cgroup
    low: Path | None = None  # uo_out_low.bin
    uio_oe_ever: int | None = None  # OR of uio_oe over every sample from clock 10 on
    uio_oe_any: int | None = None  # OR of uio_oe over every sample at all: before clock 0, reset, both phases
    two_driver_clocks: int | None = (
        None  # clocks at which the MCU drove a tied ui_in bit the design enabled on uio
    )


LIMIT_RC = -2  # what _run returns for a program that hit a cgroup or disk limit; its text is the reason
TIMED_OUT = -1  # what _run returns for a program killed on its timeout (124 is then just an exit status)


class _Track:
    """What the programs of one simulation used and hit, for the record."""

    def __init__(self):
        self.limited: str | None = None
        self.free_space_rule = ""  # "off: <why>" if the disk watchdog's free-space check could not run
        self.scratch: Path | None = None  # the directory the program writes, watched while it runs
        self.peak = {"memory": 0, "tasks": 0}
        self.pdk_loop_check_note = ""  # why the PDK loop check could not run, if it could not


def _run(
    args: list[str], log: Path, cwd: Path, timeout: float, box: sandbox.Sandbox | None, track: _Track
) -> tuple[int, str]:
    """Run a command, in the sandbox if there is one, with its output appended to sim.log, capped at
    proc.LOG_CAP bytes (the first bytes, a marker and the last bytes, so a design that prints a lot cannot
    fill the disk). Its process group is killed on a timeout (verilator spawns make and a compiler).
    Returns the exit code (TIMED_OUT on a timeout, LIMIT_RC if a memory, task or disk limit was hit) and the
    text written, so nobody has to read the log back."""
    scratch = track.scratch
    done = proc.run_logged(
        args, timeout, log, cwd=cwd, env=tools.env(), cap=proc.LOG_CAP, sandbox=box,
        scratch_check=(lambda: safefile.scratch_exceeded(scratch)) if scratch is not None else None,
        scratch_dir=scratch,
    )  # fmt: skip
    track.peak["memory"] = max(track.peak["memory"], done.peak_memory or 0)
    track.peak["tasks"] = max(track.peak["tasks"], done.peak_tasks or 0)
    if done.timed_out and done.limited in (None, "time"):
        return TIMED_OUT, done.text
    if done.free_space_rule.startswith("off"):
        track.free_space_rule = track.free_space_rule or done.free_space_rule
    if done.limited in ("disk", "disk-free"):
        track.limited = track.limited or done.limited
        return LIMIT_RC, done.detail
    if done.limited in ("memory", "tasks", "unknown") and box is not None and box.limits is not None:
        track.limited = track.limited or done.limited
        return LIMIT_RC, box.limits.reason(done.limited)
    return done.rc, done.text


def _verilator_version() -> str:
    try:
        return tools.version_line(tools.tool("verilator"))
    except tools.ToolMissing:
        raise
    except UserError:
        return "unknown"


def key_sha256(key: dict) -> str:
    return hashlib.sha256(json.dumps(key, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Profile:
    """The inputs the reference is simulated under: `ideal` (the manifest's fixed inputs, nothing attached) or a
    board's census profile (boards/<name>.json, validated by census.validate_profile)."""

    name: str  # "ideal" or the file's stem
    data: dict | None  # the whole profile as read; None for ideal

    @property
    def ideal(self) -> bool:
        return self.data is None


IDEAL = Profile(IDEAL_NAME, None)
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def load_profile(spec: str) -> Profile:
    """`ideal`, or the path of a board profile file. A profile that does not validate is refused: the
    reference would otherwise be simulated under inputs nobody checked."""
    if spec == IDEAL_NAME:
        return IDEAL
    path = Path(spec)
    name = path.stem
    if name == IDEAL_NAME or not _PROFILE_NAME.match(name) or name in (SCRATCH, "trash"):
        raise UserError(f"profile {spec}: the file name's stem {name!r} cannot name a profile")
    try:
        data = json.loads(safefile.read_text(path))
    except (OSError, ValueError, safefile.NotPlainFile) as e:
        raise UserError(f"profile {spec}: cannot read it: {e}") from e
    try:
        census.validate_profile(data)
    except census.CensusError as e:
        raise UserError(f"profile {spec} is not a valid board profile: {e}") from e
    return Profile(name, data)


@dataclass(frozen=True)
class Stimulus:
    """What the design is asked to see, and what the board lets it see.

    ui_req: the manifest's fixed ui_in (the request). ui_undriven: the level an undriven ui_in net sits at.
    uio_undriven: the level an undriven uio net sits at. ties: bit k set means ui_in[k] and uio[k] are one net.
    mcu_mask: the ui_in bits the board allows the MCU to drive (before the tie rule). uncertain: uio_in bits
    whose level is not known (--sensitivity flips them)."""

    ui_req: int
    ui_undriven: int
    uio_undriven: int
    ties: int
    mcu_mask: int
    uncertain: int
    uio_requested: int


def stimulus_of(design: Design, profile: Profile) -> Stimulus:
    if profile.data is None:  # nothing attached: the manifest's inputs, all honoured
        return Stimulus(design.ui_in, design.ui_in, design.uio_in, 0, 0xFF, 0, design.uio_in)
    st = profile.data["stimulus"]
    ties = 0
    for c in profile.data["drive_conflicts"]:
        if c["ui_in"] != c["uio"]:  # the census only ties ui_in[k] to uio[k]
            raise UserError(f"profile {profile.name}: a tie of ui_in[{c['ui_in']}] to uio[{c['uio']}]")
        ties |= 1 << c["ui_in"]
    return Stimulus(
        design.ui_in, st["ui_in"], st["uio_in"], ties, st["ui_in_mask"], st["uio_in_uncertain"],
        design.uio_in,
    )  # fmt: skip


def reference_dir(design_id: str, profile_name: str) -> Path:
    """WORK/reference/<shuttle>/<macro>/<profile>"""
    base = paths.design_dir("reference", design_id)
    out = base / profile_name
    paths.no_symlinks(out, paths.WORK)
    return out


def conservative_mask(profile: Profile) -> int:
    """The ui_in bits the MCU drives in pass 1: the profile's mask without every tied bit."""
    return 0xFF if profile.data is None else census.effective_ui_in_mask(profile.data, 0xFF)


def final_mask(profile: Profile, uio_oe_any: int) -> int:
    """The ui_in bits the MCU may drive for a design that enables the uio bits in uio_oe_any (every sample,
    reset included). Per bit: only the ui_in bit whose own tied uio bit is enabled is dropped."""
    return 0xFF if profile.data is None else census.effective_ui_in_mask(profile.data, uio_oe_any)


def applied_ui_in(stim: Stimulus, mask: int) -> int:
    """What the design's ui_in pins read at clock 10 with no uio output enabled: the requested level on the
    bits the MCU drives, the undriven level elsewhere (a tied net undriven sits at the uio level)."""
    untied = (stim.ui_req & mask) | (stim.ui_undriven & ~mask)
    tied = (stim.ui_req & mask) | (stim.uio_undriven & ~mask)
    return ((untied & ~stim.ties) | (tied & stim.ties)) & 0xFF


def applied_uio_in(stim: Stimulus, mask: int) -> int:
    """What the design's uio pins read with no uio output enabled: the undriven level, except on a tied net whose
    ui_in bit the MCU drives (it reads the requested level)."""
    tied = (stim.ui_req & mask) | (stim.uio_undriven & ~mask)
    return ((stim.uio_undriven & ~stim.ties) | (tied & stim.ties)) & 0xFF


def _bits(mask: int, name: str) -> str:
    return ", ".join(f"{name}[{k}]" for k in range(8) if mask >> k & 1) or "none"


def sim_key(
    design: Design,
    fetched: FetchResult,
    clocks: int,
    profile: Profile = IDEAL,
    ui_drive: int | None = None,
    pdk: pdkcells.Plan | None = None,
) -> dict:
    """Everything that decides the dump: the design's identity and sources, the stimulus (the whole profile),
    the simulator and its flags, the testbench and the frame cutter version. Written to sim.json and
    reference.json; a reference is reused only if the key it carries equals the current one. The key of
    reference.json has no `ui_drive`; the key of a simulation (sim.json) has the ui_in bits the MCU drove."""
    stim = stimulus_of(design, profile)
    key = {
        "repo": design.repo,
        "commit": design.commit,
        "sources_sha256": fetched.sources_sha256,
        "compilation_units": [
            str(p.relative_to(fetched.src_dir) if p.is_relative_to(fetched.src_dir) else p)
            for p in fetched.synth_sources
        ],
        "top_module_used": fetched.top_module_used,
        "ui_in": stim.ui_req,
        "uio_in": stim.uio_requested,
        "ties": stim.ties,
        "profile": {"name": profile.name, "data": profile.data},
        "clocks": clocks,
        "verilator_flags": list(design.override.get("verilator_flags") or []),
        "limits": sandbox.limits_record(),  # raising a limit redoes a simulation that hit it
        "base_flags": list(_BASE_FLAGS),
        "verilator": _verilator_version(),
        "testbench_sha256": hashlib.sha256(TB.read_bytes()).hexdigest(),
        "stimulus_version": STIMULUS_VERSION,
        "cutter_version": frames.CUTTER_VERSION,
    }
    if ui_drive is not None:
        key["ui_drive"] = ui_drive
    if pdk is not None:  # only for a design that does not verilate without them (tthfv.pdkcells)
        key.update(_pdk_key(pdk, fetched))
    return key


def _pdk_key(plan: pdkcells.Plan, fetched: FetchResult) -> dict:
    """What a reference made with PDK cell models adds to its key: the models and the loop check."""
    return {
        "pdk_cells": plan.key_fields(),
        "pdk_loop_check": pdkcells.loop_check(fetched.top_module_used, Path(pdkcells.FILE_NAME)),
    }


def _expected_key(key: dict, found: dict, design: Design, fetched: FetchResult) -> dict:
    """The key a finished record must carry: key, or, if the record was made with PDK cell models, key with the
    models the design gets today (tthfv.pdkcells.plan_for)."""
    if not isinstance(found, dict) or "pdk_cells" not in found:
        return key
    return {**key, **_pdk_key(pdkcells.plan_for(fetched.synth_sources), fetched)}


def _existing(out: Path, stem: str) -> Path | None:
    for name in (f"{stem}.bin", f"{stem}.bin.gz"):
        if safefile.is_plain(out / name):
            return out / name
    return None


def simulate(
    design: Design,
    fetched: FetchResult,
    clocks: int = DEFAULT_CLOCKS,
    force: bool = False,
    profile: Profile = IDEAL,
    ui_drive: int | None = None,
    out_sub: str = "",
) -> SimResult:
    """Verilate the design's own top module and dump uo_out (and uio_out/uio_oe) for `clocks` clocks, under
    the profile's inputs. ui_drive is the set of ui_in bits the MCU drives; the default is the conservative
    mask (every tied bit undriven), which is pass 1 of make_reference. out_sub: a subdirectory of the
    profile's directory to write to (pass 2), so that pass 1's files survive.

    Without force, a finished simulation with the same clocks, inputs, top module, flags and
    source contents is reused.
    Verilator and the simulation run with the project's src directory as working directory, so
    relative .hex/.mem paths in the design resolve.
    """
    out = (reference_dir(design.id, profile.name) / out_sub).resolve()
    drive = conservative_mask(profile) if ui_drive is None else ui_drive
    dump, uio, log, stamp_path = out / "uo_out.bin", out / "uio.bin", out / "sim.log", out / "sim.json"
    low = out / "uo_out_low.bin"
    scratch = out / SCRATCH
    key = sim_key(design, fetched, clocks, profile, drive)
    digest = key_sha256(key)

    if (
        not force
        and (found := _existing(out, "uo_out")) is not None
        and (found_low := _existing(out, "uo_out_low")) is not None
    ):
        try:
            stamp = json.loads(safefile.read_text(stamp_path))
        except (OSError, ValueError, safefile.NotPlainFile):
            stamp = {}
        want = _expected_key(key, stamp.get("key") if isinstance(stamp, dict) else None, design, fetched)
        if (
            isinstance(stamp, dict) and stamp.get("status") == "ok"
            and stamp.get("key_sha256") == key_sha256(want) and stamp.get("key") == want
        ):  # fmt: skip
            return SimResult(
                "ok", found, uio, clocks, log, sandbox=stamp.get("sandbox", ""), low=found_low,
                uio_oe_ever=stamp.get("uio_oe_ever"), uio_oe_any=stamp.get("uio_oe_any"),
                two_driver_clocks=stamp.get("two_driver_clocks"),
            )  # fmt: skip

    paths.require_free(paths.WORK, paths.min_free(), " (TTHFV_MIN_FREE)")
    out.mkdir(parents=True, exist_ok=True)
    trash = paths.WORK / "trash"
    for stale in out.iterdir():  # the evidence must be of this run only, and nothing left there is trusted
        if (note := safefile.discard(stale, trash)) and os.path.lexists(stale):
            raise UserError(f"{stale}: cannot be cleared: {note}")
    safefile.write_file(log, "")
    start = time.monotonic()

    track = _Track()

    def fail(status: Status, why: str, timed_out: bool = False) -> SimResult:
        with safefile.open_log(log) as f:
            f.write(f"{why}\n".encode())
        stamp_path.unlink(missing_ok=True)
        for partial in (dump, uio, low):
            partial.unlink(missing_ok=True)
        limited = track.limited or ("time" if timed_out else None)
        return SimResult(
            status,
            dump,
            uio,
            0,
            log,
            why,
            timed_out,
            sandbox.name(),
            limited,
            key["limits"],
            dict(track.peak) if key["limits"] else None,
        )

    if fetched.status != "ok" or not fetched.include_dirs or not fetched.top_module_used:
        return fail("verilate-fail", f"nothing to simulate: fetch status {fetched.status!r}")
    src = fetched.include_dirs[0]
    box = _box(fetched, src, scratch)
    safefile.make_empty_dir(scratch, trash)
    try:
        result = _simulate_in(
            design, fetched, clocks, key, digest, out, scratch, src, box, start, fail, track,
            _tb_args(clocks, stimulus_of(design, profile), drive, scratch, None),
        )  # fmt: skip
    except BaseException:
        _drop_scratch(scratch, trash)
        raise
    if note := _drop_scratch(scratch, trash):
        return fail("sim-fail", note)  # the note already says "scratch could not be removed"
    return result


def _box(fetched: FetchResult, src: Path, scratch: Path) -> sandbox.Sandbox | None:
    return sandbox.for_stage(
        rw=scratch,
        ro=[tools.suite_dir(), fetched.checkout or src, TB.parent],
        path=[tools.suite_dir() / "bin"],
    )


def _drop_scratch(scratch: Path, trash: Path) -> str:
    """Delete the sandbox's directory (unless TTHFV_KEEP_SCRATCH=1); '' or what went wrong."""
    if os.environ.get("TTHFV_KEEP_SCRATCH") == "1":
        return ""
    return safefile.discard(scratch, trash)


def _flags(x_initial: str) -> list[str]:
    """_BASE_FLAGS, with the model's initial state either fast (zeros) or unique (zeros or random at run time)."""
    flags = list(_BASE_FLAGS)
    flags[flags.index("--x-initial") + 1] = x_initial
    return flags


def _verilate(
    design: Design, fetched: FetchResult, key: dict, scratch: Path, src: Path, box, track: _Track,
    log: Path, x_initial: str = "fast",
) -> tuple[Status, str, bool] | None:  # fmt: skip
    """Build scratch/obj/Vtop. None if it worked, else (status, reason, timed_out) of the failure.

    The PDK cell rule (tthfv.pdkcells): if Verilator fails because a PDK cell module is missing, it runs once
    more with the models of every PDK cell the design references, written to scratch/pdk_cells.v and compiled
    last; key (the caller's dict) then gets their names and digest under "pdk_cells". A key that already has
    them gets the models from the start."""
    obj = scratch / "obj"
    obj.mkdir()
    top = fetched.top_module_used
    if fetched.top_module_fallback:
        with safefile.open_log(log) as f:
            f.write(f"top module {design.top_module} not declared; using {top}\n".encode())
    includes = [f"-I{d}" for d in fetched.include_dirs]
    cmd = [
        str(tools.tool("verilator")), *_flags(x_initial), "--top-module", top, "-Mdir", str(obj),
        *includes, "-y", str(src), *key["verilator_flags"], str(TB), *map(str, fetched.synth_sources),
    ]  # fmt: skip
    models = scratch / pdkcells.FILE_NAME
    plan = pdkcells.plan_for(fetched.synth_sources) if "pdk_cells" in key else None

    def run(args: list[str]) -> tuple[int, str]:
        if plan is not None:  # written just before each run: a retry empties scratch
            safefile.write_file(models, plan.text)
            args = [*args, str(models)]
        return _run(args, log, src, VERILATE_TIMEOUT, box, track)

    rc, text = run(cmd)
    note = ""
    if rc not in (0, LIMIT_RC, TIMED_OUT) and plan is None and pdkcells.missing_cell(text):
        found = pdkcells.plan_for(fetched.synth_sources)
        if not (note := pdkcells.retry_reason(text, found)):
            plan = found
            key.update(_pdk_key(plan, fetched))
            safefile.make_empty_dir(scratch, paths.WORK / "trash")
            obj.mkdir()
            rc, text = run(cmd)
    if (
        rc != 0
        and rc != LIMIT_RC
        and "--timing" not in cmd
        and ("NEEDTIMINGOPT" in text or "--timing" in text)
    ):
        safefile.make_empty_dir(scratch, paths.WORK / "trash")
        obj.mkdir()
        rc, text = run([*cmd[:1], "--timing", *cmd[1:]])
    if rc != 0 or not (obj / "Vtop").is_file():
        if rc == LIMIT_RC:
            return "verilate-fail", f"verilator: {text}", False
        if rc == TIMED_OUT:
            return "verilate-fail", f"verilator timed out after {VERILATE_TIMEOUT}s", True
        errors = re.findall(r"^%Error.*$", text, re.MULTILINE)
        why = errors[0] if errors else f"verilator failed (exit {rc})"
        if not note and plan is not None and pdkcells.missing_cell(text):
            note = pdkcells.retry_reason(text, plan)
        why = f"{note}: {why}" if note.startswith("needs") else why
        return "verilate-fail", macros.restate(why, text, fetched), False
    if too_big := _scratch_too_big(scratch):
        return "verilate-fail", too_big, False
    if plan is not None and _pdk_loops(fetched, key, scratch, src, box, track, log, plan):
        return "verilate-fail", pdkcells.loop_reason(plan), False
    return None


def _pdk_loops(fetched, key, scratch: Path, src: Path, box, track: _Track, log: Path, plan) -> bool:
    """The build's loop check (tthfv.pdkcells.loop_check) for a design simulated with PDK cell models: True if a
    combinational loop runs through a model, which a clocked simulation cannot reproduce. Run with yosys in the
    sandbox. A check that cannot run (an unsafe path, yosys failing for another reason) finds no loop, and says
    why in sim.log and in track.pdk_loop_check_note (sim.json and reference.json carry it)."""
    from tthfv.build import _unsafe_paths, defines  # the same reading rules as the build

    models = scratch / pdkcells.FILE_NAME
    safefile.write_file(models, plan.text)
    files = [*fetched.synth_sources, models]
    if unsafe := _unsafe_paths(files, fetched.include_dirs):
        return _loop_check_off(track, log, unsafe)
    opts = " ".join(
        [*(f"-D {d}" for d in defines(key["verilator_flags"])), *(f'-I "{d}"' for d in fetched.include_dirs)]
    )
    reads = [f'read_verilog -sv {opts} "{f}"'.replace("  ", " ") for f in files]
    script = scratch / "pdk_loops.ys"
    safefile.write_file(
        script, "\n".join([*reads, *pdkcells.loop_check(fetched.top_module_used, models)]) + "\n"
    )
    rc, text = _run(
        [str(tools.tool("yosys")), "-q", "-s", str(script)], log, src, VERILATE_TIMEOUT, box, track
    )
    if rc == 0:
        return False
    if rc not in (LIMIT_RC, TIMED_OUT) and f"@{pdkcells.LOOP_SET}" in text:
        return True
    errors = [ln.strip() for ln in text.splitlines() if "ERROR" in ln]
    why = "timed out" if rc == TIMED_OUT else (errors[-1] if errors else f"yosys exit status {rc}")
    return _loop_check_off(track, log, strip_control(why)[:300])


def _loop_check_off(track: _Track, log: Path, why: str) -> bool:
    note = f"PDK loop check could not run, so no loop was found: {why}"
    track.pdk_loop_check_note = note
    with safefile.open_log(log) as f:
        f.write(f"{note}\n".encode())
    return False


def _tb_args(
    clocks: int, stim: Stimulus, drive: int, out: Path, seed: int | None, flip_uio: int = 0
) -> list[str]:
    """The testbench's command line: the whole stimulus, as numbers (nothing the jail reads from the host)."""
    args = [
        "--clocks", str(clocks), "--ui-in", str(stim.ui_req), "--ui-drive", str(drive),
        "--ui-undriven", str(stim.ui_undriven), "--uio-in", str(stim.uio_undriven ^ flip_uio),
        "--ties", str(stim.ties), "--out", str(out),
    ]  # fmt: skip
    return args + ["--seed", str(seed)] if seed is not None else args


def _simulate_in(
    design, fetched, clocks, key, digest, out, scratch, src, box, start, fail, track, tb_args
) -> SimResult:
    """The part of simulate() that runs third-party code, in scratch. The host reads back only the dumps,
    through safefile, and only if they are plain files of exactly the expected size."""
    dump, uio, log, stamp_path = out / "uo_out.bin", out / "uio.bin", out / "sim.log", out / "sim.json"
    low = out / "uo_out_low.bin"
    track.scratch = scratch
    if failed := _verilate(design, fetched, key, scratch, src, box, track, log):
        return fail(*failed[:2], timed_out=failed[2])

    rc, text = _run(
        [str(scratch / "obj" / "Vtop"), *tb_args],
        log, src, SIM_TIMEOUT, box, track,
    )  # fmt: skip
    if rc == LIMIT_RC:
        return fail("sim-fail", f"simulation: {text}")
    if rc != 0:
        finished = re.search(r"ref_tb: design finished at clock \d+", text)
        if finished:
            return fail("sim-fail", finished.group(0))
        unsettled = re.search(r"ref_tb: the pad did not settle.*", text)
        if unsettled:
            return fail("sim-unsettled", unsettled.group(0))
        if rc == TIMED_OUT:
            return fail("sim-fail", f"simulation timed out after {SIM_TIMEOUT}s", timed_out=True)
        return fail("sim-fail", f"simulation exited with code {rc}")
    if too_big := _scratch_too_big(scratch):
        return fail("sim-fail", too_big)
    try:  # the only things the host takes from the sandbox's directory
        fd = safefile.open_dir(scratch)
        try:
            safefile.copy_plain(fd, "uo_out.bin", dump, max_size=clocks, exact=clocks)
            safefile.copy_plain(fd, "uo_out_low.bin", low, max_size=clocks, exact=clocks)
            safefile.copy_plain(fd, "uio.bin", uio, max_size=2 * clocks, exact=2 * clocks)
            uio_oe_ever = safefile.read_plain(fd, "uio_oe_ever.bin", max_size=1, exact=1)[0]
            uio_oe_any = safefile.read_plain(fd, "uio_oe_any.bin", max_size=1, exact=1)[0]
            two_driver = int.from_bytes(
                safefile.read_plain(fd, "two_driver_clocks.bin", max_size=8, exact=8), "little"
            )
        finally:
            os.close(fd)
    except safefile.NotPlainFile as e:
        return fail("sim-fail", f"the design's output was not a plain file of the expected size: {e}")

    stamp = {
        "status": "ok",
        "key": key,
        "key_sha256": key_sha256(key),  # not `digest`: _verilate adds the PDK models to key when it uses them
        "reason": "",
        "sandbox": sandbox.name(),
        "limits": key["limits"],
        "peak": dict(track.peak) if key["limits"] else None,
        "free_space_rule": track.free_space_rule,
        "uio_oe_ever": uio_oe_ever,
        "uio_oe_any": uio_oe_any,
        "two_driver_clocks": two_driver,
        "wall_seconds": round(time.monotonic() - start, 2),
        **({"pdk_loop_check_note": track.pdk_loop_check_note} if track.pdk_loop_check_note else {}),
    }
    safefile.write_file(stamp_path, json.dumps(stamp, indent=2) + "\n")
    return SimResult(
        "ok",
        dump,
        uio,
        clocks,
        log,
        sandbox=sandbox.name(),
        limits=key["limits"],
        peak=dict(track.peak) if key["limits"] else None,
        low=low,
        uio_oe_ever=uio_oe_ever,
        uio_oe_any=uio_oe_any,
        two_driver_clocks=two_driver,
    )


def _scratch_too_big(scratch: Path) -> str:
    """'' or the reason the sandbox's directory grew past safefile.MAX_SCRATCH bytes or MAX_ENTRIES entries."""
    over = safefile.scratch_overrun(scratch)
    return f"the design filled its scratch directory: {over.split(': ', 1)[1]}" if over else ""


def _finished(
    ref: Path, key: dict, design: Design | None = None, fetched: FetchResult | None = None
) -> dict | None:
    """The existing reference.json if it is a finished reference made with exactly this key, else None.

    Not finished: unreadable, written by an older version (no frames_complete, no key, not schema 3), naming
    a dump that is gone, or made with a different key (any input, the profile, the clock count, the tools,
    the testbench). A reference made with PDK cell models must carry the models the design gets today."""
    try:
        done = json.loads(safefile.read_text(ref))
        if "frames_complete" not in done or not safefile.is_plain(ref.parent / done["dump"]):
            return None
        if done.get("schema") != SCHEMA:
            return None
        if fetched is not None and design is not None:
            key = _expected_key(key, done.get("key"), design, fetched)
        if done.get("key") != key or done.get("key_sha256") != key_sha256(key):
            return None
    except (OSError, ValueError, KeyError, TypeError, safefile.NotPlainFile):
        return None
    return done


@dataclass(frozen=True)
class ReferenceResult:
    status: str  # "ok", "no-sync", "verilate-fail" or "sim-fail"
    mode: str | None
    frames_complete: int
    reason: str = ""  # why a failed simulation failed; empty otherwise
    timed_out: bool = False  # the simulation or Verilator timed out
    limited: str | None = None  # "memory", "tasks" or "time": a resource limit ended the simulation
    ui_in_unmet: int = (
        0  # ui_in bits whose applied level is not the requested one (reference.json ui_in_unmet)
    )
    uio_in_unmet: int = 0  # the same for uio_in
    ui_in_requested: int = 0
    uio_in_requested: int = 0

    @property
    def unmet_text(self) -> str:
        return frames.unmet_text(
            self.ui_in_unmet, self.uio_in_unmet, self.ui_in_requested, self.uio_in_requested
        )

    @property
    def unmet(self) -> bool:
        """The reference was made, but not under the inputs the manifest asked for."""
        return bool(self.ui_in_unmet or self.uio_in_unmet)


def _record_failure(
    out: Path,
    design: Design,
    status: str,
    reason: str,
    timed_out: bool,
    limited: str | None = None,
    limits: dict | None = None,
) -> None:
    """Leave reference-fail.json (by rename) so the results index can say why there is no reference
    and whether a timeout caused it. A failure is never reused: every run simulates again."""
    out.mkdir(parents=True, exist_ok=True)
    safefile.write_file(
        out / "reference-fail.json",
        json.dumps(
            {
                "id": design.id,
                "status": status,
                "reason": reason,
                "timed_out": timed_out,
                "limited": limited,
                "limits": limits,
            },
            indent=2,
        )
        + "\n",
    )


# ---- plan 1's layout: WORK/reference/<id>/{reference.json, uo_out.bin.gz, ...} was the `ideal` reference

_OLD_FILES = (
    "reference.json", "reference.json.tmp", "reference-fail.json", "sim.json", "sim.log",
    "uo_out.bin", "uo_out.bin.gz", "uio.bin", "uio.bin.gz",
)  # fmt: skip
_KEEP_OUT_OF_THE_MOVE = ("reference.json", "reference.json.tmp", "reference-fail.json", "sim.log", SCRATCH)


def _old_files(base: Path) -> list[Path]:
    names = [base / n for n in _OLD_FILES] + sorted(base.glob("frame-*.png")) + [base / SCRATCH]
    return [p for p in names if os.path.lexists(p)]


def _old_key(key: dict) -> dict:
    """What an ideal key looked like before profiles: today's key without the profile fields."""
    return {k: v for k, v in key.items() if k not in ("profile", "ties")}


def _remove_old(base: Path) -> None:
    """Remove the plan 1 files of a design: only called once its reference under a profile exists."""
    trash = paths.WORK / "trash"
    for p in _old_files(base):
        if (note := safefile.discard(p, trash)) and os.path.lexists(p):
            raise UserError(f"{p}: old reference cannot be removed: {note}")


def _adopt_old(design: Design, key: dict, out: Path) -> dict | None:
    """Read a plan 1 reference (directly under WORK/reference/<id>/) as the `ideal` reference, if and only if
    the key it carries is exactly the key `ideal` computes today without the profile fields. Then its files
    move (by rename) into <id>/ideal/, and its reference.json becomes schema 3: the fields plan 1 did not
    measure are null (phase_sensitive: it has no low-phase dump) or derived from what it holds
    (uio_oe_ever, from uio.bin.gz), and `migrated_from_schema` says so. Returns the new record, or None
    (nothing touched) if there is no such reference or it does not match."""
    base = out.parent
    try:
        old = json.loads(safefile.read_text(base / "reference.json"))
        if old.get("schema") != 2 or "frames_complete" not in old:
            return None
        if old.get("key") != _old_key(key) or old.get("key_sha256") != key_sha256(old["key"]):
            return None
        if not all(safefile.is_plain(base / old[n]) for n in ("dump", "uio_dump")):
            return None
        uio_oe = frames.load_dump(base / old["uio_dump"], host_owned=True)[1::2]
    except (OSError, ValueError, KeyError, TypeError, safefile.NotPlainFile):
        return None
    out.mkdir(parents=True, exist_ok=True)
    for p in _old_files(base):
        if p.name not in _KEEP_OUT_OF_THE_MOVE:
            os.rename(p, out / p.name)
    record = {
        **old,
        "schema": SCHEMA,
        "profile": IDEAL_NAME,
        "ui_in_mask_used": 0xFF,
        "ui_in_mask_note": "ideal profile: nothing is tied; carried over from plan 1 (pass 1)",
        "ui_in_requested": old["key"]["ui_in"],
        "ui_in_applied": old["key"]["ui_in"],
        "ui_in_unmet": 0,
        "reference_pass": 1,
        "two_driver_clocks": None,
        "uio_in_requested": old["key"]["uio_in"],
        "uio_in_applied": old["key"]["uio_in"],
        "uio_in_unmet": 0,
        "uio_oe_any": int(np.bitwise_or.reduce(uio_oe, initial=0)),
        "uio_oe_ever": int(np.bitwise_or.reduce(uio_oe[frames.RESET_CLOCKS :], initial=0)),
        "low_dump": None,
        "low_dump_sha256": None,
        "phase_sensitive": None,
        "reset_determined": None,
        "input_sensitive": None,
        "migrated_from_schema": 2,
        "key": key,
        "key_sha256": key_sha256(key),
    }
    safefile.write_file(out / "reference.json", json.dumps(record, indent=2) + "\n")
    _remove_old(base)
    return record


# ---- --sensitivity


def _flip_note(stim: Stimulus, profile: Profile) -> str:
    """What --sensitivity flipped and what it did not, for reference.json."""
    if profile.data is None:
        return "ideal profile: no uncertain uio_in bits, nothing was flipped"
    fl = profile.data["stimulus"]["floating_assumed_low"]
    held = profile.data["stimulus"]["ui_in_mask"] ^ 0xFF
    return (
        f"uio_in bits 0x{stim.uncertain:02x} flipped together in one run (the profile's uio_in_uncertain); "
        f"not flipped: the nets taken as low by assumption (floating_assumed_low: ui_in 0x{fl['ui_in']:02x}, "
        f"uio 0x{fl['uio']:02x}) and the ui_in nets the board holds, which could not be told from a driver "
        f"(ui_in bits 0x{held:02x}: held at their levels)"
    )


def _sensitivity(
    design: Design,
    fetched: FetchResult,
    clocks: int,
    profile: Profile,
    out: Path,
    reference_uo: np.ndarray,
    drive: int,
) -> tuple[dict, str]:
    """Simulate the design again in the sandbox to find out what its picture depends on beyond reset and the
    profile's stimulus: two random initial states (SENS_SEEDS) and, if the profile has uncertain uio_in bits,
    those bits flipped. One model is built with --x-initial unique; the runs are zero state (`base`), seed 1,
    seed 2 and `flip`, all with the MCU driving the ui_in bits `drive` (the stored reference's). Returns
    ({"reset_determined", "input_sensitive"}, notes). A run that stops (a design that calls $finish from a
    random state) counts as a different output."""
    stim = stimulus_of(design, profile)
    log, scratch = out / "sens.log", out / "scratch-sens"
    trash = paths.WORK / "trash"
    safefile.write_file(log, "")
    src = fetched.include_dirs[0]
    key = sim_key(design, fetched, clocks, profile, drive)
    box = _box(fetched, src, scratch)
    track = _Track()
    safefile.make_empty_dir(scratch, trash)
    track.scratch = scratch
    notes: list[str] = [_flip_note(stim, profile)]
    got: dict[str, np.ndarray | None] = {}
    try:
        if failed := _verilate(design, fetched, key, scratch, src, box, track, log, x_initial="unique"):
            raise UserError(f"sensitivity: {failed[1]} (see {log})")
        runs: dict[str, tuple[int | None, int]] = {
            "base": (None, 0),
            **{f"seed{n}": (n, 0) for n in SENS_SEEDS},
        }
        if stim.uncertain:
            runs["flip"] = (None, stim.uncertain)
        for name, (seed, flip) in runs.items():
            run_dir = scratch / name
            safefile.make_empty_dir(run_dir, trash)  # whatever the jail left under that name is gone
            rc, text = _run(
                [str(scratch / "obj" / "Vtop"), *_tb_args(clocks, stim, drive, run_dir, seed, flip)],
                log, src, SIM_TIMEOUT, box, track,
            )  # fmt: skip
            if rc == LIMIT_RC:
                raise UserError(f"sensitivity: simulation {name}: {text}")
            if rc == TIMED_OUT:
                raise UserError(f"sensitivity: simulation {name} timed out after {SIM_TIMEOUT}s")
            if too_big := _scratch_too_big(scratch):
                raise UserError(f"sensitivity: simulation {name}: {too_big}")
            if rc != 0:
                notes.append(f"{name}: the simulation stopped (exit {rc}): counted as a different output")
                got[name] = None
                continue
            try:
                fd = safefile.open_dir(run_dir)
                try:
                    raw = safefile.read_plain(fd, "uo_out.bin", max_size=clocks, exact=clocks)
                finally:
                    os.close(fd)
            except safefile.NotPlainFile as e:
                raise UserError(f"sensitivity: {name}: not a plain file of the expected size: {e}") from e
            got[name] = np.frombuffer(raw, dtype=np.uint8)
            safefile.discard(run_dir, trash)
    finally:
        _drop_scratch(scratch, trash)

    r = frames.RESET_CLOCKS

    def same(x: np.ndarray | None, y: np.ndarray | None) -> bool:
        return x is not None and y is not None and len(x) == len(y) and bool(np.array_equal(x[r:], y[r:]))

    base = got["base"]
    if base is None or not np.array_equal(base, reference_uo):
        notes.append("base: the zero state of the --x-initial unique model differs from the reference")
    reset_determined = all(same(got[f"seed{n}"], reference_uo) for n in SENS_SEEDS)
    sensitive = bool(stim.uncertain) and not same(got["flip"], base)
    found = {"reset_determined": reset_determined, "input_sensitive": {"uio_in_uncertain": sensitive}}
    return found, "; ".join(notes)


def _add_sensitivity(design, fetched, clocks, profile, out, ref: Path) -> None:
    record = json.loads(safefile.read_text(ref))
    uo = frames.load_dump(out / record["dump"], host_owned=True)
    try:
        drive = json.loads(safefile.read_text(out / "sim.json"))["key"]["ui_drive"]
    except (OSError, ValueError, KeyError, safefile.NotPlainFile):
        drive = 0xFF  # an adopted plan 1 reference: ideal, the MCU drove everything
    try:
        found, note = _sensitivity(design, fetched, clocks, profile, out, uo, drive)
        record.update(found)
    except UserError as e:
        note = f"sensitivity not determined: {e}"
        print(strip_control(f"{design.id}: WARNING: {note}"), file=sys.stderr)
    record["sensitivity_note"] = note
    safefile.write_file(ref, json.dumps(record, indent=2) + "\n")


# ---- the two passes


def _promote_pass2(out: Path, keep_dump: bool) -> None:
    """Pass 2 was simulated in out/pass2/. If it is the stored reference its dumps and stamp replace pass 1's
    (by rename); either way its log is kept as sim-pass2.log and the directory goes."""
    sub = out / "pass2"
    if keep_dump:
        for name in ("uo_out.bin", "uo_out_low.bin", "uio.bin", "sim.json"):
            os.rename(sub / name, out / name)
    if safefile.is_plain(sub / "sim.log"):
        os.rename(sub / "sim.log", out / "sim-pass2.log")
    safefile.discard(sub, paths.WORK / "trash")


def _choose(design, fetched, clocks, force, profile, stim, sim1: SimResult, out: Path):
    """Pass 1 (the tied ui_in bits undriven) gave uio_oe_any, the enables over every sample (before clock 0,
    reset, both phases). Decide which simulation is the reference: returns (sim, mask_used, pass number, note).
    Pass 2 (the MCU drives the tied bits the design never enables) is made only if a newly allowed tied bit is
    requested at a level other than the one pass 1 presented. It is rejected if its own uio_oe_any shows an
    enable on a tied bit the MCU drives (a clock with two drivers always shows one, so no separate check)."""
    oe1 = sim1.uio_oe_any or 0
    cons, final = conservative_mask(profile), final_mask(profile, oe1)
    if profile.data is None:
        return sim1, 0xFF, 1, "ideal profile: nothing is tied, the MCU drives every ui_in bit (pass 1)"
    conflicts = profile.data["drive_conflicts"]
    if not conflicts:
        return sim1, final, 1, "the profile has no ties: the profile's whole mask (pass 1)"
    enabled = [c["uio"] for c in conflicts if oe1 >> c["uio"] & 1]
    if enabled:
        what = (
            f"the design enables {', '.join(f'uio[{k}]' for k in enabled)} as an output (somewhere from before "
            f"clock 0 on) and tied to ui_in: the MCU must not drive {', '.join(f'ui_in[{k}]' for k in enabled)}"
        )
    else:
        what = f"the design never enables a tied uio bit (uio_oe_any 0x{oe1:02x}): the profile's whole mask applies"
    newly = final & ~cons
    differ = newly & stim.ties & (stim.ui_req ^ stim.uio_undriven)
    if differ == 0:
        why = "pass 2 not needed: " + (
            f"every newly allowed tied bit ({_bits(newly & stim.ties, 'ui_in')}) is requested at the level pass 1 "
            "presented (the undriven level)"
            if newly & stim.ties
            else "no tied bit is newly allowed"
        )
        return sim1, final, 1, f"{what}; pass 1 stands ({why})"
    sim2 = simulate(design, fetched, clocks, force=force, profile=profile, ui_drive=final, out_sub="pass2")
    tied_driven = stim.ties & final
    problem = ""
    if sim2.status != "ok":
        problem = f"pass 2 failed: {sim2.status}: {sim2.reason}"
    elif (sim2.uio_oe_any or 0) & tied_driven:
        problem = f"pass 2's uio_oe_any 0x{sim2.uio_oe_any:02x} enables a tied bit the MCU drives"
    if problem:
        if sim2.status == "ok":
            _promote_pass2(out, keep_dump=False)
        else:
            safefile.discard(out / "pass2", paths.WORK / "trash")
        return (
            sim1, cons, 1,
            (
                f"{what}; pass 2 (MCU drives {_bits(differ, 'ui_in')}, mask 0x{final:02x}) was rejected: "
                f"{problem}; fell back to pass 1, conservative mask 0x{cons:02x}"
            ),
        )  # fmt: skip
    _promote_pass2(out, keep_dump=True)
    sim = SimResult(
        "ok", out / "uo_out.bin", out / "uio.bin", sim2.clocks, out / "sim.log", sandbox=sim2.sandbox,
        limits=sim2.limits, peak=sim2.peak, low=out / "uo_out_low.bin", uio_oe_ever=sim2.uio_oe_ever,
        uio_oe_any=sim2.uio_oe_any, two_driver_clocks=sim2.two_driver_clocks,
    )  # fmt: skip
    return (
        sim, final, 2,
        (
            f"{what}; pass 2 stands: the MCU drives {_bits(differ, 'ui_in')} (requested at another level than pass 1 "
            f"presented), and pass 2's uio_oe_any 0x{sim2.uio_oe_any:02x} shows no enable on a tied bit it drives "
            f"(two_driver_clocks {sim2.two_driver_clocks})"
        ),
    )  # fmt: skip


def make_reference(
    design: Design,
    clocks: int | None = None,
    force: bool = False,
    profile: Profile | None = None,
    sensitivity: bool = False,
) -> ReferenceResult:
    """Fetch, simulate, cut and write one design's reference under a profile; the whole `tthfv reference`
    pipeline.

    Without force, a finished reference.json made with the same simulation key is reused. A failed simulation
    is a result, recorded in reference-fail.json; it is not an exception. With sensitivity, the reference
    also carries reset_determined and input_sensitive (measured once; a finished reference without them is
    completed, not simulated again). A reference that plan 1 left directly under work/reference/<id>/ is
    carried over to `ideal` only if its key matches (_adopt_old), and is removed only once the new one exists.
    An interrupted run leaves no half-made profile directory."""
    profile = profile or IDEAL
    out = reference_dir(design.id, profile.name)
    ref = out / "reference.json"
    clocks = DEFAULT_CLOCKS if clocks is None else clocks
    fetched = fetch.fetch(design)
    if fetched.status != "ok":
        print(strip_control(f"{design.id}: fetch {fetched.status}: {fetched.reason}"), file=sys.stderr)
    else:
        key = sim_key(design, fetched, clocks, profile)
        done = None if force else _finished(ref, key, design, fetched)
        if done is None and not force and profile.ideal and not ref.exists():
            done = _adopt_old(design, key, out)
        if done is not None:
            if sensitivity and done.get("reset_determined") is None:
                _add_sensitivity(design, fetched, clocks, profile, out, ref)
            return _result_of(done)  # the same function the fresh path uses
    ref.unlink(missing_ok=True)
    (out / "reference-fail.json").unlink(missing_ok=True)
    try:
        return _make(design, fetched, clocks, force, profile, sensitivity, out, ref)
    except BaseException:
        finished = (out / "reference.json").exists() or (out / "reference-fail.json").exists()
        if (
            os.path.lexists(out) and not finished
        ):  # a completed reference stays (sensitivity is retried next run)
            safefile.discard(
                out, paths.WORK / "trash"
            )  # nothing in it is a result: do not leave sim.log alone
        raise


def _result_of(record: dict) -> ReferenceResult:
    """The result of a finished reference.json; a reference without sync that was made under unmet inputs says so
    in its reason (the line `tthfv reference` prints)."""
    ui, uio = record.get("ui_in_unmet") or 0, record.get("uio_in_unmet") or 0
    ok = bool(record["frames"])
    req_ui, req_uio = record.get("ui_in_requested") or 0, record.get("uio_in_requested") or 0
    reason = "" if ok or not (ui or uio) else f"inputs unmet: {frames.unmet_text(ui, uio, req_ui, req_uio)}"
    return ReferenceResult(
        "ok" if ok else "no-sync",
        record["mode"],
        record["frames_complete"],
        reason,
        ui_in_unmet=ui,
        uio_in_unmet=uio,
        ui_in_requested=req_ui,
        uio_in_requested=req_uio,
    )


def _make(design, fetched, clocks, force, profile, sensitivity, out, ref) -> ReferenceResult:
    sim = simulate(design, fetched, clocks, force=force, profile=profile)
    if sim.status != "ok":
        print(f"{design.id}: see {sim.log}", file=sys.stderr)
        _record_failure(out, design, sim.status, sim.reason, sim.timed_out, sim.limited, sim.limits)
        return ReferenceResult(sim.status, None, 0, sim.reason, sim.timed_out, sim.limited)
    stim = stimulus_of(design, profile)
    sim, mask, which, note = _choose(design, fetched, clocks, force, profile, stim, sim, out)
    cut = frames.cut_frames(frames.load_dump(sim.dump, host_owned=True))
    applied, applied_uio = applied_ui_in(stim, mask), applied_uio_in(stim, mask)
    ui_unmet, uio_unmet = (stim.ui_req ^ applied) & 0xFF, (stim.uio_requested ^ applied_uio) & 0xFF
    if ui_unmet or uio_unmet:
        note += f"; INPUTS UNMET: {frames.unmet_text(ui_unmet, uio_unmet, stim.ui_req, stim.uio_requested)}"
    frames.write_reference(
        design, sim, cut, profile=profile.name, ui_in_mask_used=mask, ui_in_mask_note=note,
        stimulus={
            "ui_in_requested": stim.ui_req,
            "ui_in_applied": applied,
            "ui_in_unmet": ui_unmet,
            "reference_pass": which,
            "two_driver_clocks": sim.two_driver_clocks,
            "uio_in_requested": stim.uio_requested,
            "uio_in_applied": applied_uio,
            "uio_in_unmet": uio_unmet,
        },
    )  # fmt: skip
    if profile.ideal:
        _remove_old(out.parent)  # replaced: the plan 1 files of this design are no longer the reference
    if sensitivity:
        _add_sensitivity(design, fetched, clocks, profile, out, ref)
    return _result_of(json.loads(safefile.read_text(ref)))


def _cmd_reference(args: argparse.Namespace) -> int:
    return sandbox.command(lambda: _reference_command(args))


def _reference_command(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)  # before anything is simulated: an invalid profile stops here
    sandbox.announce()
    safefile.empty_trash_and_say(paths.WORK / "trash")
    designs = manifest.load(extras=manifest.EXTRAS_DEFAULT)
    chosen = [manifest.by_id(designs, i) for i in args.ids]
    bad = unmet = 0
    for d in chosen:
        r = make_reference(d, args.clocks, args.force, profile, args.sensitivity)
        why = f" ({r.reason})" if r.reason else ""
        mark = f" [inputs unmet: {r.unmet_text}]" if r.unmet and r.status == "ok" else ""
        print(strip_control(f"{d.id} {r.status}{why} {r.mode or '-'} {r.frames_complete} frames{mark}"))
        unmet += r.unmet
        bad += r.status != "ok"
    if unmet:  # one warning for the run, after the last design; each line above says which and what
        print(
            f"WARNING: {unmet} of {len(chosen)} references have unmet inputs (see each line)", file=sys.stderr
        )
    return 1 if bad else 0


def add_parser(sub) -> None:
    p = sub.add_parser("reference", help="simulate designs and cut the output into reference frames")
    p.add_argument("ids", nargs="+", metavar="id", help="<shuttle>/<macro>")
    p.add_argument("--clocks", type=int, default=None, help=f"clocks to simulate (default {DEFAULT_CLOCKS})")
    p.add_argument("--force", action="store_true", help="simulate and cut again even if already done")
    p.add_argument(
        "--profile", default=IDEAL_NAME, metavar="ideal|boards/<name>.json",
        help="the inputs to simulate under: ideal (the manifest's fixed inputs) or a board's census profile; "
        "the reference is stored under work/reference/<id>/<profile name>/",
    )  # fmt: skip
    p.add_argument(
        "--sensitivity", action="store_true",
        help="also simulate with two random initial states (reset_determined) and with the profile's uncertain "
        "uio_in bits flipped (input_sensitive), and record both in reference.json",
    )  # fmt: skip
    p.set_defaults(func=_cmd_reference)

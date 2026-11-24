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

from tthfv import fetch, frames, manifest, paths, proc, safefile, sandbox, tools
from tthfv.board import census
from tthfv.errors import UserError
from tthfv.fetch import FetchResult
from tthfv.manifest import Design
from tthfv.text import strip_control

Status = Literal["ok", "verilate-fail", "sim-fail"]

DEFAULT_CLOCKS = 6_000_000
# Bump when the testbench's stimulus changes in a way the key cannot see. 2: the pad readback model. (The
# board profile, with its ties, is in the key; the testbench hash covers the rest.)
STIMULUS_VERSION = 2
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
    uio_oe_ever: int | None = None  # OR of uio_oe over every clock from clock 10 on


LIMIT_RC = -2  # what _run returns for a program that hit a cgroup or disk limit; its text is the reason
TIMED_OUT = -1  # what _run returns for a program killed on its timeout (124 is then just an exit status)


class _Track:
    """What the programs of one simulation used and hit, for the record."""

    def __init__(self):
        self.limited: str | None = None
        self.free_space_rule = ""  # "off: <why>" if the disk watchdog's free-space check could not run
        self.scratch: Path | None = None  # the directory the program writes, watched while it runs
        self.peak = {"memory": 0, "tasks": 0}


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
    ui_in: int  # level of ui_in (what the MCU presents, or the held level)
    uio_in: int  # level of each uio net nobody drives
    ties: int  # bit k set: ui_in[k] and uio[k] are one net
    uncertain: int  # uio_in bits whose level is not known (--sensitivity flips them)


def stimulus_of(design: Design, profile: Profile) -> Stimulus:
    if profile.data is None:
        return Stimulus(design.ui_in, design.uio_in, 0, 0)
    st = profile.data["stimulus"]
    ties = 0
    for c in profile.data["drive_conflicts"]:
        if c["ui_in"] != c["uio"]:  # the census only ties ui_in[k] to uio[k]
            raise UserError(f"profile {profile.name}: a tie of ui_in[{c['ui_in']}] to uio[{c['uio']}]")
        ties |= 1 << c["ui_in"]
    return Stimulus(st["ui_in"], st["uio_in"], ties, st["uio_in_uncertain"])


def reference_dir(design_id: str, profile_name: str) -> Path:
    """WORK/reference/<shuttle>/<macro>/<profile>"""
    base = paths.design_dir("reference", design_id)
    out = base / profile_name
    paths.no_symlinks(out, paths.WORK)
    return out


def ui_in_mask_for(profile: Profile, uio_oe_ever: int) -> tuple[int, str]:
    """The ui_in bits the MCU may drive for a design that enables the uio bits in uio_oe_ever, and why. The
    reference was simulated with every tied ui_in bit undriven (the conservative mask); for a design that
    never enables the tied uio bit the two are the same simulation, which is checked here: the level an
    undriven tied net sits at (the uio level) has to equal the level the MCU would drive."""
    if profile.data is None:
        return 0xFF, "ideal profile: nothing is tied, the MCU may drive every ui_in bit"
    conservative = census.effective_ui_in_mask(profile.data, 0xFF)
    mask = census.effective_ui_in_mask(profile.data, uio_oe_ever)
    conflicts = profile.data["drive_conflicts"]
    if not conflicts:
        return mask, "the profile has no ties: the whole profile mask"
    st = profile.data["stimulus"]
    for c in conflicts:
        k = c["ui_in"]
        if mask >> k & 1 and not conservative >> k & 1 and (st["ui_in"] >> k & 1) != (st["uio_in"] >> k & 1):
            raise UserError(
                f"profile {profile.name}: ui_in[{k}] would be driven {st['ui_in'] >> k & 1} but the tied "
                f"uio[{k}] sits at {st['uio_in'] >> k & 1}: the reference simulated with ui_in[{k}] undriven "
                "does not stand for it"
            )
    enabled = [c["uio"] for c in conflicts if uio_oe_ever >> c["uio"] & 1]
    if enabled:
        bits = ", ".join(f"uio[{k}]" for k in enabled)
        return mask, (
            f"the design enables {bits} as an output and tied to ui_in: the MCU must not drive "
            f"{', '.join(f'ui_in[{k}]' for k in enabled)}; simulated with exactly this mask"
        )
    return mask, (
        f"the design never enables a tied uio bit (uio_oe_ever 0x{uio_oe_ever:02x}): the profile's whole mask "
        "applies; simulated with the tied bits undriven, which is the same simulation because each tied ui_in "
        "bit and its uio bit sit at the same level"
    )


def sim_key(design: Design, fetched: FetchResult, clocks: int, profile: Profile = IDEAL) -> dict:
    """Everything that decides the dump: the design's identity and sources, the stimulus (the whole profile),
    the simulator and its flags, the testbench and the frame cutter version. Written to sim.json and
    reference.json; a reference is reused only if the key it carries equals the current one."""
    stim = stimulus_of(design, profile)
    return {
        "repo": design.repo,
        "commit": design.commit,
        "sources_sha256": fetched.sources_sha256,
        "compilation_units": [
            str(p.relative_to(fetched.src_dir) if p.is_relative_to(fetched.src_dir) else p)
            for p in fetched.synth_sources
        ],
        "top_module_used": fetched.top_module_used,
        "ui_in": stim.ui_in,
        "uio_in": stim.uio_in,
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
) -> SimResult:
    """Verilate the design's own top module and dump uo_out (and uio_out/uio_oe) for `clocks` clocks, under
    the profile's inputs. Every tied ui_in bit is simulated as not driven by the MCU (the conservative mask,
    see ui_in_mask_for).

    Without force, a finished simulation with the same clocks, inputs, top module, flags and
    source contents is reused.
    Verilator and the simulation run with the project's src directory as working directory, so
    relative .hex/.mem paths in the design resolve.
    """
    out = reference_dir(design.id, profile.name).resolve()
    dump, uio, log, stamp_path = out / "uo_out.bin", out / "uio.bin", out / "sim.log", out / "sim.json"
    low = out / "uo_out_low.bin"
    scratch = out / SCRATCH
    key = sim_key(design, fetched, clocks, profile)
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
        if stamp.get("status") == "ok" and stamp.get("key_sha256") == digest and stamp.get("key") == key:
            return SimResult(
                "ok", found, uio, clocks, log, sandbox=stamp.get("sandbox", ""), low=found_low,
                uio_oe_ever=stamp.get("uio_oe_ever"),
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
            design, fetched, clocks, key, digest, out, scratch, src, box, start, fail, track
        )
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
    """Build scratch/obj/Vtop. None if it worked, else (status, reason, timed_out) of the failure."""
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
    rc, text = _run(cmd, log, src, VERILATE_TIMEOUT, box, track)
    if (
        rc != 0
        and rc != LIMIT_RC
        and "--timing" not in cmd
        and ("NEEDTIMINGOPT" in text or "--timing" in text)
    ):
        safefile.make_empty_dir(scratch, paths.WORK / "trash")
        obj.mkdir()
        rc, text = _run([*cmd[:1], "--timing", *cmd[1:]], log, src, VERILATE_TIMEOUT, box, track)
    if rc != 0 or not (obj / "Vtop").is_file():
        if rc == LIMIT_RC:
            return "verilate-fail", f"verilator: {text}", False
        if rc == TIMED_OUT:
            return "verilate-fail", f"verilator timed out after {VERILATE_TIMEOUT}s", True
        errors = re.findall(r"^%Error.*$", text, re.MULTILINE)
        return "verilate-fail", errors[0] if errors else f"verilator failed (exit {rc})", False
    if too_big := _scratch_too_big(scratch):
        return "verilate-fail", too_big, False
    return None


def _tb_args(clocks: int, ui_in: int, uio_in: int, ties: int, out: Path, seed: int | None) -> list[str]:
    args = [
        "--clocks", str(clocks), "--ui-in", str(ui_in), "--uio-in", str(uio_in), "--ties", str(ties),
        "--out", str(out),
    ]  # fmt: skip
    return args + ["--seed", str(seed)] if seed is not None else args


def _simulate_in(
    design, fetched, clocks, key, digest, out, scratch, src, box, start, fail, track
) -> SimResult:
    """The part of simulate() that runs third-party code, in scratch. The host reads back only the dumps,
    through safefile, and only if they are plain files of exactly the expected size."""
    dump, uio, log, stamp_path = out / "uo_out.bin", out / "uio.bin", out / "sim.log", out / "sim.json"
    low = out / "uo_out_low.bin"
    track.scratch = scratch
    if failed := _verilate(design, fetched, key, scratch, src, box, track, log):
        return fail(*failed[:2], timed_out=failed[2])

    rc, text = _run(
        [str(scratch / "obj" / "Vtop"), *_tb_args(clocks, key["ui_in"], key["uio_in"], key["ties"], scratch, None)],
        log, src, SIM_TIMEOUT, box, track,
    )  # fmt: skip
    if rc == LIMIT_RC:
        return fail("sim-fail", f"simulation: {text}")
    if rc != 0:
        finished = re.search(r"ref_tb: design finished at clock \d+", text)
        if finished:
            return fail("sim-fail", finished.group(0))
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
        finally:
            os.close(fd)
    except safefile.NotPlainFile as e:
        return fail("sim-fail", f"the design's output was not a plain file of the expected size: {e}")

    stamp = {
        "status": "ok",
        "key": key,
        "key_sha256": digest,
        "reason": "",
        "sandbox": sandbox.name(),
        "limits": key["limits"],
        "peak": dict(track.peak) if key["limits"] else None,
        "free_space_rule": track.free_space_rule,
        "uio_oe_ever": uio_oe_ever,
        "wall_seconds": round(time.monotonic() - start, 2),
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
    )


def _scratch_too_big(scratch: Path) -> str:
    """'' or the reason the sandbox's directory grew past safefile.MAX_SCRATCH bytes or MAX_ENTRIES entries."""
    over = safefile.scratch_overrun(scratch)
    return f"the design filled its scratch directory: {over.split(': ', 1)[1]}" if over else ""


def _finished(ref: Path, key: dict) -> dict | None:
    """The existing reference.json if it is a finished reference made with exactly this key, else None.

    Not finished: unreadable, written by an older version (no frames_complete, no key, not schema 3), naming
    a dump that is gone, or made with a different key (any input, the profile, the clock count, the tools,
    the testbench)."""
    try:
        done = json.loads(safefile.read_text(ref))
        if "frames_complete" not in done or not safefile.is_plain(ref.parent / done["dump"]):
            return None
        if done.get("schema") != SCHEMA:
            return None
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
        "ui_in_mask_note": ui_in_mask_for(IDEAL, 0)[1],
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


def _sensitivity(
    design: Design, fetched: FetchResult, clocks: int, profile: Profile, out: Path, reference_uo: np.ndarray
) -> tuple[dict, str]:
    """Simulate the design again in the sandbox to find out what its picture depends on beyond reset and the
    profile's stimulus: two random initial states (SENS_SEEDS) and, if the profile has uncertain uio_in bits,
    those bits flipped. One model is built with --x-initial unique; the runs are zero state (`base`), seed 1,
    seed 2 and `flip`. Returns ({"reset_determined", "input_sensitive"}, notes). A run that stops (a design
    that calls $finish from a random state) counts as a different output."""
    stim = stimulus_of(design, profile)
    log, scratch = out / "sens.log", out / "scratch-sens"
    trash = paths.WORK / "trash"
    safefile.write_file(log, "")
    src = fetched.include_dirs[0]
    key = sim_key(design, fetched, clocks, profile)
    box = _box(fetched, src, scratch)
    track = _Track()
    safefile.make_empty_dir(scratch, trash)
    track.scratch = scratch
    notes: list[str] = []
    got: dict[str, np.ndarray | None] = {}
    try:
        if failed := _verilate(design, fetched, key, scratch, src, box, track, log, x_initial="unique"):
            raise UserError(f"sensitivity: {failed[1]} (see {log})")
        runs: dict[str, tuple[int, int, int | None]] = {
            "base": (stim.ui_in, stim.uio_in, None),
            **{f"seed{n}": (stim.ui_in, stim.uio_in, n) for n in SENS_SEEDS},
        }
        if stim.uncertain:
            runs["flip"] = (stim.ui_in, stim.uio_in ^ stim.uncertain, None)
        for name, (ui_in, uio_in, seed) in runs.items():
            run_dir = scratch / name
            run_dir.mkdir()
            rc, text = _run(
                [str(scratch / "obj" / "Vtop"), *_tb_args(clocks, ui_in, uio_in, stim.ties, run_dir, seed)],
                log, src, SIM_TIMEOUT, box, track,
            )  # fmt: skip
            if rc == LIMIT_RC:
                raise UserError(f"sensitivity: simulation {name}: {text}")
            if rc == TIMED_OUT:
                raise UserError(f"sensitivity: simulation {name} timed out after {SIM_TIMEOUT}s")
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
    found, note = _sensitivity(design, fetched, clocks, profile, out, uo)
    record.update(found)
    record["sensitivity_note"] = note
    safefile.write_file(ref, json.dumps(record, indent=2) + "\n")


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
    carried over to `ideal` only if its key matches (_adopt_old), and is removed only once the new one exists."""
    profile = profile or IDEAL
    out = reference_dir(design.id, profile.name)
    ref = out / "reference.json"
    clocks = DEFAULT_CLOCKS if clocks is None else clocks
    fetched = fetch.fetch(design)
    if fetched.status != "ok":
        print(strip_control(f"{design.id}: fetch {fetched.status}: {fetched.reason}"), file=sys.stderr)
    else:
        key = sim_key(design, fetched, clocks, profile)
        done = None if force else _finished(ref, key)
        if done is None and not force and profile.ideal and not ref.exists():
            done = _adopt_old(design, key, out)
        if done is not None:
            if sensitivity and done.get("reset_determined") is None:
                _add_sensitivity(design, fetched, clocks, profile, out, ref)
            return ReferenceResult(
                "ok" if done["frames"] else "no-sync", done["mode"], done["frames_complete"]
            )
    ref.unlink(missing_ok=True)
    (out / "reference-fail.json").unlink(missing_ok=True)
    sim = simulate(design, fetched, clocks, force=force, profile=profile)
    if sim.status != "ok":
        print(f"{design.id}: see {sim.log}", file=sys.stderr)
        _record_failure(out, design, sim.status, sim.reason, sim.timed_out, sim.limited, sim.limits)
        return ReferenceResult(sim.status, None, 0, sim.reason, sim.timed_out, sim.limited)
    cut = frames.cut_frames(frames.load_dump(sim.dump, host_owned=True))
    mask, why = ui_in_mask_for(profile, sim.uio_oe_ever or 0)
    frames.write_reference(design, sim, cut, profile=profile.name, ui_in_mask_used=mask, ui_in_mask_note=why)
    if profile.ideal:
        _remove_old(out.parent)  # replaced: the plan 1 files of this design are no longer the reference
    if sensitivity:
        _add_sensitivity(design, fetched, clocks, profile, out, ref)
    return ReferenceResult("ok" if cut.frames else "no-sync", cut.mode, len(cut.frames))


def _cmd_reference(args: argparse.Namespace) -> int:
    return sandbox.command(lambda: _reference_command(args))


def _reference_command(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)  # before anything is simulated: an invalid profile stops here
    sandbox.announce()
    safefile.empty_trash_and_say(paths.WORK / "trash")
    designs = manifest.load(extras=manifest.EXTRAS_DEFAULT)
    chosen = [manifest.by_id(designs, i) for i in args.ids]
    bad = 0
    for d in chosen:
        r = make_reference(d, args.clocks, args.force, profile, args.sensitivity)
        why = f" ({r.reason})" if r.reason else ""
        print(strip_control(f"{d.id} {r.status}{why} {r.mode or '-'} {r.frames_complete} frames"))
        bad += r.status != "ok"
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

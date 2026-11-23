# SPDX-License-Identifier: Apache-2.0
"""Reference simulation: run a design under Verilator and dump its output pins on every clock.

Layout under WORK/reference/<shuttle>/<macro>/:

    scratch/      the only directory the sandbox can write: the Verilated model and the compiled testbench
                  (scratch/obj/Vtop) and the testbench's raw dumps. The host reads back only the two dumps,
                  through tthfv.safefile, and deletes the directory when the simulation is over (unless
                  TTHFV_KEEP_SCRATCH=1). Nothing else here is ever bound into the sandbox.
    uo_out.bin    one byte per clock: uo_out after that clock's rising edge (clock 0 = first reset clock)
    uio.bin       two bytes per clock: uio_out, uio_oe
    sim.log       verilator, make and simulation output
    sim.json      written last, by rename; its presence marks a finished, successful simulation

Reset stimulus: ena=1, ui_in and uio_in fixed, rst_n low for clocks 0-9 and high from clock 10.
A failed verilation or simulation is a result (SimResult.status), not an exception.
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

from tthfv import fetch, frames, manifest, paths, proc, safefile, sandbox, tools
from tthfv.errors import UserError
from tthfv.fetch import FetchResult
from tthfv.manifest import Design
from tthfv.text import strip_control

Status = Literal["ok", "verilate-fail", "sim-fail"]

DEFAULT_CLOCKS = 6_000_000
# Bump when the testbench's stimulus changes in a way the key cannot see. 2: the pad readback model.
STIMULUS_VERSION = 2
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


def _run(args: list[str], log: Path, cwd: Path, timeout: float, box: sandbox.Sandbox | None) -> proc.Logged:
    """Run a command, in the sandbox if there is one, with its output appended to sim.log, capped at
    proc.LOG_CAP bytes (the first bytes, a marker and the last bytes, so a design that prints a lot cannot
    fill the disk). Its process group is killed on a timeout (verilator spawns make and a compiler).
    Returns the exit code (proc.TIMEOUT_RC on a timeout) and the text written, so nobody has to read the
    log back."""
    return proc.run_logged(args, timeout, log, cwd=cwd, env=tools.env(), cap=proc.LOG_CAP, sandbox=box)


def _verilator_version() -> str:
    try:
        return tools.version_line(tools.tool("verilator"))
    except tools.ToolMissing:
        raise
    except UserError:
        return "unknown"


def key_sha256(key: dict) -> str:
    return hashlib.sha256(json.dumps(key, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sim_key(design: Design, fetched: FetchResult, clocks: int) -> dict:
    """Everything that decides the dump: the design's identity and sources, the stimulus, the
    simulator and its flags, the testbench and the frame cutter version. Written to sim.json and
    reference.json; a reference is reused only if the key it carries equals the current one."""
    return {
        "repo": design.repo,
        "commit": design.commit,
        "sources_sha256": fetched.sources_sha256,
        "compilation_units": [
            str(p.relative_to(fetched.src_dir) if p.is_relative_to(fetched.src_dir) else p)
            for p in fetched.synth_sources
        ],
        "top_module_used": fetched.top_module_used,
        "ui_in": design.ui_in,
        "uio_in": design.uio_in,
        "clocks": clocks,
        "verilator_flags": list(design.override.get("verilator_flags") or []),
        "base_flags": list(_BASE_FLAGS),
        "verilator": _verilator_version(),
        "testbench_sha256": hashlib.sha256(TB.read_bytes()).hexdigest(),
        "stimulus_version": STIMULUS_VERSION,
        "cutter_version": frames.CUTTER_VERSION,
    }


def _existing_dump(out: Path) -> Path | None:
    for name in ("uo_out.bin", "uo_out.bin.gz"):
        if (out / name).exists():
            return out / name
    return None


def simulate(
    design: Design, fetched: FetchResult, clocks: int = DEFAULT_CLOCKS, force: bool = False
) -> SimResult:
    """Verilate the design's own top module and dump uo_out (and uio_out/uio_oe) for `clocks` clocks.

    Without force, a finished simulation with the same clocks, inputs, top module, flags and
    source contents is reused.
    Verilator and the simulation run with the project's src directory as working directory, so
    relative .hex/.mem paths in the design resolve.
    """
    out = paths.design_dir("reference", design.id).resolve()
    dump, uio, log, stamp_path = out / "uo_out.bin", out / "uio.bin", out / "sim.log", out / "sim.json"
    scratch = out / SCRATCH
    key = sim_key(design, fetched, clocks)
    digest = key_sha256(key)

    if not force and stamp_path.exists() and (found := _existing_dump(out)) is not None:
        try:
            stamp = json.loads(stamp_path.read_text())
        except ValueError:
            stamp = {}
        if stamp.get("status") == "ok" and stamp.get("key_sha256") == digest and stamp.get("key") == key:
            return SimResult("ok", found, uio, clocks, log, sandbox=stamp.get("sandbox", ""))

    out.mkdir(parents=True, exist_ok=True)
    stamp_path.unlink(missing_ok=True)
    for stale in (dump, uio, out / "uo_out.bin.gz"):
        stale.unlink(missing_ok=True)
    safefile.remove_tree(out / "obj")  # the layout before the scratch directory
    log.write_text("")
    start = time.monotonic()

    def fail(status: Status, why: str, timed_out: bool = False) -> SimResult:
        with log.open("a") as f:
            f.write(f"{why}\n")
        for partial in (dump, uio):
            partial.unlink(missing_ok=True)
        return SimResult(status, dump, uio, 0, log, why, timed_out, sandbox.name())

    if fetched.status != "ok" or not fetched.include_dirs or not fetched.top_module_used:
        return fail("verilate-fail", f"nothing to simulate: fetch status {fetched.status!r}")
    src = fetched.include_dirs[0]
    box = sandbox.for_stage(
        rw=scratch,
        ro=[tools.suite_dir(), fetched.checkout or src, TB.parent],
        path=[tools.suite_dir() / "bin"],
    )
    safefile.make_empty_dir(scratch)
    try:
        return _simulate_in(design, fetched, clocks, key, digest, out, scratch, src, box, start, fail)
    finally:
        if os.environ.get("TTHFV_KEEP_SCRATCH") != "1":
            safefile.remove_tree(scratch)


def _simulate_in(design, fetched, clocks, key, digest, out, scratch, src, box, start, fail) -> SimResult:
    """The part of simulate() that runs third-party code, in scratch. The host reads back only the two
    dumps, through safefile, and only if they are plain files of exactly the expected size."""
    dump, uio, log, stamp_path = out / "uo_out.bin", out / "uio.bin", out / "sim.log", out / "sim.json"
    obj = scratch / "obj"
    obj.mkdir()

    top = fetched.top_module_used
    if fetched.top_module_fallback:
        with log.open("a") as f:
            f.write(f"top module {design.top_module} not declared; using {top}\n")
    includes = [f"-I{d}" for d in fetched.include_dirs]
    cmd = [
        str(tools.tool("verilator")), *_BASE_FLAGS, "--top-module", top, "-Mdir", str(obj),
        *includes, "-y", str(src), *key["verilator_flags"], str(TB), *map(str, fetched.synth_sources),
    ]  # fmt: skip
    rc, text = _run(cmd, log, src, VERILATE_TIMEOUT, box)
    if rc != 0 and "--timing" not in cmd and ("NEEDTIMINGOPT" in text or "--timing" in text):
        safefile.make_empty_dir(scratch)
        obj.mkdir()
        rc, text = _run([*cmd[:1], "--timing", *cmd[1:]], log, src, VERILATE_TIMEOUT, box)
    if rc != 0 or not (obj / "Vtop").is_file():
        if rc == proc.TIMEOUT_RC:
            return fail("verilate-fail", f"verilator timed out after {VERILATE_TIMEOUT}s", timed_out=True)
        errors = re.findall(r"^%Error.*$", text, re.MULTILINE)
        return fail("verilate-fail", errors[0] if errors else f"verilator failed (exit {rc})")
    if too_big := _scratch_too_big(scratch):
        return fail("verilate-fail", too_big)

    rc, text = _run(
        [str(obj / "Vtop"), "--clocks", str(clocks), "--ui-in", str(design.ui_in),
         "--uio-in", str(design.uio_in), "--out", str(scratch)],
        log, src, SIM_TIMEOUT, box,
    )  # fmt: skip
    if rc != 0:
        finished = re.search(r"ref_tb: design finished at clock \d+", text)
        if finished:
            return fail("sim-fail", finished.group(0))
        if rc == proc.TIMEOUT_RC:
            return fail("sim-fail", f"simulation timed out after {SIM_TIMEOUT}s", timed_out=True)
        return fail("sim-fail", f"simulation exited with code {rc}")
    if too_big := _scratch_too_big(scratch):
        return fail("sim-fail", too_big)
    try:  # the only things the host takes from the sandbox's directory
        fd = safefile.open_dir(scratch)
        try:
            safefile.copy_plain(fd, "uo_out.bin", dump, max_size=clocks, exact=clocks)
            safefile.copy_plain(fd, "uio.bin", uio, max_size=2 * clocks, exact=2 * clocks)
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
        "wall_seconds": round(time.monotonic() - start, 2),
    }
    tmp = out / "sim.json.tmp"
    tmp.write_text(json.dumps(stamp, indent=2) + "\n")
    tmp.rename(stamp_path)
    return SimResult("ok", dump, uio, clocks, log, sandbox=sandbox.name())


def _scratch_too_big(scratch: Path) -> str:
    """'' or the reason the sandbox's directory grew past safefile.MAX_SCRATCH."""
    used = safefile.tree_size(scratch)
    if used > safefile.MAX_SCRATCH:
        return (
            f"the design filled its scratch directory: {used} bytes, at most {safefile.MAX_SCRATCH} allowed"
        )
    return ""


def _finished(ref: Path, key: dict) -> dict | None:
    """The existing reference.json if it is a finished reference made with exactly this key, else None.

    Not finished: unreadable, written by an older version (no frames_complete or no key), naming a dump
    that is gone, or made with a different key (any input, the clock count, the tools, the testbench)."""
    try:
        done = json.loads(ref.read_text())
        if "frames_complete" not in done or not (ref.parent / done["dump"]).exists():
            return None
        if done.get("key") != key or done.get("key_sha256") != key_sha256(key):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return done


@dataclass(frozen=True)
class ReferenceResult:
    status: str  # "ok", "no-sync", "verilate-fail" or "sim-fail"
    mode: str | None
    frames_complete: int
    reason: str = ""  # why a failed simulation failed; empty otherwise
    timed_out: bool = False  # the simulation or Verilator timed out


def _record_failure(out: Path, design: Design, status: str, reason: str, timed_out: bool) -> None:
    """Leave reference-fail.json (by rename) so the results index can say why there is no reference
    and whether a timeout caused it. A failure is never reused: every run simulates again."""
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "reference-fail.json.tmp"
    tmp.write_text(
        json.dumps({"id": design.id, "status": status, "reason": reason, "timed_out": timed_out}, indent=2)
        + "\n"
    )
    tmp.rename(out / "reference-fail.json")


def make_reference(design: Design, clocks: int | None = None, force: bool = False) -> ReferenceResult:
    """Fetch, simulate, cut and write one design's reference; the whole `tthfv reference` pipeline.

    Without force, a finished reference.json made with the same simulation key is reused. A failed simulation
    is a result, recorded in reference-fail.json; it is not an exception."""
    out = paths.design_dir("reference", design.id)
    ref = out / "reference.json"
    clocks = DEFAULT_CLOCKS if clocks is None else clocks
    fetched = fetch.fetch(design)
    if fetched.status != "ok":
        print(strip_control(f"{design.id}: fetch {fetched.status}: {fetched.reason}"), file=sys.stderr)
    elif not force and (done := _finished(ref, sim_key(design, fetched, clocks))) is not None:
        return ReferenceResult("ok" if done["frames"] else "no-sync", done["mode"], done["frames_complete"])
    ref.unlink(missing_ok=True)
    (out / "reference-fail.json").unlink(missing_ok=True)
    sim = simulate(design, fetched, clocks, force=force)
    if sim.status != "ok":
        print(f"{design.id}: see {sim.log}", file=sys.stderr)
        _record_failure(out, design, sim.status, sim.reason, sim.timed_out)
        return ReferenceResult(sim.status, None, 0, sim.reason, sim.timed_out)
    cut = frames.cut_frames(frames.load_dump(sim.dump))
    frames.write_reference(design, sim, cut)
    return ReferenceResult("ok" if cut.frames else "no-sync", cut.mode, len(cut.frames))


def _cmd_reference(args: argparse.Namespace) -> int:
    sandbox.announce()
    designs = manifest.load(extras=manifest.EXTRAS_DEFAULT)
    chosen = [manifest.by_id(designs, i) for i in args.ids]
    bad = 0
    for d in chosen:
        r = make_reference(d, args.clocks, args.force)
        why = f" ({r.reason})" if r.reason else ""
        print(strip_control(f"{d.id} {r.status}{why} {r.mode or '-'} {r.frames_complete} frames"))
        bad += r.status != "ok"
    return 1 if bad else 0


def add_parser(sub) -> None:
    p = sub.add_parser("reference", help="simulate designs and cut the output into reference frames")
    p.add_argument("ids", nargs="+", metavar="id", help="<shuttle>/<macro>")
    p.add_argument("--clocks", type=int, default=None, help=f"clocks to simulate (default {DEFAULT_CLOCKS})")
    p.add_argument("--force", action="store_true", help="simulate and cut again even if already done")
    p.set_defaults(func=_cmd_reference)

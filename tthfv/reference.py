# SPDX-License-Identifier: Apache-2.0
"""Reference simulation: run a design under Verilator and dump its output pins on every clock.

Layout under WORK/reference/<shuttle>/<macro>/:

    obj/          the Verilated model and the compiled testbench (obj/Vtop)
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
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tthfv import fetch, frames, manifest, paths, proc, tools
from tthfv.errors import UserError
from tthfv.fetch import FetchResult
from tthfv.manifest import Design

Status = Literal["ok", "verilate-fail", "sim-fail"]

DEFAULT_CLOCKS = 6_000_000
TB = Path(__file__).resolve().parent / "harness" / "ref_tb.cpp"
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


def _run(args: list[str], log: Path, cwd: Path, timeout: float) -> proc.Logged:
    """Run a command with its output appended to sim.log, capped at proc.LOG_CAP bytes (the first bytes,
    a marker and the last bytes, so a design that prints a lot cannot fill the disk). Its process group is
    killed on a timeout (verilator spawns make and a compiler). Returns the exit code (proc.TIMEOUT_RC on
    a timeout) and the text written, so nobody has to read the log back."""
    return proc.run_logged(args, timeout, log, cwd=cwd, env=tools.env(), cap=proc.LOG_CAP)


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
    key = sim_key(design, fetched, clocks)
    digest = key_sha256(key)

    if not force and stamp_path.exists() and (found := _existing_dump(out)) is not None:
        try:
            stamp = json.loads(stamp_path.read_text())
        except ValueError:
            stamp = {}
        if stamp.get("status") == "ok" and stamp.get("key_sha256") == digest and stamp.get("key") == key:
            return SimResult("ok", found, uio, clocks, log)

    out.mkdir(parents=True, exist_ok=True)
    stamp_path.unlink(missing_ok=True)
    for stale in (dump, uio, out / "uo_out.bin.gz"):
        stale.unlink(missing_ok=True)
    log.write_text("")
    start = time.monotonic()

    def fail(status: Status, why: str) -> SimResult:
        with log.open("a") as f:
            f.write(f"{why}\n")
        for partial in (dump, uio):
            partial.unlink(missing_ok=True)
        return SimResult(status, dump, uio, 0, log, why)

    if fetched.status != "ok" or not fetched.include_dirs or not fetched.top_module_used:
        return fail("verilate-fail", f"nothing to simulate: fetch status {fetched.status!r}")
    src = fetched.include_dirs[0]
    obj = out / "obj"
    shutil.rmtree(obj, ignore_errors=True)
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
    rc, text = _run(cmd, log, src, VERILATE_TIMEOUT)
    if rc != 0 and "--timing" not in cmd and ("NEEDTIMINGOPT" in text or "--timing" in text):
        shutil.rmtree(obj)
        obj.mkdir()
        rc, text = _run([*cmd[:1], "--timing", *cmd[1:]], log, src, VERILATE_TIMEOUT)
    if rc != 0 or not (obj / "Vtop").exists():
        if rc == proc.TIMEOUT_RC:
            return fail("verilate-fail", f"verilator timed out after {VERILATE_TIMEOUT}s")
        errors = re.findall(r"^%Error.*$", text, re.MULTILINE)
        return fail("verilate-fail", errors[0] if errors else f"verilator failed (exit {rc})")

    rc, text = _run(
        [str(obj / "Vtop"), "--clocks", str(clocks), "--ui-in", str(design.ui_in),
         "--uio-in", str(design.uio_in), "--out", str(out)],
        log, src, SIM_TIMEOUT,
    )  # fmt: skip
    short = (
        not (dump.exists() and uio.exists())
        or dump.stat().st_size != clocks
        or uio.stat().st_size != 2 * clocks
    )
    if rc != 0 or short:
        finished = re.search(r"ref_tb: design finished at clock \d+", text)
        if finished:
            return fail("sim-fail", finished.group(0))
        if rc == proc.TIMEOUT_RC:
            return fail("sim-fail", f"simulation timed out after {SIM_TIMEOUT}s")
        if rc != 0:
            return fail("sim-fail", f"simulation exited with code {rc}")
        return fail("sim-fail", "simulation wrote a short dump")

    stamp = {
        "status": "ok",
        "key": key,
        "key_sha256": digest,
        "reason": "",
        "wall_seconds": round(time.monotonic() - start, 2),
    }
    tmp = out / "sim.json.tmp"
    tmp.write_text(json.dumps(stamp, indent=2) + "\n")
    tmp.rename(stamp_path)
    return SimResult("ok", dump, uio, clocks, log)


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


def _record_failure(out: Path, design: Design, status: str, reason: str) -> None:
    """Leave reference-fail.json (by rename) so the results index can say why there is no reference."""
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "reference-fail.json.tmp"
    tmp.write_text(json.dumps({"id": design.id, "status": status, "reason": reason}, indent=2) + "\n")
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
        print(f"{design.id}: fetch {fetched.status}: {fetched.reason}", file=sys.stderr)
    elif not force and (done := _finished(ref, sim_key(design, fetched, clocks))) is not None:
        return ReferenceResult("ok" if done["frames"] else "no-sync", done["mode"], done["frames_complete"])
    ref.unlink(missing_ok=True)
    (out / "reference-fail.json").unlink(missing_ok=True)
    sim = simulate(design, fetched, clocks, force=force)
    if sim.status != "ok":
        print(f"{design.id}: see {sim.log}", file=sys.stderr)
        _record_failure(out, design, sim.status, sim.reason)
        return ReferenceResult(sim.status, None, 0, sim.reason)
    cut = frames.cut_frames(frames.load_dump(sim.dump))
    frames.write_reference(design, sim, cut)
    return ReferenceResult("ok" if cut.frames else "no-sync", cut.mode, len(cut.frames))


def _cmd_reference(args: argparse.Namespace) -> int:
    designs = manifest.load(extras=manifest.EXTRAS_DEFAULT)
    chosen = [manifest.by_id(designs, i) for i in args.ids]
    bad = 0
    for d in chosen:
        r = make_reference(d, args.clocks, args.force)
        why = f" ({r.reason})" if r.reason else ""
        print(f"{d.id} {r.status}{why} {r.mode or '-'} {r.frames_complete} frames")
        bad += r.status != "ok"
    return 1 if bad else 0


def add_parser(sub) -> None:
    p = sub.add_parser("reference", help="simulate designs and cut the output into reference frames")
    p.add_argument("ids", nargs="+", metavar="id", help="<shuttle>/<macro>")
    p.add_argument("--clocks", type=int, default=None, help=f"clocks to simulate (default {DEFAULT_CLOCKS})")
    p.add_argument("--force", action="store_true", help="simulate and cut again even if already done")
    p.set_defaults(func=_cmd_reference)

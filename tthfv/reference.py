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
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tthfv import fetch, frames, manifest, paths, tools
from tthfv.fetch import FetchResult
from tthfv.manifest import Design

Status = Literal["ok", "verilate-fail", "sim-fail"]

DEFAULT_CLOCKS = 6_000_000
TB = Path(__file__).resolve().parent / "harness" / "ref_tb.cpp"
_VERILATE_TIMEOUT = 1800
_SIM_TIMEOUT = 3600

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


def _top_module(design: Design, fetched: FetchResult, log: Path) -> str:
    """Re-hardened projects are sometimes renamed on the shuttle while the repo declares the original
    name: when top_module is not declared and exactly one tt_um_ module is, use that one."""
    declared: set[str] = set()
    for s in fetched.sources:
        if s.suffix in (".v", ".sv"):
            declared.update(
                re.findall(r"^\s*module\s+(tt_um_\w+)", s.read_text(errors="replace"), re.MULTILINE)
            )
    if design.top_module not in declared and len(declared) == 1:
        top = next(iter(declared))
        with log.open("a") as f:
            f.write(f"top module {design.top_module} not declared; using {top}\n")
        return top
    return design.top_module


def _run(args: list[str], log: Path, cwd: Path, timeout: int) -> int:
    """Run a command with output appended to the log; returns the exit code (124 on timeout).

    The command gets its own process group, which is killed on timeout (verilator spawns make and a
    compiler)."""
    with log.open("a") as f:
        f.write(f"$ {' '.join(args)}\n")
        f.flush()
        proc = subprocess.Popen(
            args, cwd=cwd, env=tools.env(), stdout=f, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            f.write(f"timed out after {timeout}s\n")
            return 124


def _verilator_version() -> str:
    try:
        done = subprocess.run(
            [str(tools.tool("verilator")), "--version"],
            env=tools.env(), capture_output=True, text=True, check=False, timeout=60,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return "unknown"
    lines = (done.stdout.strip() or done.stderr.strip()).splitlines()
    return lines[0] if done.returncode == 0 and lines else "unknown"


def _fingerprint(design: Design, fetched: FetchResult) -> str:
    """sha256 over everything besides the inputs that decides the dump: the repo and commit, each source
    file's name and contents, the testbench and the base flags."""
    h = hashlib.sha256()
    for part in (design.repo, design.commit, json.dumps(_BASE_FLAGS)):
        h.update(part.encode() + b"\0")
    for s in fetched.sources:
        h.update(s.name.encode() + b"\0")
        h.update(s.read_bytes() if s.exists() else b"<missing>")
        h.update(b"\0")
    h.update(TB.read_bytes())
    return h.hexdigest()


def _key(design: Design, fetched: FetchResult, clocks: int) -> dict:
    return {
        "fingerprint": _fingerprint(design, fetched),
        "clocks": clocks,
        "ui_in": design.ui_in,
        "uio_in": design.uio_in,
        "top_module": design.top_module,
        "verilator_flags": list(design.override.get("verilator_flags") or []),
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
    key = _key(design, fetched, clocks)

    if not force and stamp_path.exists() and (found := _existing_dump(out)) is not None:
        try:
            stamp = json.loads(stamp_path.read_text())
        except ValueError:
            stamp = {}
        if stamp.get("status") == "ok" and all(stamp.get(k) == v for k, v in key.items()):
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

    if fetched.status != "ok" or not fetched.include_dirs:
        return fail("verilate-fail", f"nothing to simulate: fetch status {fetched.status!r}")
    src = fetched.include_dirs[0]
    obj = out / "obj"
    shutil.rmtree(obj, ignore_errors=True)
    obj.mkdir()

    top = _top_module(design, fetched, log)
    sources = [str(p) for p in fetched.sources if p.suffix not in (".vh", ".svh", ".h")]
    cmd = [
        str(tools.tool("verilator")), *_BASE_FLAGS, "--top-module", top, "-Mdir", str(obj),
        "-I" + str(src), "-y", str(src), *key["verilator_flags"], str(TB), *sources,
    ]  # fmt: skip
    rc = _run(cmd, log, src, _VERILATE_TIMEOUT)
    text = log.read_text()
    if rc != 0 and "--timing" not in cmd and ("NEEDTIMINGOPT" in text or "--timing" in text):
        shutil.rmtree(obj)
        obj.mkdir()
        rc = _run([*cmd[:1], "--timing", *cmd[1:]], log, src, _VERILATE_TIMEOUT)
    if rc != 0 or not (obj / "Vtop").exists():
        if rc == 124:
            return fail("verilate-fail", f"verilator timed out after {_VERILATE_TIMEOUT}s")
        errors = re.findall(r"^%Error.*$", log.read_text(), re.MULTILINE)
        return fail("verilate-fail", errors[0] if errors else f"verilator failed (exit {rc})")

    log_mark = len(log.read_text())
    rc = _run(
        [str(obj / "Vtop"), "--clocks", str(clocks), "--ui-in", str(design.ui_in),
         "--uio-in", str(design.uio_in), "--out", str(out)],
        log, src, _SIM_TIMEOUT,
    )  # fmt: skip
    short = (
        not (dump.exists() and uio.exists())
        or dump.stat().st_size != clocks
        or uio.stat().st_size != 2 * clocks
    )
    if rc != 0 or short:
        finished = re.search(r"ref_tb: design finished at clock \d+", log.read_text()[log_mark:])
        if finished:
            return fail("sim-fail", finished.group(0))
        if rc == 124:
            return fail("sim-fail", f"simulation timed out after {_SIM_TIMEOUT}s")
        if rc != 0:
            return fail("sim-fail", f"simulation exited with code {rc}")
        return fail("sim-fail", "simulation wrote a short dump")

    stamp = {
        "status": "ok",
        **key,
        "reason": "",
        "verilator": _verilator_version(),
        "wall_seconds": round(time.monotonic() - start, 2),
    }
    tmp = out / "sim.json.tmp"
    tmp.write_text(json.dumps(stamp, indent=2) + "\n")
    tmp.rename(stamp_path)
    return SimResult("ok", dump, uio, clocks, log)


def _finished(ref: Path, clocks: int | None) -> dict | None:
    """The existing reference.json if it is a finished reference for these clocks, else None.

    Not finished: unreadable, written by an older version (no frames_complete), naming a dump that is gone,
    or made for a different number of clocks than the one asked for.
    """
    try:
        done = json.loads(ref.read_text())
        if "frames_complete" not in done or not (ref.parent / done["dump"]).exists():
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return done if clocks is None or done.get("clocks") == clocks else None


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

    Without force, a finished reference.json for the same clocks is reused. A failed simulation
    is a result, recorded in reference-fail.json; it is not an exception."""
    out = paths.design_dir("reference", design.id)
    ref = out / "reference.json"
    if not force and (done := _finished(ref, clocks)) is not None:
        return ReferenceResult("ok" if done["frames"] else "no-sync", done["mode"], done["frames_complete"])
    ref.unlink(missing_ok=True)
    (out / "reference-fail.json").unlink(missing_ok=True)
    fetched = fetch.fetch(design)
    if fetched.status != "ok":
        print(f"{design.id}: fetch {fetched.status}: {fetched.reason}", file=sys.stderr)
    sim = simulate(design, fetched, clocks or DEFAULT_CLOCKS, force=force)
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

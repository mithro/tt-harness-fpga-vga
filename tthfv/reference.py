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

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tthfv import paths, tools
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
    """Run a command with output appended to the log; returns the exit code (124 on timeout)."""
    with log.open("a") as f:
        f.write(f"$ {' '.join(args)}\n")
        f.flush()
        try:
            return subprocess.run(
                args, cwd=cwd, env=tools.env(), stdout=f, stderr=subprocess.STDOUT,
                timeout=timeout, check=False,
            ).returncode  # fmt: skip
        except subprocess.TimeoutExpired:
            f.write(f"timed out after {timeout}s\n")
            return 124


def _verilator_version() -> str:
    done = subprocess.run(
        [str(tools.tool("verilator")), "--version"],
        env=tools.env(),
        capture_output=True,
        text=True,
        check=False,
    )
    return (done.stdout.strip() or done.stderr.strip()).splitlines()[0] if done.returncode == 0 else "unknown"


def _key(design: Design, clocks: int) -> dict:
    return {
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

    Without force, a finished simulation with the same clocks, inputs, top module and flags is reused.
    Verilator and the simulation run with the project's src directory as working directory, so
    relative .hex/.mem paths in the design resolve.
    """
    out = paths.design_dir("reference", design.id).resolve()
    dump, uio, log, stamp_path = out / "uo_out.bin", out / "uio.bin", out / "sim.log", out / "sim.json"
    key = _key(design, clocks)

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
        return SimResult(status, dump, uio, 0, log)

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
        return fail("verilate-fail", f"verilator failed (exit {rc})")

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
        return fail("sim-fail", f"simulation failed (exit {rc}) or wrote a short dump")

    stamp = {
        "status": "ok",
        **key,
        "verilator": _verilator_version(),
        "wall_seconds": round(time.monotonic() - start, 2),
    }
    tmp = out / "sim.json.tmp"
    tmp.write_text(json.dumps(stamp, indent=2) + "\n")
    tmp.rename(stamp_path)
    return SimResult("ok", dump, uio, clocks, log)

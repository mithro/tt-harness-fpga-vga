# SPDX-License-Identifier: Apache-2.0
"""Build stage: turn a fetched design into an iCE40UP5K bitstream with yosys, nextpnr and icepack.

A failed build is a result, not an exception: build() returns a BuildResult whose status says what
went wrong, and the same record is written to WORK/build/<id>/build.json (by rename, so the file is
either absent or complete). Files in that directory:

    tt_fpga_top.v   the harness wrapper with the design's top module substituted in
    yosys.log, nextpnr.log, icepack.log, sv2v.log   each tool's full output
    out.json, out.asc, design.bin   the netlist, placed-and-routed design and bitstream
    build.json      written last
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from tthfv import fetch, manifest, paths, reports, tools
from tthfv.errors import UserError
from tthfv.fetch import FetchResult
from tthfv.manifest import Design

Status = Literal["ok", "synth-fail", "no-fit", "timing-fail", "pack-fail"]

HARNESS = Path(__file__).parent / "harness"
PLACEHOLDER = "__tt_um_placeholder"
PREAMBLE = b"\x7e\xaa\x99\x7e"
BITSTREAM = "design.bin"
LC_TOTAL_KEY = "ICESTORM_LC"
_STAMP_VERSION = 1
_YOSYS_TIMEOUT = 1800
_NEXTPNR_TIMEOUT = 3600


@dataclass(frozen=True)
class BuildResult:
    id: str
    status: Status
    reason: str = ""
    bitstream: str | None = None  # file name inside the build directory
    bitstream_sha256: str | None = None
    clock_hz: int = 0
    fmax_hz: int | None = None
    cells: dict[str, int] = field(default_factory=dict)
    lut_percent: float | None = None
    tools: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0
    sv2v: bool = False  # True if synthesis only worked after converting the sources with sv2v
    inputs_sha256: str = ""  # digest of everything that decides the result; used to reuse a build


def _tool_versions() -> dict[str, str]:
    out = {}
    for name in ("yosys", "nextpnr-ice40"):
        done = subprocess.run(
            [str(tools.tool(name)), "--version"], env=tools.env(), capture_output=True, text=True, check=False
        )
        out[name] = (
            (done.stdout.strip() or done.stderr.strip()).splitlines()[0] if done.returncode == 0 else ""
        )
        if not out[name]:
            raise UserError(f"{name} --version failed: {(done.stdout + done.stderr).strip()}")
    return out


def _inputs_digest(design: Design, fetched: FetchResult, wrapper: str, versions: dict[str, str]) -> str:
    h = hashlib.sha256()
    meta = {"v": _STAMP_VERSION, "top": design.top_module, "clock_hz": design.clock_hz, "tools": versions}
    h.update(json.dumps(meta, sort_keys=True).encode())
    h.update(wrapper.encode())
    h.update((HARNESS / "tt_fpga_fabricfox.pcf").read_bytes())
    for src in fetched.sources:
        h.update(
            str(src.relative_to(fetched.src_dir) if src.is_relative_to(fetched.src_dir) else src).encode()
        )
        h.update(src.read_bytes())
    return h.hexdigest()


def _run(args: list[str], log: Path, cwd: Path, timeout: int) -> tuple[int, str]:
    """Run a tool with the pinned toolchain on PATH, writing its output to log. Returns (rc, output)."""
    try:
        with log.open("w") as f:
            done = subprocess.run(
                args,
                cwd=cwd,
                env=tools.env(),
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        rc = done.returncode
    except subprocess.TimeoutExpired:
        with log.open("a") as f:
            f.write(f"\ntimed out after {timeout}s\n")
        rc = -1
    return rc, log.read_text(errors="replace")


def _why(output: str, rc: int, tool: str) -> str:
    """The most telling line of a failed tool's output: the last ERROR line, else the last line."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    errors = [ln for ln in lines if "ERROR" in ln]
    detail = (errors or lines or ["no output"])[-1]
    return f"{tool} exit status {rc}: {detail}"


_UNSAFE = re.compile(r'["\\;#`\n\r]')


def _unsafe_paths(*groups) -> str:
    """Return a reason naming the first path that cannot be put safely in a yosys script, or ''.
    File names come from third-party repositories, and yosys reads quotes, ';', '#' and backticks."""
    for group in groups:
        for p in group:
            if _UNSAFE.search(str(p)):
                return f'unsafe character in path {str(p)!r}: script paths may not contain " \\ ; # ` or newlines'
    return ""


def _yosys_script(files: list[Path], include_dirs: list[Path], out_json: Path, sv: bool) -> str:
    inc = " ".join(f'-I "{d}"' for d in include_dirs)
    reads = [
        f'read_verilog {"-sv " if sv and f.suffix == ".sv" else ""}{inc} "{f}"'.replace("  ", " ")
        for f in files
    ]
    return "\n".join([*reads, f'synth_ice40 -top tt_fpga_top -json "{out_json}"']) + "\n"


def _run_yosys(script: str, out: Path, cwd: Path) -> tuple[int, str]:
    script_path = out / "yosys.ys"
    script_path.write_text(script)
    return _run([str(tools.tool("yosys")), "-s", str(script_path)], out / "yosys.log", cwd, _YOSYS_TIMEOUT)


def _synthesise(
    design: Design, fetched: FetchResult, out: Path, wrapper_path: Path
) -> tuple[bool, str, bool]:
    """Run yosys, retrying once through sv2v if it fails and sv2v is installed.
    Returns (succeeded, reason, used_sv2v)."""
    cwd = fetched.include_dirs[0] if fetched.include_dirs else fetched.sources[0].parent
    incs = list(fetched.include_dirs)
    out_json = out / "out.json"
    script = _yosys_script([wrapper_path, *fetched.sources], incs, out_json, sv=True)
    rc, text = _run_yosys(script, out, cwd)
    if rc == 0:
        return True, "", False
    reason = _why(text, rc, "yosys")
    sv2v = shutil.which("sv2v", path=tools.env()["PATH"])
    if not sv2v:
        return False, reason, False
    converted = out / "sv2v.v"
    with converted.open("w") as f, (out / "sv2v.log").open("w") as errf:
        done = subprocess.run(
            [sv2v, *[f"-I{d}" for d in incs], *map(str, fetched.sources)],
            cwd=cwd, env=tools.env(), stdout=f, stderr=errf, check=False,
        )  # fmt: skip
    if done.returncode != 0:
        return (
            False,
            f"{reason}; sv2v retry failed: {_why((out / 'sv2v.log').read_text(), done.returncode, 'sv2v')}",
            False,
        )
    script = _yosys_script([wrapper_path, converted], incs, out_json, sv=False)
    rc, text = _run_yosys(script, out, cwd)
    if rc == 0:
        return True, "", True
    return False, f"{reason}; after sv2v: {_why(text, rc, 'yosys')}", False


def _write_result(out: Path, result: BuildResult) -> BuildResult:
    tmp = out / "build.json.tmp"
    tmp.write_text(json.dumps(asdict(result), indent=2) + "\n")
    tmp.rename(out / "build.json")
    return result


def _cached(out: Path, digest: str) -> BuildResult | None:
    path = out / "build.json"
    if not path.exists():
        return None
    try:
        result = BuildResult(**json.loads(path.read_text()))
    except (ValueError, TypeError):
        return None
    if result.inputs_sha256 != digest:
        return None
    if result.bitstream and not (out / result.bitstream).exists():
        return None
    return result


def build(design: Design, fetched: FetchResult, force: bool = False) -> BuildResult:
    """Synthesise, place, route and pack design into WORK/build/<id>/design.bin.

    Without force, a finished build of identical sources, clock and tools is reused.
    """
    if fetched.status != "ok":
        raise UserError(f"{design.id}: not fetched ({fetched.reason or fetched.status})")
    started = time.monotonic()
    out = paths.design_dir("build", design.id).resolve()
    out.mkdir(parents=True, exist_ok=True)
    wrapper = (HARNESS / "tt_fpga_top.v").read_text().replace(PLACEHOLDER, design.top_module)
    versions = _tool_versions()
    digest = _inputs_digest(design, fetched, wrapper, versions)
    if not force and (hit := _cached(out, digest)):
        return hit
    (out / "build.json").unlink(missing_ok=True)  # a crash from here on must not leave a stale ok
    wrapper_path = out / "tt_fpga_top.v"
    wrapper_path.write_text(wrapper)

    base = {"id": design.id, "clock_hz": design.clock_hz, "tools": versions, "inputs_sha256": digest}

    def finish(status: Status, reason: str = "", **kw) -> BuildResult:
        seconds = round(time.monotonic() - started, 1)
        return _write_result(out, BuildResult(status=status, reason=reason, seconds=seconds, **base, **kw))

    unsafe = _unsafe_paths(
        [wrapper_path, out / "out.json", out / "sv2v.v"], fetched.sources, fetched.include_dirs
    )
    if unsafe:
        return finish("synth-fail", unsafe)
    ok, reason, used_sv2v = _synthesise(design, fetched, out, wrapper_path)
    if not ok:
        return finish("synth-fail", reason)
    cells = reports.parse_yosys((out / "yosys.log").read_text(errors="replace"))

    pnr = [
        str(tools.tool("nextpnr-ice40")), "--up5k", "--package", "sg48",
        "--pcf", str(HARNESS / "tt_fpga_fabricfox.pcf"), "--pcf-allow-unconstrained",
        "--freq", f"{design.clock_hz / 1e6:g}", "--timing-allow-fail", "--seed", "1",
        "--json", str(out / "out.json"), "--asc", str(out / "out.asc"),
    ]  # fmt: skip
    rc, text = _run(pnr, out / "nextpnr.log", out, _NEXTPNR_TIMEOUT)
    report = reports.parse_nextpnr(text)
    lc = report.utilisation.get(LC_TOTAL_KEY)
    lut_percent = round(100 * lc[0] / lc[1], 1) if lc else None
    known = {"cells": cells, "lut_percent": lut_percent, "fmax_hz": report.fmax_hz, "sv2v": used_sv2v}
    if rc != 0 or not report.fit or not (out / "out.asc").exists():
        return finish("no-fit", _why(text, rc, "nextpnr-ice40"), **known)

    bin_path = out / BITSTREAM
    bin_path.unlink(missing_ok=True)
    rc, text = _run(
        [str(tools.tool("icepack")), str(out / "out.asc"), str(bin_path)], out / "icepack.log", out, 600
    )
    if rc != 0:
        return finish("pack-fail", _why(text, rc, "icepack"), **known)
    data = bin_path.read_bytes()
    if PREAMBLE not in data[:64]:
        bin_path.unlink()
        return finish(
            "pack-fail", "bitstream has no iCE40 preamble 7E AA 99 7E in its first 64 bytes", **known
        )
    known |= {"bitstream": BITSTREAM, "bitstream_sha256": hashlib.sha256(data).hexdigest()}

    if report.fmax_hz is None:
        return finish("timing-fail", "nextpnr reported no timing for clk", **known)
    if report.fmax_hz < design.clock_hz:
        reason = f"fmax {report.fmax_hz / 1e6:.2f} MHz is below the {design.clock_hz / 1e6:.3f} MHz clock"
        return finish("timing-fail", reason, **known)
    return finish("ok", **known)


def _line(design: Design, r: BuildResult) -> str:
    lut = "-" if r.lut_percent is None else f"{r.lut_percent:.1f}"
    fmax = "-" if r.fmax_hz is None else f"{r.fmax_hz / 1e6:.2f}"
    return f"{design.id} {r.status} {lut}% fmax {fmax}/{design.clock_hz / 1e6:.3f} {r.reason}".rstrip()


def _cmd_build(args: argparse.Namespace) -> int:
    designs = manifest.load(extras=manifest.EXTRAS_DEFAULT)
    chosen = [manifest.by_id(designs, i) for i in args.ids]
    failed = 0
    for d in chosen:
        fetched = fetch.fetch(d)
        if fetched.status != "ok":
            print(f"{d.id} {fetched.status} -% fmax -/{d.clock_hz / 1e6:.3f} {fetched.reason}")
            failed += 1
            continue
        r = build(d, fetched, force=args.force)
        print(_line(d, r), flush=True)
        failed += r.status != "ok"
    return 1 if failed else 0


def add_parser(sub) -> None:
    p = sub.add_parser("build", help="build bitstreams with yosys, nextpnr-ice40 and icepack")
    p.add_argument("ids", nargs="+", metavar="id", help="<shuttle>/<macro>; fetched first if needed")
    p.add_argument("--force", action="store_true", help="build again even if inputs are unchanged")
    p.set_defaults(func=_cmd_build)

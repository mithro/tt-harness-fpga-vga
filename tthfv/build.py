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

    tt_fpga_top.v                    the harness wrapper with the design's top module substituted in
    yosys-<synth>.ys, .log, out-<synth>.json    synthesis script, output and netlist
    nextpnr-<synth>-<seed>.log       one per attempt
    out.asc, design.bin              the kept attempt's placed-and-routed design and bitstream
    build.json                       written last
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

Status = Literal["ok", "synth-fail", "no-fit", "pnr-fail", "timing-fail", "pack-fail"]

HARNESS = Path(__file__).parent / "harness"
PLACEHOLDER = "__tt_um_placeholder"
PREAMBLE = b"\x7e\xaa\x99\x7e"
BITSTREAM = "design.bin"
LC_TOTAL_KEY = "ICESTORM_LC"
_STAMP_VERSION = 2
_YOSYS_TIMEOUT = 1800
_NEXTPNR_TIMEOUT = 3600

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
    fmax_hz: int | None = None  # of the kept attempt
    cells: dict[str, int] = field(default_factory=dict)
    lut_percent: float | None = None  # logic-cell (ICESTORM_LC) utilisation of the 5280 on the UP5K
    tools: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0
    sv2v: bool = False  # True if synthesis only worked after converting the sources with sv2v
    sv_reader: bool = False  # True if synthesis only worked reading every file with read_verilog -sv
    inputs_sha256: str = ""  # digest of everything that decides the result; used to reuse a build
    recipe: dict | None = None  # {"synth", "seed"} of the kept attempt
    attempts: list[dict] = field(default_factory=list)  # {"synth", "seed", "status", "fmax_hz"} each


def _version_line(exe: str) -> str:
    done = subprocess.run([exe, "--version"], env=tools.env(), capture_output=True, text=True, check=False)
    first = (done.stdout.strip() or done.stderr.strip()).splitlines()
    if done.returncode != 0 or not first:
        raise UserError(f"{exe} --version failed: {(done.stdout + done.stderr).strip()}")
    return first[0]


def _sv2v() -> str | None:
    return shutil.which("sv2v", path=tools.env()["PATH"])


def _tool_versions() -> dict[str, str]:
    out = {name: _version_line(str(tools.tool(name))) for name in ("yosys", "nextpnr-ice40")}
    out["icepack"] = tools.icepack_version()
    if sv2v := _sv2v():
        out["sv2v"] = _version_line(sv2v)
    return out


def _synth_command(synth: str) -> list[str]:
    return ["synth_ice40", "-top", "tt_fpga_top", *SYNTH_FLAGS[synth]]


def _pnr_flags(clock_hz: int, seed: int) -> list[str]:
    """nextpnr arguments that do not name files."""
    return [
        "--up5k", "--package", "sg48", "--pcf-allow-unconstrained",
        "--freq", f"{clock_hz / 1e6:g}", "--timing-allow-fail", "--seed", str(seed),
    ]  # fmt: skip


def _inputs_digest(
    design: Design, fetched: FetchResult, wrapper: str, versions: dict[str, str], max_attempts: int
) -> str:
    """Everything that decides the result: sources and the whole src tree, the wrapper, the pcf,
    tool versions, and every command line (yosys synthesis commands, nextpnr flags, recipes)."""
    h = hashlib.sha256()
    meta = {
        "v": _STAMP_VERSION,
        "top": design.top_module,
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
    # yosys also reads files by relative path ($readmemh) and by `include, so hash the whole src tree.
    for root in fetched.include_dirs:
        for f in sorted(
            p for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts
        ):
            h.update(b"tree:" + str(f.relative_to(root)).encode() + b"\0")
            h.update(f.read_bytes())
    for src in fetched.sources:
        h.update(
            str(src.relative_to(fetched.src_dir) if src.is_relative_to(fetched.src_dir) else src).encode()
        )
        h.update(src.read_bytes())
    return h.hexdigest()


def _run(args: list[str], log: Path, cwd: Path, timeout: int) -> tuple[int, str]:
    """Run a tool with the pinned toolchain on PATH, writing its output to log. Returns (rc, output);
    rc is -1 if the tool timed out."""
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
    if rc == -1:
        return f"{tool} timed out"
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


def _yosys_script(
    files: list[Path], include_dirs: list[Path], out_json: Path, sv: bool, synth: str, all_sv: bool = False
) -> str:
    """sv: read .sv files with -sv. all_sv: read every file with -sv, as the Tiny Tapeout hardening
    flow does (some designs use SystemVerilog constructs such as `>>=` in plain .v files)."""
    inc = " ".join(f'-I "{d}"' for d in include_dirs)
    reads = [
        f'read_verilog {"-sv " if all_sv or (sv and f.suffix == ".sv") else ""}{inc} "{f}"'.replace("  ", " ")
        for f in files
    ]
    synth_line = " ".join(_synth_command(synth)) + f' -json "{out_json}"'
    return "\n".join([*reads, synth_line]) + "\n"


def _run_yosys(script: str, out: Path, synth: str, cwd: Path) -> tuple[int, str]:
    script_path = out / f"yosys-{synth}.ys"
    script_path.write_text(script)
    return _run(
        [str(tools.tool("yosys")), "-s", str(script_path)], out / f"yosys-{synth}.log", cwd, _YOSYS_TIMEOUT
    )


def _convert_with_sv2v(sv2v: str, fetched: FetchResult, out: Path, cwd: Path) -> str:
    """Convert all sources to plain Verilog in out/sv2v.v. Returns '' or the failure reason."""
    incs = [f"-I{d}" for d in fetched.include_dirs]
    with (out / "sv2v.v").open("w") as f, (out / "sv2v.log").open("w") as errf:
        done = subprocess.run(
            [sv2v, *incs, *map(str, fetched.sources)],
            cwd=cwd, env=tools.env(), stdout=f, stderr=errf, check=False,
        )  # fmt: skip
    if done.returncode != 0:
        return _why((out / "sv2v.log").read_text(errors="replace"), done.returncode, "sv2v")
    return ""


def _synthesise(
    fetched: FetchResult, out: Path, wrapper_path: Path, synth: str, reader: str
) -> tuple[bool, str, str]:
    """Run yosys for one synthesis variant. The reader is how the sources are read: "" (.v plain,
    .sv with -sv), "sv" (every file with -sv) or "sv2v" (converted). If the first run fails, retry
    once with every file read with -sv, then, if sv2v is installed, once through sv2v. A reader that
    worked is kept for later variants. Returns (succeeded, reason, reader)."""
    cwd = fetched.include_dirs[0] if fetched.include_dirs else fetched.sources[0].parent
    incs = list(fetched.include_dirs)
    out_json = out / f"out-{synth}.json"
    converted = out / "sv2v.v"

    def attempt(how: str) -> tuple[int, str]:
        if how == "sv2v":
            script = _yosys_script([wrapper_path, converted], incs, out_json, sv=False, synth=synth)
        else:
            script = _yosys_script(
                [wrapper_path, *fetched.sources], incs, out_json, sv=True, synth=synth, all_sv=how == "sv"
            )
        return _run_yosys(script, out, synth, cwd)

    if reader:
        rc, text = attempt(reader)
        return rc == 0, "" if rc == 0 else _why(text, rc, "yosys"), reader
    rc, text = attempt("")
    if rc == 0:
        return True, "", ""
    reason = _why(text, rc, "yosys")
    if any(f.suffix == ".v" for f in fetched.sources):
        rc, text = attempt("sv")
        if rc == 0:
            return True, "", "sv"
        reason = f"{reason}; with -sv: {_why(text, rc, 'yosys')}"
    sv2v = _sv2v()
    if not sv2v:
        return False, reason, ""
    if err := _convert_with_sv2v(sv2v, fetched, out, cwd):
        return False, f"{reason}; sv2v retry failed: {err}", ""
    rc, text = attempt("sv2v")
    if rc == 0:
        return True, "", "sv2v"
    return False, f"{reason}; after sv2v: {_why(text, rc, 'yosys')}", ""


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
    if result.bitstream:
        try:
            data = (out / result.bitstream).read_bytes()
        except OSError:
            return None
        if hashlib.sha256(data).hexdigest() != result.bitstream_sha256:
            return None
    return result


def _place_and_route(
    design: Design, out: Path, synth: str, seed: int
) -> tuple[int, str, reports.NextpnrReport]:
    """One nextpnr run; leaves the placed design in out/attempt.asc."""
    asc = out / "attempt.asc"
    asc.unlink(missing_ok=True)
    cmd = [
        str(tools.tool("nextpnr-ice40")),
        *_pnr_flags(design.clock_hz, seed),
        "--pcf", str(HARNESS / "tt_fpga_fabricfox.pcf"),
        "--json", str(out / f"out-{synth}.json"),
        "--asc", str(asc),
    ]  # fmt: skip
    rc, text = _run(cmd, out / f"nextpnr-{synth}-{seed}.log", out, _NEXTPNR_TIMEOUT)
    return rc, text, reports.parse_nextpnr(text)


def _lut_percent(report: reports.NextpnrReport) -> float | None:
    lc = report.utilisation.get(LC_TOTAL_KEY)
    return round(100 * lc[0] / lc[1], 1) if lc else None


def build(
    design: Design, fetched: FetchResult, force: bool = False, max_attempts: int = DEFAULT_MAX_ATTEMPTS
) -> BuildResult:
    """Synthesise, place, route and pack design into WORK/build/<id>/design.bin.

    Place-and-route is attempted with up to max_attempts recipes, stopping at the first that meets
    the clock. A no-fit ends the seeds of that synthesis variant, but the next variant is tried.
    Without force, a finished build of identical inputs and settings is reused.
    """
    if fetched.status != "ok":
        raise UserError(f"{design.id}: not fetched ({fetched.reason or fetched.status})")
    if max_attempts < 1:
        raise UserError("max attempts must be at least 1")
    started = time.monotonic()
    out = paths.design_dir("build", design.id).resolve()
    out.mkdir(parents=True, exist_ok=True)
    wrapper, n = re.subn(rf"\b{PLACEHOLDER}\b", design.top_module, (HARNESS / "tt_fpga_top.v").read_text())
    if n != 1:
        raise UserError(f"harness tt_fpga_top.v must name {PLACEHOLDER} exactly once, found {n}")
    versions = _tool_versions()
    digest = _inputs_digest(design, fetched, wrapper, versions, max_attempts)
    if not force and (hit := _cached(out, digest)):
        return hit
    (out / "build.json").unlink(missing_ok=True)  # a crash from here on must not leave a stale ok
    wrapper_path = out / "tt_fpga_top.v"
    wrapper_path.write_text(wrapper)

    base = {"id": design.id, "clock_hz": design.clock_hz, "tools": versions, "inputs_sha256": digest}
    attempts: list[dict] = []

    def finish(status: Status, reason: str = "", **kw) -> BuildResult:
        seconds = round(time.monotonic() - started, 1)
        result = BuildResult(status=status, reason=reason, seconds=seconds, attempts=attempts, **base, **kw)
        return _write_result(out, result)

    unsafe = _unsafe_paths([wrapper_path, out / "sv2v.v"], fetched.sources, fetched.include_dirs)
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
        if synth not in cells_by_synth:
            ok, reason, reader = _synthesise(fetched, out, wrapper_path, synth, reader)
            if not ok:
                stop = ("synth-fail", reason)
                break
            log = (out / f"yosys-{synth}.log").read_text(errors="replace")
            cells_by_synth[synth] = reports.parse_yosys(log)
        rc, text, report = _place_and_route(design, out, synth, seed)
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
        if rc != 0 or not (out / "attempt.asc").exists():
            attempt["status"] = "pnr-fail"
            stop = ("pnr-fail", _why(text, rc, "nextpnr"))
            break
        passed = report.fmax_hz is not None and report.fmax_hz >= design.clock_hz
        attempt["status"] = "ok" if passed else "timing-fail"
        if passed or best is None or (report.fmax_hz or -1) > (best["fmax_hz"] or -1):
            (out / "attempt.asc").replace(out / "out.asc")
            best = {
                "fmax_hz": report.fmax_hz,
                "recipe": recipe,
                "cells": cells_by_synth[synth],
                "lut_percent": _lut_percent(report),
            }
        else:
            (out / "attempt.asc").unlink()
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
                sv2v=reader == "sv2v", sv_reader=reader == "sv",
            )
        return finish("pnr-fail", "no place-and-route attempt ran", sv2v=reader == "sv2v", sv_reader=reader == "sv")

    known = {
        "cells": best["cells"],
        "lut_percent": best["lut_percent"],
        "fmax_hz": best["fmax_hz"],
        "recipe": best["recipe"],
        "sv2v": reader == "sv2v",
        "sv_reader": reader == "sv",
    }
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
    if passed:
        return finish("ok", **known)
    if best["fmax_hz"] is None:
        reason = "nextpnr reported no timing for clk"
    else:
        reason = (
            f"fmax {best['fmax_hz'] / 1e6:.2f} MHz is below the {design.clock_hz / 1e6:.3f} MHz clock"
            f" (best of {len(attempts)} attempts)"
        )
    if stop:
        reason += f"; sweep stopped: {stop[1]}"
    return finish("timing-fail", reason, **known)


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
        r = build(d, fetched, force=args.force, max_attempts=args.max_attempts)
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
    p.set_defaults(func=_cmd_build)

"""Classify hardware mismatches: which simulation model reproduces each capture exactly?

usage: uv run --project <harness> python classify.py HW_OUT_DIR [shuttle/macro ...]
  (no designs given: every design dir in HW_OUT_DIR whose latest result is not 'exact')

Models (Verilator, the harness's ref_tb.cpp or the late-reset copy, ui_in 0, uio_in 0xc0 = the fpga-4 stimulus):
  rtl        the RTL, reset from clock 0 (what the harness reference is)
  net        the FPGA build's own synthesised netlist (out-default.json), reset from clock 0
               -> exact here only: the RTL and the synthesis disagree (e.g. out-of-range selects, x)
  rtl_late   the RTL, clock free for LATE clocks, then reset -> exact: the board ran the clock before reset
  net_late   both
Each capture of the design (the plain dir and any .attempt-* sibling) is placed in each model's output (after the
late reset for *_late) and compared over the overlap. Writes classify-<macro>.log per design and a summary line.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

from tthfv import compare
from tthfv.board.capture_file import load_capture

H = Path(__file__).resolve().parents[2]  # the harness checkout
S = Path(os.environ.get("TTHFV_CLASSIFY_DIR", H / "work/classify"))  # models, runs and logs
BIN = H / "tools/oss-cad-suite/bin"
TB = H / "tthfv/harness/ref_tb.cpp"
TB_LATE = Path(__file__).resolve().parent / "tb/ref_tb_late_reset.cpp"
LATE = 1_000_000
CLOCKS = 4_800_000
ENV = {"PATH": f"{BIN}:/usr/bin:/bin", "HOME": str(Path.home())}
FLAGS = [
    "--cc",
    "--exe",
    "--build",
    "-j",
    "8",
    "--prefix",
    "Vtop",
    "-O3",
    "--x-assign",
    "fast",
    "--x-initial",
    "fast",
    "-Wno-fatal",
    "-Wno-lint",
    "-Wno-style",
    "-Wno-MULTIDRIVEN",
    "-Wno-UNOPTFLAT",
    "-CFLAGS",
    "-std=c++17",
    "-MAKEFLAGS",
    "OPT_FAST=-O2",
    "--relative-includes",
]
WRAP = """module SB_IO (input PACKAGE_PIN, input LATCH_INPUT_VALUE, input CLOCK_ENABLE, input INPUT_CLK,
              input OUTPUT_CLK, input OUTPUT_ENABLE, input D_OUT_0, input D_OUT_1,
              output D_IN_0, output D_IN_1);
  parameter [5:0] PIN_TYPE = 6'b000000;
  parameter [0:0] PULLUP = 1'b0;
  parameter [0:0] NEG_TRIGGER = 1'b0;
  parameter IO_STANDARD = "SB_LVCMOS";
  assign D_IN_0 = PACKAGE_PIN;
  assign D_IN_1 = PACKAGE_PIN;
endmodule
module wrap (input [7:0] ui_in, output [7:0] uo_out, input [7:0] uio_in, output [7:0] uio_out,
             output [7:0] uio_oe, input ena, input clk, input rst_n);
  wire [7:0] uio_w = uio_in;
  tt_fpga_top t (.ui_in(ui_in), .uo_out(uo_out), .uio(uio_w), .clk(clk), .rst_n(rst_n));
  assign uio_out = 8'h00;
  assign uio_oe = 8'h00;
endmodule
"""


def run(log: Path, argv: list[str]) -> int:
    with log.open("a") as f:
        f.write("$ " + " ".join(argv) + "\n")
        f.flush()
        return subprocess.run(argv, stdout=f, stderr=subprocess.STDOUT, env=ENV, check=False).returncode


def build(shuttle: str, macro: str, out: Path, log: Path) -> dict[str, Path]:
    b = H / f"work/build/{shuttle}/{macro}"
    src = H / f"work/src/{shuttle}/{macro}/repo/src"
    srcs = []
    for line in (b / "yosys-default.ys").read_text().splitlines():
        if line.startswith("read_verilog"):
            m = re.search(r'"([^"]+)"$', line)
            if m and not m.group(1).endswith("tt_fpga_top.v"):
                srcs.append(m.group(1))
    net = out / "net"
    net.mkdir(parents=True, exist_ok=True)
    if run(
        log,
        [
            str(BIN / "yosys"),
            "-q",
            "-p",
            f"read_json {b / 'out-default.json'}; write_verilog -noattr {net / 'netlist.v'}",
        ],
    ):
        raise RuntimeError("yosys write_verilog failed")
    cells = (
        (BIN.parent / "share/yosys/ice40/cells_sim.v")
        .read_text()
        .replace("module SB_IO (", "module SB_IO_unused (")
    )
    (net / "cells_sim.v").write_text(cells)
    (net / "wrap.v").write_text(WRAP)
    models = {}
    for name, tb, kind in (
        ("rtl", TB, "rtl"),
        ("net", TB, "net"),
        ("rtl_late", TB_LATE, "rtl"),
        ("net_late", TB_LATE, "net"),
    ):
        obj = out / name / "obj"
        shutil.rmtree(obj, ignore_errors=True)
        obj.parent.mkdir(parents=True, exist_ok=True)
        if kind == "rtl":
            argv = [
                "verilator",
                *FLAGS,
                "--top-module",
                macro,
                "-Mdir",
                str(obj),
                f"-I{src}",
                "-y",
                str(src),
                str(tb),
                *srcs,
            ]
        else:
            argv = [
                "verilator",
                *FLAGS,
                "-Wno-TIMESCALEMOD",
                "-Wno-MULTITOP",
                "--top-module",
                "wrap",
                "-Mdir",
                str(obj),
                str(tb),
                str(net / "cells_sim.v"),
                str(net / "wrap.v"),
                str(net / "netlist.v"),
            ]
        if run(log, argv):
            raise RuntimeError(f"verilator {name} failed")
        models[name] = obj / "Vtop"
    return models


def captures(hw: Path, shuttle: str, macro: str) -> list[Path]:
    return sorted(p for p in hw.glob(f"{shuttle}__{macro}*/capture.vgacap"))


def classify(hw: Path, key: str) -> str:
    shuttle, macro = key.split("/")
    out = S / "classify" / macro
    out.mkdir(parents=True, exist_ok=True)
    S.mkdir(parents=True, exist_ok=True)
    log = S / f"classify-{macro}.log"
    log.write_text("")
    models = build(shuttle, macro, out, log)
    caps = [(p.parent.name, load_capture(p).uo_out) for p in captures(hw, shuttle, macro)]
    verdicts = {}
    for name, exe in models.items():
        d = out / name / "run"
        d.mkdir(exist_ok=True)
        late = name.endswith("_late")
        argv = [
            str(exe),
            "--clocks",
            str(CLOCKS + (LATE if late else 0)),
            "--ui-in",
            "0",
            "--uio-in",
            "0xc0",
            "--out",
            str(d),
        ]
        if late:
            argv += ["--reset-at", str(LATE)]
        if run(log, argv):
            verdicts[name] = "sim failed"
            continue
        dump = np.fromfile(d / "uo_out.bin", np.uint8)
        if late:
            dump = dump[LATE:]
        res = []
        for cname, cap in caps:
            loc = compare.locate_capture(cap, dump, max_start_clocks=840000)
            if loc.start_clock is None:
                res.append("no-match")
                continue
            c = compare._compare_arrays(cap, dump, -loc.start_clock, overlap_only=True)
            res.append("exact" if c.diff_clocks == 0 else f"{c.diff_clocks}diff")
            with log.open("a") as f:
                f.write(
                    f"{name} vs {cname}: placed {loc.start_clock}, {c.compared_clocks} compared, {c.diff_clocks} differ\n"
                )
        verdicts[name] = ",".join(res)
        (d / "uo_out.bin").unlink(missing_ok=True)
        (d / "uo_out_low.bin").unlink(missing_ok=True)
        (d / "uio.bin").unlink(missing_ok=True)
    exact = [n for n, v in verdicts.items() if v and all(x == "exact" for x in v.split(","))]
    cause = (
        "rtl matches (?)"
        if "rtl" in exact
        else "synthesis differs from RTL"
        if "net" in exact
        else "clock ran before reset"
        if "rtl_late" in exact
        else "synthesis + clock before reset"
        if "net_late" in exact
        else "unexplained"
    )
    line = f"{key}: {cause}  {json.dumps(verdicts)}  ({len(caps)} captures)"
    with log.open("a") as f:
        f.write(line + "\n")
    return line


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for need in (BIN, H / "work/build"):
        if not need.is_dir():
            sys.exit(f"missing {need}: run from the harness checkout that built the library")
    hw = Path(sys.argv[1]).resolve()
    keys = sys.argv[2:]
    if not keys:
        seen = set()
        for d in sorted(hw.iterdir()):
            if not d.is_dir() or ".attempt-" in d.name:
                continue
            r = d / "result" / "result.json"
            if r.exists() and json.loads(r.read_text()).get("verdict") != "exact":
                shuttle, macro = d.name.split("__", 1)
                seen.add(f"{shuttle}/{macro}")
        keys = sorted(seen)
    print(f"{len(keys)} designs", flush=True)
    for k in keys:
        try:
            print(classify(hw, k), flush=True)
        except Exception as exc:  # noqa: BLE001 - one design failing must not stop the rest
            print(f"{k}: error {exc!r}", flush=True)


if __name__ == "__main__":
    main()

"""Write docs/results/2026-11-hardware-library-fpga-4.md from a hw_batch.py output directory and classify.py's log.

usage: uv run python scripts/hardware_library/make_report.py HW_OUT_DIR CLASSIFY_LOG OUT_MD

For each design directory (not the .attempt-* ones) it reads design.json and result/result.json; the cause
column for non-exact designs comes from the classify log ("<shuttle>/<macro>: <cause>  {...}").
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

hw, clog, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])

causes = {}
for line in clog.read_text().splitlines():
    m = re.match(r"(\S+/\S+): (.+?)  \{", line)
    if m:
        causes[m.group(1)] = m.group(2)

CAUSE_TEXT = {
    "synthesis differs from RTL": "synthesis differs",
    "clock ran before reset": "clock before reset",
    "synthesis + clock before reset": "both",
    "unexplained": "board history",
}

rows = []
for d in sorted(hw.iterdir()):
    if not d.is_dir() or ".attempt-" in d.name or not (d / "design.json").exists():
        continue
    dj = json.loads((d / "design.json").read_text())
    rj_path = d / "result" / "result.json"
    rj = json.loads(rj_path.read_text()) if rj_path.exists() else {}
    attempts = 1 + sum(1 for _ in hw.glob(d.name + ".attempt-*"))
    rows.append(
        {
            "key": dj["key"],
            "verdict": rj.get("verdict", "not run"),
            "compared": (rj.get("comparison") or {}).get("compared_clocks"),
            "sha": dj["bin_sha256"][:12],
            "attempts": attempts,
            "cause": CAUSE_TEXT.get(causes.get(dj["key"], ""), ""),
        }
    )

tally = Counter(r["verdict"] for r in rows)
ctally = Counter(r["cause"] for r in rows if r["verdict"] != "exact")
exact = [r for r in rows if r["verdict"] == "exact"]
other = [r for r in rows if r["verdict"] != "exact"]


def fmt(n):
    return f"{n:,}" if isinstance(n, int) else "-"


lines = [
    "# Published library bitstreams on fpga-4: hardware against simulation",
    "",
    "Date: 2026-11-27, 06:46-09:32 UTC, in three batches with gaps, each under a broker claim on",
    "[fpga-4](https://tinytapeout.fpgas.online/board/fpga-4/) (RP2350 demo board with the FabricFox iCE40UP5K",
    "breakout; Pi `pi-sw2-p35`). Each design's published `.bin` from the",
    "[FPGA library](https://data.wafer.space/big-storage/tinytapeout-vga-videos/fpga-library.json) was loaded",
    "through the board's daemon, captured with [vgacap](https://github.com/mithro/vgacap) `ttcap capture` at",
    "commit 2a1caa7 (`--mode stream --clock-hz 400000 --frames 8`: one `uo_out` sample per project clock,",
    "4,210,688 clocks), and compared with `tthfv board compare-capture` against the design's",
    "[`fpga-4`](../../boards/fpga-4.json) reference (Verilator, ui_in 0, uio_in 0xc0).",
    "",
    "Driver: [`scripts/hardware_library/hw_batch.py`](../../scripts/hardware_library/hw_batch.py) with its Pi",
    "side [`pi_helper.py`](../../scripts/hardware_library/pi_helper.py). Causes of the non-exact results:",
    "[`classify.py`](../../scripts/hardware_library/classify.py).",
    "",
    "Designs: every library entry with `build_status` ok, no inputs needed (`ui_in` 0 or none), no PDK",
    f"latch cells, and an fpga-4 reference: {len(rows)}.",
    "",
    "## Totals",
    "",
    "| verdict | designs |",
    "|---|---|",
]
for v, n in tally.most_common():
    lines.append(f"| {v} | {n} |")
lines += [
    "",
    "`exact` means every compared clock of the capture equals the reference, bit for bit, after placing the",
    "capture's first 4,096 samples in the reference's first two frames.",
    "",
    "## Causes of the non-exact results",
    "",
    "`classify.py` simulates each non-exact design four ways and reports which one reproduces the board's",
    "capture exactly:",
    "",
    "| model | what differs from the reference |",
    "|---|---|",
    "| rtl | nothing (the reference itself) |",
    "| net | the FPGA build's own synthesised netlist instead of the RTL |",
    "| rtl_late | the clock runs 1,000,000 clocks before reset is applied |",
    "| net_late | both |",
    "",
    "| cause | meaning | designs |",
    "|---|---|---|",
    f"| synthesis differs | only `net` matches | {ctally.get('synthesis differs', 0)} |",
    f"| clock before reset | `rtl_late` matches | {ctally.get('clock before reset', 0)} |",
    f"| both | only `net_late` matches | {ctally.get('both', 0)} |",
    f"| board history | no model matches | {ctally.get('board history', 0)} |",
    "",
    "- **Synthesis differs.** The design reads bits outside a vector (for example `layer_a_x[86]` of a",
    "  10-bit wire in `tt_um_harveywong85_harveywilly`) or otherwise depends on `x`. Yosys and Verilator resolve",
    "  it differently. The board matches the netlist exactly, so the bitstream is right; the RTL reference",
    "  cannot model it. The ASIC flow also uses Yosys.",
    "- **Clock before reset.** The board clocks the design before ttcap's reset. A design whose reset logic is",
    "  clocked by one of its own outputs (for example `tt_um_KK_VGA01`, `motor_handler.v:37-38`, clocked by",
    "  vsync) never sees the edge it needs during reset. No reset sequence can fix this",
    "  ([vgacap #73](https://github.com/mithro/vgacap/issues/73)); loading with the clock held until reset is",
    "  released can.",
    "- **Board history.** Registers that the reset never clears hold a value that depends on how long the",
    "  design ran before the capture (for example the older `tt_um_rejunity_vga_logo`, whose frame counter is",
    "  clocked by vsync). The differing-clock count changes with the pre-reset clocking in the models; this cause",
    "  is likely, not proven.",
    "",
    "No design was found where the FPGA differs from its own synthesised netlist.",
    "",
    "| id | verdict | clocks compared | cause | attempts | bin sha256 |",
    "|---|---|---|---|---|---|",
]
for r in other:
    lines.append(
        f"| {r['key']} | {r['verdict']} | {fmt(r['compared'])} | {r['cause']} | {r['attempts']} | {r['sha']} |"
    )
lines += [
    "",
    "Attempts: `tt_um_harveywong85_harveywilly` and `tt_um_KK_VGA01` were also captured once with `ui_in`",
    "driven to 0 (`ttcap --ui-in 0`); the result was the same.",
    "",
    "## Exact",
    "",
    "| id | clocks compared | bin sha256 |",
    "|---|---|---|",
]
for r in exact:
    lines.append(f"| {r['key']} | {fmt(r['compared'])} | {r['sha']} |")
out.write_text("\n".join(lines) + "\n")
print(f"wrote {out}: {len(rows)} designs, {dict(tally)}, causes {dict(ctally)}")

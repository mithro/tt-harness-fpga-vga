"""Write docs/results/2026-11-hardware-library-fpga-4.md from a hw_batch.py run and classify.py's log.

usage: uv run python scripts/hardware_library/make_report.py HW_OUT_DIR CLASSIFY_LOG OUT_MD

HW_OUT_DIR is hw_batch.py's --out directory: one directory per design (design.json, result/result.json, plus
.attempt-* siblings for earlier captures) and batch.log. CLASSIFY_LOG is classify.py's output over the non-exact
designs. Tables, counts, run times and the netlist statement are computed from these inputs. The method and the
explanation of each cause are fixed text about the 2026-11-27 run: re-check them before regenerating from a new run.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

if len(sys.argv) != 4:
    sys.exit(__doc__)
hw, clog, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])

VGACAP = "https://github.com/mithro/vgacap"
VGACAP_REV = "2a1caa703dc0196feac0f9fd5d7f6dfcc443f892"
KK_SRC = "https://github.com/magnetoField/ttsky-zuzel-ieee/blob/3cb736cc17304add62f5cf4f2de477c5041dc56c"
HARVEY_SRC = "https://github.com/harveywong85/harvey_and_willy/blob/5b3d55dcf3e1e9dcab816408c1e55842969ce7fa"
LATCHUP_SRC = (
    "https://github.com/28add11/tt_workshop_latchup2026/blob/40d4453982cf2ef76f53985f83110d4817e11d29"
)
LOGO_SRC = (
    "https://github.com/rejunity/tt08-huge-480x480-tiny-tapeout-logo-in-1-tile/blob/"
    "db5615f0a8d3fd5ee738526bbc2e877fc608d1d2"
)

# classify.py's cause text -> the name used in the doc
CAUSE_TEXT = {
    "synthesis differs from RTL": "synthesis differs",
    "clock ran before reset": "clock before reset",
    "synthesis + clock before reset": "both",
    "unexplained": "unexplained",
}

causes, verdicts = {}, {}
for line in clog.read_text().splitlines():
    m = re.match(r"(\S+/\S+): (.+?)  (\{.*\})  \(\d+ captures\)$", line)
    if m:
        causes[m.group(1)] = CAUSE_TEXT[m.group(2)]
        verdicts[m.group(1)] = json.loads(m.group(3))


def all_exact(v: str) -> bool:
    return all(x == "exact" for x in v.split(","))


rows = []
for d in sorted(hw.iterdir()):
    if not d.is_dir() or ".attempt-" in d.name or not (d / "design.json").exists():
        continue
    dj = json.loads((d / "design.json").read_text())
    rj_path = d / "result" / "result.json"
    rj = json.loads(rj_path.read_text()) if rj_path.exists() else {}
    verdict = rj.get("verdict", "not run")
    if verdict != "exact" and dj["key"] not in causes:
        sys.exit(f"{dj['key']} is {verdict} but has no line in {clog}")
    rows.append(
        {
            "key": dj["key"],
            "verdict": verdict,
            "compared": (rj.get("comparison") or {}).get("compared_clocks"),
            "sha": dj["bin_sha256"],
            "attempts": 1 + sum(1 for _ in hw.glob(d.name + ".attempt-*")),
            "cause": causes.get(dj["key"], ""),
        }
    )

# runs: each "eligible" line starts one; "[i/n]" lines are designs; "restored:" ends it
runs = []
for line in (hw / "batch.log").read_text().splitlines():
    t, _, msg = line.partition(" ")
    if msg.startswith("eligible "):
        runs.append({"start": t, "end": None, "designs": 0})
    elif re.match(r"\[\d+/\d+\] ", msg):
        runs[-1]["designs"] += 1
    elif msg.startswith("restored:"):
        runs[-1]["end"] = t
empty_starts = [r for r in runs if r["designs"] == 0]
runs = [r for r in runs if r["designs"]]

tally = Counter(r["verdict"] for r in rows)
ctally = Counter(r["cause"] for r in rows if r["verdict"] != "exact")
exact = [r for r in rows if r["verdict"] == "exact"]
other = [r for r in rows if r["verdict"] != "exact"]
exact_clocks = {r["compared"] for r in exact}
if len(exact_clocks) != 1:
    sys.exit(f"exact designs compared different clock counts: {sorted(exact_clocks)}")
(exact_clocks,) = exact_clocks
by_net = [k for k, v in verdicts.items() if all_exact(v["net"]) or all_exact(v["net_late"])]
not_net = sorted(set(verdicts) - set(by_net))


def fmt(n):
    return f"{n:,}" if isinstance(n, int) else "-"


def hhmm(t: str) -> str:
    return t[11:16]


lines = [
    "# Published library bitstreams on fpga-4: hardware against simulation",
    "",
    f"Date: 2026-11-27, {hhmm(runs[0]['start'])}-{hhmm(runs[-1]['end'])} UTC, under",
    "[fpgas.online broker](https://tinytapeout.fpgas.online/) claims on",
    "[fpga-4](https://tinytapeout.fpgas.online/board/fpga-4/) (RP2350 demo board, FabricFox iCE40UP5K breakout, Pi",
    "`pi-sw2-p35`).",
    "",
    "| run | start (UTC) | end (UTC) | entries captured |",
    "|---|---|---|---|",
]
for i, r in enumerate(runs, 1):
    lines.append(f"| {i} | {hhmm(r['start'])} | {hhmm(r['end'])} | {r['designs']} |")
lines += [
    "",
    f"Runs 1 and 3 stopped at their time budget; run 2 recaptured 2 designs. {len(empty_starts)} further start"
    + (f" ({', '.join(hhmm(r['start']) for r in empty_starts)})" if empty_starts else "")
    + " ran no design.",
    "",
    "## Method",
    "",
    "- **Designs.** Every entry of the",
    "  [FPGA library](https://data.wafer.space/big-storage/tinytapeout-vga-videos/fpga-library.json) with",
    "  `build_status` ok, `ui_in` 0 or none, no PDK latch cells and an fpga-4 reference:",
    f"  {len(rows)} entries, {len({r['sha'] for r in rows})} distinct bitstreams (designs submitted to",
    "  several shuttles).",
    "- **Load.** The published `.bin`, through the board's fpgas-tt daemon.",
    f"- **Capture.** [ttcap]({VGACAP}/tree/{VGACAP_REV}/python/ttcap) `capture` at vgacap",
    f"  [{VGACAP_REV[:7]}]({VGACAP}/commit/{VGACAP_REV}), `--mode stream --clock-hz 400000 --frames 8`",
    "  (capture header `mode=extclk`): the RP2350 drives the project clock and records `uo_out` once per clock.",
    "- **Compare.** [tthfv](../../README.md) `board compare-capture` against the design's",
    "  [`fpga-4`](../../boards/fpga-4.json) reference ([Verilator](https://verilator.org/), `ui_in` 0, `uio_in`",
    "  0xc0).",
    "- **Scripts.** Driver [`hw_batch.py`](../../scripts/hardware_library/hw_batch.py), Pi side",
    "  [`pi_helper.py`](../../scripts/hardware_library/pi_helper.py), causes",
    "  [`classify.py`](../../scripts/hardware_library/classify.py), this page",
    "  [`make_report.py`](../../scripts/hardware_library/make_report.py).",
    "",
    "## Totals",
    "",
    "| verdict | entries |",
    "|---|---|",
]
for v, n in tally.most_common():
    lines.append(f"| {v} | {n} |")
lines += [
    "",
    "`exact`: every compared clock of the capture equals the reference, bit for bit, after placing the capture's",
    "first 4,096 samples in the reference's first two frames.",
    "",
    "## Causes of the non-exact results",
    "",
    "[`classify.py`](../../scripts/hardware_library/classify.py) simulates each non-exact design four ways",
    "([Verilator](https://verilator.org/), same stimulus as the reference) and checks which reproduces every",
    "capture of it exactly.",
    "",
    "| model | design source | reset released after |",
    "|---|---|---|",
    "| rtl | RTL | 10 clocks |",
    "| net | FPGA build netlist | 10 clocks |",
    "| rtl_late | RTL | 1,000,010 clocks |",
    "| net_late | FPGA build netlist | 1,000,010 clocks |",
    "",
    "`rtl` is the reference itself. The netlist is the build's [Yosys](https://yosyshq.net/yosys/) output",
    "(`out-default.json`).",
    "",
    "| cause | model that matches | entries |",
    "|---|---|---|",
    f"| synthesis differs | net | {ctally.get('synthesis differs', 0)} |",
    f"| clock before reset | rtl_late | {ctally.get('clock before reset', 0)} |",
    f"| both | net_late only | {ctally.get('both', 0)} |",
    f"| unexplained | none | {ctally.get('unexplained', 0)} |",
    "",
    "- **Synthesis differs.** The RTL and the synthesised netlist behave differently, and the board follows the",
    "  netlist. The mechanism was read in the source for two designs; for the rest only the model match is known.",
    "  - `tt_um_harveywong85_harveywilly` reads bits far outside 10-bit wires, for example `layer_a_x[86]`",
    f"    ([`project.v:46`]({HARVEY_SRC}/src/project.v#L46)). Yosys makes them x; Verilator gives them values.",
    "  - `tt_um_28add11_latchup` reads bit -1 in `xShiftReg[pix_x[9:4] -: 2]`",
    f"    ([`project.v:74`]({LATCHUP_SRC}/src/project.v#L74)).",
    "- **Clock before reset.** The RTL matches once it runs freely before reset: the board clocks the design",
    "  before ttcap's reset. The mechanism was read in the source for one design; for the rest only the model",
    "  match is known.",
    "  - `tt_um_KK_VGA01` samples reset only on vsync rising edges",
    f"    ([`motor_handler.v:37-38`]({KK_SRC}/src/motor_handler.v#L37-L38)). On the board vsync is already high",
    "    when reset starts, so its game state is never reset.",
    f"  - No reset sequence can fix this ([vgacap #73]({VGACAP}/issues/73)). A load that holds the clock until",
    "    reset is released (planned vgacap PR 4d) should; that is untested.",
    "- **Unexplained.** No model reproduces the capture; the cause is not established.",
    "  - `tt_um_rejunity_vga_logo` (tt08, ttcad25a) clears a frame counter on reset inside",
    f"    `always @(posedge vsync)` ([`project.v:335`]({LOGO_SRC}/src/project.v#L335)), the same pattern as",
    "    `tt_um_KK_VGA01`, yet `rtl_late` does not match it.",
    "",
    f"The netlist models (`net` or `net_late`) reproduce the board exactly for {len(by_net)} of the",
    f"{len(verdicts)} non-exact entries. They do not for these {len(not_net)}:",
    "",
]
for k in not_net:
    lines.append(f"- `{k}` ({causes[k]})")
lines += [
    "",
    "For the clock-before-reset entries in this list, `rtl_late` matches but `net_late` does not; why is not",
    "established.",
    "",
    "| id | verdict | clocks compared | cause | captures | bin sha256 |",
    "|---|---|---|---|---|---|",
]
for r in other:
    lines.append(
        f"| {r['key']} | {r['verdict']} | {fmt(r['compared'])} | {r['cause']} | {r['attempts']} | {r['sha'][:12]} |"
    )
lines += [
    "",
    "Extra captures:",
    "",
    "- Run 3 recaptured the 3 entries that were not exact in run 1.",
    "- Run 2 captured `tt_um_harveywong85_harveywilly` and `tt_um_KK_VGA01` with `ttcap --ui-in 0`. Whether that",
    "  reaches the FPGA is not established: the inputs these designs read are 0 when undriven too.",
    "- Every capture of an entry gave the same verdict, and `classify.py` checked all of them.",
    "",
    "## Exact",
    "",
    f"All {len(exact)} compared {fmt(exact_clocks)} clocks in one capture.",
    "",
    "| id | bin sha256 |",
    "|---|---|",
]
for r in exact:
    lines.append(f"| {r['key']} | {r['sha'][:12]} |")
out.write_text("\n".join(lines) + "\n")
print(
    f"wrote {out}: {len(rows)} entries, {dict(tally)}, causes {dict(ctally)}, netlist matches {len(by_net)}"
)

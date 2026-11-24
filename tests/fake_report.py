# SPDX-License-Identifier: Apache-2.0
"""Writes the machine-readable files the real tools write, for the fake nextpnr and yosys of the build tests
(run by them as `python fake_report.py ...`).

    fake_report.py nextpnr REPORT FREQ_MHZ MAIN_MHZ SIDE LOG
        MAIN_MHZ: the main clock's achieved MHz, or "" to take the clocks and utilisation from the old-style
        log text in LOG (the last "Max frequency" line of each clock). SIDE: "name=MHz" or "".
    fake_report.py stat PATH LOG
        yosys `stat -json` of the cell counts of the old-style "design hierarchy" block of LOG.
"""

import json
import re
import sys
from pathlib import Path

MAIN = "clk$SB_IO_IN_$glb_clk"


def nextpnr(report: str, freq: float, main: str, side: str, log: str) -> None:
    fmax: dict[str, dict] = {}
    util = {"ICESTORM_LC": {"used": 824, "available": 5280}}
    if main:
        fmax[MAIN] = {"achieved": float(main), "constraint": freq}
        if side:
            name, _, value = side.partition("=")
            fmax[name] = {"achieved": float(value), "constraint": freq}
    else:
        text = Path(log).read_text()
        for name, mhz in re.findall(r"Max frequency for clock\s+'([^']+)':\s+([0-9.]+) MHz", text):
            fmax[name] = {"achieved": float(mhz), "constraint": freq}
        for name, used, total in re.findall(r"(ICESTORM_\w+):\s+(\d+)\s*/\s*(\d+)\s+\d+%", text):
            util[name] = {"used": int(used), "available": int(total)}
    Path(report).write_text(json.dumps({"fmax": fmax, "utilization": util}))


def stat(path: str, log: str) -> None:
    text = Path(log).read_text()
    marker = text.rfind("=== design hierarchy ===")
    block = text[marker:] if marker >= 0 else text
    cells: dict[str, int] = {}
    for n, cell in re.findall(r"^\s+(\d+)\s+(SB_\w+)\s*$", block, re.MULTILINE):
        cells[cell] = cells.get(cell, 0) + int(n)
    Path(path).write_text(json.dumps({"design": {"num_cells_by_type": cells}}))


if __name__ == "__main__":
    if sys.argv[1] == "nextpnr":
        nextpnr(sys.argv[2], float(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6])
    else:
        stat(sys.argv[2], sys.argv[3])

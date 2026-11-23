# SPDX-License-Identifier: Apache-2.0
"""Timing experiment: re-run nextpnr on a timing-fail design's kept attempt with extra options.

For each design id it reads WORK/build/<id>/build.json, takes the kept attempt's recipe (synthesis
variant and seed) and re-runs nextpnr-ice40 on that variant's synthesized netlist with the same flags
the build stage uses, three times: as built (baseline, to show the run is reproducible), with
--opt-timing, and with --tmg-ripup. It prints the fmax of each. Nothing under WORK is changed;
outputs go to --scratch.

Usage:
  uv run python scripts/first_batch/timing_experiment.py --work WORK --tools TOOLS --scratch DIR ID...
"""

import argparse
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

FMAX = re.compile(r"Max frequency for clock\s+'[^']*':\s+([\d.]+) MHz")
VARIANTS = {"baseline": [], "opt-timing": ["--opt-timing"], "tmg-ripup": ["--tmg-ripup"]}


def run_one(
    nextpnr: str, pcf: Path, build: Path, rec: dict, clock_hz: int, extra: list[str], log: Path
) -> str:
    cmd = [
        nextpnr, "--up5k", "--package", "sg48", "--pcf-allow-unconstrained",
        "--freq", f"{clock_hz / 1e6:g}", "--timing-allow-fail", "--seed", str(rec["seed"]),
        *extra, "--pcf", str(pcf), "--json", str(build / f"out-{rec['synth']}.json"),
        "--asc", str(log.with_suffix(".asc")),
    ]  # fmt: skip
    with log.open("w") as f:
        rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, check=False).returncode
    found = FMAX.findall(log.read_text(errors="replace"))
    return (found[-1] + " MHz" if found else "no fmax") + ("" if rc == 0 else f" (exit {rc})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--tools", type=Path, required=True)
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument(
        "--pcf",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "tthfv/harness/tt_fpga_fabricfox.pcf",
    )
    ap.add_argument("ids", nargs="+")
    a = ap.parse_args()
    nextpnr = str(a.tools / "oss-cad-suite/bin/nextpnr-ice40")
    jobs = []
    for design in a.ids:
        build = a.work / "build" / design
        info = json.loads((build / "build.json").read_text())
        out = a.scratch / design
        out.mkdir(parents=True, exist_ok=True)
        for name, extra in VARIANTS.items():
            jobs.append((design, name, build, info, extra, out / f"{name}.log"))
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = [
            (d, n, info, ex.submit(run_one, nextpnr, a.pcf, b, info["recipe"], info["clock_hz"], e, log))
            for d, n, b, info, e, log in jobs
        ]
        for d, n, info, fut in futs:
            print(
                f"{d} recipe={info['recipe']} clock={info['clock_hz'] / 1e6:g} MHz "
                f"build.json fmax={info['fmax_hz'] / 1e6:.2f} MHz  {n}: {fut.result()}"
            )


if __name__ == "__main__":
    main()

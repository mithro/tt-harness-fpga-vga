# SPDX-License-Identifier: Apache-2.0
"""List the clocks each design's build reports: build.json status and every clock's final Fmax/verdict,
and, for the kept attempt, how many clocks the final nextpnr log of that attempt reports.

Usage:
  uv run python scripts/first_batch/list_clocks.py --work WORK ID...
"""

import argparse
import json
from pathlib import Path

from tthfv import reports


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("ids", nargs="+")
    a = ap.parse_args()
    for did in a.ids:
        b = a.work / "build" / did
        info = json.loads((b / "build.json").read_text())
        rec = info["recipe"]
        log = b / f"nextpnr-{rec['synth']}-{rec['seed']}.log" if rec else None
        parsed = reports.parse_nextpnr(log.read_text(errors="replace")) if log and log.exists() else None
        print(f"{did}: {info['status']} main fmax {info['fmax_hz']}; build.json clocks {len(info['clocks'])}; "
              f"kept-attempt log clocks {len(parsed.clocks) if parsed else None}")
        for n, c in info["clocks"].items():
            print(f"    {n}: {c['fmax_hz'] / 1e6:.2f} MHz {'PASS' if c['pass'] else 'FAIL'}")


if __name__ == "__main__":
    main()

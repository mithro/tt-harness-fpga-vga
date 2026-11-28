# SPDX-License-Identifier: Apache-2.0
"""Write data/hardware/fpga-4.json, the machine-readable record of a hw_batch.py run, from its output.

usage: uv run python scripts/hardware_library/make_record.py HW_OUT_DIR CLASSIFY_LOG OUT_JSON

HW_OUT_DIR and CLASSIFY_LOG are the inputs of make_report.py (read it for their format). The output holds one record
per design that was run, keyed by library id: the SHA-256 of the captured bitstream, the verdict, the cause (null
for exact), the capture date, the vgacap revision and the board. `tthfv publish` reads it (rule in
tthfv/publish.py, `_validation`). The vgacap revision and the board are fixed facts of the 2026-11-27 run:
re-check them before regenerating from a new run.
"""

import json
import re
import sys
from pathlib import Path

from tthfv.publish import library_id

if len(sys.argv) != 4:
    sys.exit(__doc__)
hw, clog, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])

VGACAP_REV = "2a1caa703dc0196feac0f9fd5d7f6dfcc443f892"
BOARD = "fpga-4"

# classify.py's cause text -> the name used in the record (the same mapping as make_report.py)
CAUSE_TEXT = {
    "synthesis differs from RTL": "synthesis differs",
    "clock ran before reset": "clock before reset",
    "synthesis + clock before reset": "both",
    "unexplained": "unexplained",
}

causes = {}
for line in clog.read_text().splitlines():
    m = re.match(r"(\S+/\S+): (.+?)  (\{.*\})  \(\d+ captures\)$", line)
    if m:
        causes[m.group(1)] = CAUSE_TEXT[m.group(2)]

records = {}
for d in sorted(hw.iterdir()):
    if not d.is_dir() or ".attempt-" in d.name or not (d / "design.json").exists():
        continue
    dj = json.loads((d / "design.json").read_text())
    key = dj["key"]
    rj_path = d / "result" / "result.json"
    verdict = json.loads(rj_path.read_text()).get("verdict") if rj_path.exists() else None
    if verdict not in ("exact", "mismatch", "no-match"):
        sys.exit(f"{key}: verdict {verdict!r} is not exact, mismatch or no-match")
    if dj.get("board") != BOARD:
        sys.exit(f"{key}: captured on {dj.get('board')!r}, not {BOARD}")
    if verdict != "exact" and key not in causes:
        sys.exit(f"{key} is {verdict} but has no line in {clog}")
    shuttle, _, macro = key.partition("/")
    id = library_id(shuttle, macro)
    if id in records:
        sys.exit(f"{key}: the id {id} is already used by another design")
    records[id] = {
        "bin_sha256": dj["bin_sha256"],
        "board": BOARD,
        "cause": None if verdict == "exact" else causes[key],
        "date": dj["started"][:10],
        "vgacap_revision": VGACAP_REV,
        "verdict": verdict,
    }

out.write_text(json.dumps({"records": records, "schema": 1}, indent=2, sort_keys=True) + "\n")
print(f"wrote {out}: {len(records)} records")

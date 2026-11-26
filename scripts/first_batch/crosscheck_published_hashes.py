# SPDX-License-Identifier: Apache-2.0
"""Cross-check: our frames against the per-frame hashes another project published.

For each design id: read the published tb-v1 hash file
<published>/<id>/frames/tb-v1/frame_hashes.fnv1a64-tb2026-rgb888.txt (vgacap's hash-file format; header
fields capture_after_clock, clocks_per_pixel, hash_offset_basis, mode, frames) and its result.json, cut ALL
frames from our dump, take the frames whose start clock is strictly after capture_after_clock (both are in
the spec's clock numbering: clock 0 is the first reset clock and our start_clock is that clock, so nothing
is added or subtracted), re-hash them with that project's FNV offset and compare, in order, with its
first hashes. At videos-repo commit 148ccf5 and before, the file was data/results/<id>/frame_hashes.txt
(added at 3505405) and the value was in the testbench's counter, one more than our start clock.

A design is skipped (and says why) when its published run was scripted, used a non-zero ui_in, did
not report 640x480 or had clocks_per_pixel other than 1.0, or has no hash file.

Usage:
  uv run python scripts/first_batch/crosscheck_published_hashes.py --work WORK \\
      --published DIR/data/results ID...
"""

import argparse
import json
import sys
from pathlib import Path

from tthfv import frames, manifest, paths

HASH_FILE = Path("frames") / "tb-v1" / "frame_hashes.fnv1a64-tb2026-rgb888.txt"
HASH_METHOD = "fnv1a64-tb2026-rgb888"


def parse_header(path: Path) -> tuple[dict, list[str]]:
    meta, hashes = {}, []
    for line in path.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
        else:
            hashes.append(line.strip())
    return meta, hashes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument(
        "--published", type=Path, required=True, help="the other project's data/results directory"
    )
    ap.add_argument("ids", nargs="+")
    a = ap.parse_args()
    designs = manifest.load(extras=manifest.EXTRAS_DEFAULT)
    bad = 0
    for did in a.ids:
        pub = a.published / did
        hf, rj = pub / HASH_FILE, pub / "result.json"
        if not hf.exists():
            print(f"{did}: SKIP no published {HASH_FILE}")
            continue
        meta, theirs = parse_header(hf)
        if meta.get("hash_method") != HASH_METHOD or meta.get("sampler") != "tb-v1":
            raise SystemExit(f"{did}: {hf} is not a tb-v1 {HASH_METHOD} file: {meta}")
        if meta.get("frames") != str(len(theirs)):
            raise SystemExit(f"{did}: {hf} says frames: {meta.get('frames')} but has {len(theirs)} hashes")
        res = json.loads(rj.read_text())
        design = manifest.by_id(designs, did)
        why = []
        if design.scripted or res.get("override"):
            why.append(f"scripted (override {res.get('override')})")
        if design.ui_in != 0 or res.get("auto_ui_in") not in (None, 0):
            why.append(f"ui_in {design.ui_in} / auto_ui_in {res.get('auto_ui_in')}")
        if (meta.get("width"), meta.get("height")) != ("640", "480"):
            why.append(f"size {meta.get('width')}x{meta.get('height')}")
        if float(meta.get("clocks_per_pixel", 0)) != 1.0:
            why.append(f"clocks_per_pixel {meta.get('clocks_per_pixel')}")
        if why:
            print(f"{did}: SKIP {'; '.join(why)}")
            continue
        after = int(meta["capture_after_clock"])
        offset = int(meta["hash_offset_basis"], 16)
        ref_dir = paths.design_dir("reference", did) / "ideal"
        samples = frames.load_dump(ref_dir / "uo_out.bin.gz")
        fs = frames.cut_frames(samples)
        ours = [f for f in fs.frames if f.start_clock > after]
        n = min(len(ours), len(theirs))
        rehash = [f"{frames.frame_hash(f.rgb888, offset=offset):016x}" for f in ours[:n]]
        equal = sum(x == y for x, y in zip(rehash, theirs[:n]))
        first = ours[0].index if ours else None
        print(
            f"{did}: {len(samples)} clocks, {len(fs.frames)} frames cut ({fs.mode}), capture_after_clock {after}, "
            f"our first frame after it = {first} (start {ours[0].start_clock if ours else None}); "
            f"compared {n}, equal {equal}, distinct hashes among them {len(set(rehash))}"
        )
        if equal != n or n == 0:
            bad += 1
            for i, (x, y) in enumerate(zip(rehash, theirs[:n])):
                print(f"    frame {ours[i].index}: ours {x} theirs {y} {'==' if x == y else '!='}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

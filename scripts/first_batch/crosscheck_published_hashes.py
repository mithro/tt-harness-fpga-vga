# SPDX-License-Identifier: Apache-2.0
"""Cross-check: our frames against the per-frame hashes another project published.

For each design id: read the published frame_hashes.txt (header fields capture_after_clock,
clocks_per_pixel, hash_offset_basis, mode) and its result.json, cut ALL frames from our dump, take
the frames whose start is after capture_after_clock (their clock counter is our sample index + 1),
re-hash them with that project's FNV offset and compare, in order, with its first hashes.

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
        hf, rj = pub / "frame_hashes.txt", pub / "result.json"
        if not hf.exists():
            print(f"{did}: SKIP no published frame_hashes.txt")
            continue
        meta, theirs = parse_header(hf)
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
        ours = [f for f in fs.frames if f.start_clock + 1 > after]
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

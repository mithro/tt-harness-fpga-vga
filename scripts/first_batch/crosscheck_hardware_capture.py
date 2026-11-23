# SPDX-License-Identifier: Apache-2.0
"""Cross-check: a real board capture (vgacap stream) against our simulated reference.

Two comparisons for one design:
  1. Every frame that the vgacap project's own `vgacap-frames` tool reconstructed (PPM files) against
     our simulated frame-000.png.
  2. The raw one-byte-per-clock samples of the capture, read with the vgacap Python package, run
     through OUR cutter (tthfv.frames.cut_frames); our frame hashes against the simulated reference's.

The vgacap project is used only as a program and a library loaded from a path given on the command
line; nothing of it is copied here. The stream must already be decompressed (gzip -d).

Usage:
  uv run python scripts/first_batch/crosscheck_hardware_capture.py --stream bars.vgacap \\
      --vgacap-python PATH/TO/vgacap/python --ppm-dir DIR_WITH_f-0000.ppm --reference-dir WORK/reference/calibration/tt_um_vgacal_bars
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from tthfv import frames


def read_ppm(path: Path) -> np.ndarray:
    data = path.read_bytes()
    assert data.startswith(b"P6"), path
    fields, pos = [], 2
    while len(fields) < 3:
        while data[pos : pos + 1].isspace():
            pos += 1
        start = pos
        while not data[pos : pos + 1].isspace():
            pos += 1
        fields.append(int(data[start:pos]))
    w, h, _ = fields
    return np.frombuffer(data, np.uint8, w * h * 3, pos + 1).reshape(h, w, 3)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stream", type=Path, required=True)
    ap.add_argument("--vgacap-python", type=Path, required=True)
    ap.add_argument("--ppm-dir", type=Path, required=True)
    ap.add_argument("--reference-dir", type=Path, required=True)
    a = ap.parse_args()

    ref = json.loads((a.reference_dir / "reference.json").read_text())
    ours = np.asarray(Image.open(a.reference_dir / "frame-000.png").convert("RGB"))
    ref_hashes = {f["hash"] for f in ref["frames"]}
    print(
        f"simulated reference: {ref['id']} {ref['mode']} frames={ref['frames_complete']} "
        f"distinct frame hashes={sorted(ref_hashes)}"
    )

    print("1. frames reconstructed by vgacap-frames vs our frame-000.png")
    ppms = sorted(a.ppm_dir.glob("*.ppm"))
    same = 0
    for p in ppms:
        img = read_ppm(p)
        n = int(np.any(img != ours, axis=-1).sum()) if img.shape == ours.shape else -1
        same += n == 0
        print(
            f"   {p.name}: {img.shape[1]}x{img.shape[0]} differing pixels: {n if n >= 0 else 'size differs'}"
        )
    print(f"   {same} of {len(ppms)} identical")

    print("2. raw hardware samples through our cutter")
    sys.path.insert(0, str(a.vgacap_python))
    from vgacap import stream

    header, items = stream.read_stream(a.stream.read_bytes())
    print(
        f"   header: bits={header.sample_bits} clock_hz={header.clock_hz} map={header.signal_map} "
        f"flags={header.flags} desc={header.desc!r}"
    )
    runs = [it for it in items if it[0] == "run"]
    samples = np.repeat(
        np.array([r[1] for r in runs], dtype=np.uint16), np.array([r[2] for r in runs], dtype=np.int64)
    )
    assert samples.max() < 256
    samples = samples.astype(np.uint8)
    print(
        f"   {len(samples)} samples, {len(runs)} runs; time chunks: {[it[1:] for it in items if it[0] == 'time']}"
    )
    fs = frames.cut_frames(samples)
    print(
        f"   our cutter: mode={fs.mode} line_clocks={fs.line_clocks} lines={fs.lines} "
        f"hsync_active_low={fs.hsync_active_low} vsync_active_low={fs.vsync_active_low} "
        f"frames={len(fs.frames)} partial_dropped={fs.partial_frames_dropped}"
    )
    equal = 0
    for f in fs.frames:
        h = f"{f.hash:016x}"
        equal += h in ref_hashes
        print(
            f"   frame {f.index} start_clock={f.start_clock} hash={h} in simulated reference hashes: {h in ref_hashes}"
        )
    print(f"   {equal} of {len(fs.frames)} frame hashes equal the simulated reference")
    return 0 if equal == len(fs.frames) and same == len(ppms) else 1


if __name__ == "__main__":
    sys.exit(main())

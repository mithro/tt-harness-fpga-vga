# SPDX-License-Identifier: Apache-2.0
"""Run the tt_um_vgacal_modes reference simulation with a different ui_in and compare the result.

The reference stage always drives ui_in 0 (mode 0, 640x480). The design picks its timing from
ui_in[1:0], and a hardware capture of it was made with ui_in 1 (800x600@60, positive syncs). This
runs the already compiled testbench (obj/Vtop) by hand with --ui-in, cuts the dump with our cutter
and compares frame 0 with the given PNG (the hardware frame) and with the vgacal reference
renderer's render.modes(ui_in). It writes only under --scratch.

Usage:
  uv run python scripts/first_batch/compare_modes_ui_in.py --reference-dir WORK/reference/calibration/tt_um_vgacal_modes \\
      --scratch DIR --ui-in 1 --hardware-png PATH/modes-frame.png --render-py PATH/tools/render.py
"""

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from tthfv import frames


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reference-dir", type=Path, required=True)
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument("--ui-in", type=int, required=True)
    ap.add_argument("--clocks", type=int, default=4_000_000)
    ap.add_argument("--hardware-png", type=Path, required=True)
    ap.add_argument("--render-py", type=Path, required=True)
    a = ap.parse_args()
    a.scratch.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(a.reference_dir / "obj" / "Vtop"),
        "--clocks",
        str(a.clocks),
        "--ui-in",
        str(a.ui_in),
        "--uio-in",
        "0",
        "--out",
        str(a.scratch),
    ]
    with (a.scratch / "sim.log").open("w") as f:
        rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, check=False).returncode
    print(f"simulation exit {rc}")
    samples = np.fromfile(a.scratch / "uo_out.bin", dtype=np.uint8)
    fs = frames.cut_frames(samples)
    print(
        f"ui_in={a.ui_in}: mode={fs.mode} line_clocks={fs.line_clocks} lines={fs.lines} "
        f"hsync_active_low={fs.hsync_active_low} vsync_active_low={fs.vsync_active_low} frames={len(fs.frames)}"
    )
    if not fs.frames:
        return 1
    f0 = fs.frames[0]
    ours = np.frombuffer(f0.rgb888, np.uint8).reshape(f0.height, f0.width, 3)
    spec = importlib.util.spec_from_file_location("vgacal_render", a.render_py)
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)
    ok = True
    for label, other in (
        ("hardware frame", np.asarray(Image.open(a.hardware_png).convert("RGB"))),
        ("render.modes(ui_in)", render.to_rgb(render.modes(a.ui_in))),
    ):
        if other.shape != ours.shape:
            print(
                f"frame 0 {ours.shape[1]}x{ours.shape[0]} vs {label} {other.shape[1]}x{other.shape[0]}: SIZE DIFFERS"
            )
            ok = False
            continue
        n = int(np.any(ours != other, axis=-1).sum())
        print(f"frame 0 {ours.shape[1]}x{ours.shape[0]} vs {label}: differing pixels {n}")
        ok &= n == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

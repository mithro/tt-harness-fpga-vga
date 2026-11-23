# SPDX-License-Identifier: Apache-2.0
"""Compare each calibration design's simulated frames with two kinds of reference picture.

  1. The reference renderer of the calibration designs (a Python module, tools/render.py of the
     tt-vga-testpatterns project, loaded by path and used as a program: nothing is copied).
  2. The frame a real board produced for the same design (PNG files from the tt-vga-capture
     calibration-loop results).

For every comparison it prints the two sizes, whether the pictures are identical, and if not the
number of differing pixels and the best whole-pixel offset (searched within +-OFFSET_RANGE).

Usage:
  uv run python scripts/first_batch/compare_calibration.py \\
      --reference-dir WORK/reference/calibration --render-py PATH/TO/tools/render.py \\
      --hardware-dir PATH/TO/2026-09-15-calibration-loop-fpga1
"""

import argparse
import importlib.util
import sys
from pathlib import Path

import numpy as np
from PIL import Image

OFFSET_RANGE = 8
DESIGNS = ("bars", "grid", "counter", "prbs", "modes")


def load_png(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"))


def load_render(path: Path):
    spec = importlib.util.spec_from_file_location("vgacal_render", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def best_offset(a: np.ndarray, b: np.ndarray) -> tuple[tuple[int, int], int, int]:
    """(dy, dx) with the fewest differing pixels over the overlap of a and b shifted by it,
    that count and the overlap size. A shift (dy, dx) compares a[y, x] with b[y+dy, x+dx]."""
    best = None
    for dy in range(-OFFSET_RANGE, OFFSET_RANGE + 1):
        for dx in range(-OFFSET_RANGE, OFFSET_RANGE + 1):
            ya, yb = max(0, -dy), max(0, dy)
            xa, xb = max(0, -dx), max(0, dx)
            h = min(a.shape[0] - ya, b.shape[0] - yb)
            w = min(a.shape[1] - xa, b.shape[1] - xb)
            if h <= 0 or w <= 0:
                continue
            d = int(np.any(a[ya : ya + h, xa : xa + w] != b[yb : yb + h, xb : xb + w], axis=-1).sum())
            if best is None or d < best[1]:
                best = ((dy, dx), d, h * w)
    return best


def compare(label: str, ours: np.ndarray, theirs: np.ndarray) -> bool:
    print(
        f"  {label}: ours {ours.shape[1]}x{ours.shape[0]}, reference {theirs.shape[1]}x{theirs.shape[0]}",
        end="",
    )
    if ours.shape != theirs.shape:
        print(" -> SIZE DIFFERS")
        return False
    n = int(np.any(ours != theirs, axis=-1).sum())
    if n == 0:
        print(f" -> identical ({ours.shape[0] * ours.shape[1]} pixels)")
        return True
    off, d, overlap = best_offset(ours, theirs)
    print(f" -> {n} pixels differ; best offset (dy,dx)={off} leaves {d} of {overlap}")
    return False


def decode_counter(rgb: np.ndarray, blocks: int = 8) -> int:
    """The 8-bit frame number drawn as `blocks` blocks across row 0 (white = 1, blue = 0), MSB first."""
    w = rgb.shape[1]
    value = 0
    for i in range(blocks):
        px = rgb[0, i * (w // blocks) + (w // blocks) // 2]
        value = (value << 1) | int(tuple(px) == (255, 255, 255))
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reference-dir", type=Path, required=True, help="WORK/reference/calibration")
    ap.add_argument("--render-py", type=Path, required=True, help="tt-vga-testpatterns tools/render.py")
    ap.add_argument("--hardware-dir", type=Path, required=True, help="directory with <name>-frame.png")
    a = ap.parse_args()
    render = load_render(a.render_py)
    failures = 0
    for name in DESIGNS:
        d = a.reference_dir / f"tt_um_vgacal_{name}"
        frames = sorted(d.glob("frame-*.png"))
        print(f"{name}: {len(frames)} frames in {d.name}")
        if not frames:
            failures += 1
            continue
        f0 = load_png(frames[0])
        hw = load_png(a.hardware_dir / f"{name}-frame.png")
        # 1. the reference renderer
        if name == "bars":
            ok = compare("frame 0 vs renderer", f0, render.to_rgb(render.bars()))
        elif name == "grid":
            ok = compare("frame 0 vs renderer", f0, render.to_rgb(render.grid()))
        elif name == "modes":
            ok = compare("frame 0 vs renderer (ui_in=0, mode 0)", f0, render.to_rgb(render.modes(0)))
        else:
            fn = render.counter if name == "counter" else render.prbs
            ok = True
            for k, fp in enumerate(frames):
                got = load_png(fp)
                c = decode_counter(got)
                print(f"  frame {k}: counter decoded from the picture = {c}")
                ok &= compare(f"frame {k} vs renderer for counter {c}", got, render.to_rgb(fn(c)))
        failures += not ok
        # 2. the real board's frame
        if name in ("counter", "prbs"):
            print(
                f"  hardware frame decodes to counter {decode_counter(hw)}; comparing every simulated frame"
            )
            hits = []
            for k, fp in enumerate(frames):
                same = load_png(fp).shape == hw.shape and not np.any(load_png(fp) != hw)
                hits.append(same)
            print(
                f"  simulated frames identical to the hardware frame: {[k for k, h in enumerate(hits) if h]}"
            )
            if not any(hits):
                # report the closest by counter for the record
                k = next(
                    (k for k, fp in enumerate(frames) if decode_counter(load_png(fp)) == decode_counter(hw)),
                    None,
                )
                if k is None:
                    print(
                        "  (no simulated frame carries that counter value, so no direct comparison is possible)"
                    )
                else:
                    compare(f"frame {k} vs hardware (same counter)", load_png(frames[k]), hw)
        else:
            compare("frame 0 vs hardware frame", f0, hw)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

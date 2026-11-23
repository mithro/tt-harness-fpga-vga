# SPDX-License-Identifier: Apache-2.0
"""simulate -> cut_frames -> write_reference with the real Verilator, on tiny designs (fetch-less)."""

import gzip
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from test_reference import COUNTER, make

from tthfv import frames, paths, reference, tools

DATA = Path(__file__).parent / "data"

try:
    tools.tool("verilator")
    _MISSING = ""
except Exception as e:  # noqa: BLE001 - any failure to locate the tool means skip
    _MISSING = f"verilator not available: {e}"

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]


@pytest.fixture(autouse=True)
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    return tmp_path / "work"


def test_counter_design_is_no_sync(tmp_path):
    d, fr = make(tmp_path, COUNTER)
    sim = reference.simulate(d, fr, clocks=20_000)
    assert sim.status == "ok", sim.log.read_text()
    cut = frames.cut_frames(frames.load_dump(sim.dump))
    assert cut.frames == [] and cut.mode is None
    j = json.loads(frames.write_reference(d, sim, cut).read_text())
    assert j["frames"] == [] and j["mode"] is None and j["frames_complete"] == 0


def test_synthetic_vga_design_end_to_end(tmp_path):
    text = (DATA / "tt_um_synth_vga.v").read_text()
    d, fr = make(tmp_path, text, name="tt_um_synth_vga")
    sim = reference.simulate(d, fr, clocks=1_300_000)
    assert sim.status == "ok", sim.log.read_text()
    cut = frames.cut_frames(frames.load_dump(sim.dump))
    assert cut.mode == "640x480@60"
    assert (cut.hsync_active_low, cut.vsync_active_low) == (True, True)
    assert len(cut.frames) >= 2
    assert cut.frames[1].start_clock - cut.frames[0].start_clock == 420_000
    # The design's pattern at pixel (0, 0) and (639, 479): r = x[6:5], g = y[5:4], b = x[8:7] ^ y[8:7]
    px = np.frombuffer(cut.frames[0].rgb888, np.uint8).reshape(480, 640, 3)
    assert tuple(px[0, 0]) == (0, 0, 0)
    x, y = 639, 479
    assert tuple(px[479, 639]) == (
        85 * ((x >> 5) & 3),
        85 * ((y >> 4) & 3),
        85 * (((x >> 7) & 3) ^ ((y >> 7) & 3)),
    )
    assert cut.frames[0].hash == cut.frames[1].hash

    ref = frames.write_reference(d, sim, cut, n=2)
    out = ref.parent
    j = json.loads(ref.read_text())
    raw = gzip.decompress((out / j["dump"]).read_bytes())
    assert len(raw) == 1_300_000
    assert hashlib.sha256(raw).hexdigest() == j["dump_sha256"]
    assert j["clocks"] == 1_300_000 and j["mode"] == "640x480@60"
    assert len(j["frames"]) == 2
    for f in j["frames"]:
        assert Image.open(out / f["png"]).size == (640, 480)

# SPDX-License-Identifier: Apache-2.0
"""Tests that run the real pinned yosys, nextpnr-ice40 and icepack. Skipped when they are absent."""

import dataclasses
import subprocess
from pathlib import Path

import pytest

from tthfv import build, fetch, paths, tools
from tthfv.manifest import Design

try:
    for _name in ("yosys", "nextpnr-ice40", "icepack", "verilator"):
        tools.tool(_name)
    _MISSING = ""
except Exception as e:  # noqa: BLE001 - any failure to locate a tool means skip
    _MISSING = f"toolchain not available: {e}"

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]


def yosys(tmp_path, defs):
    v = tmp_path / "t.v"
    v.write_text("module tt_um_x(output [7:0] o);\n  assign o = `VAL;\nendmodule\n")
    script = build._yosys_script([v], [tmp_path], tmp_path / "o.json", sv=True, synth="default", defs=defs)
    reads = script.splitlines()[:-2]  # everything but the synth_ice40 and stat lines
    ys = tmp_path / "t.ys"
    ys.write_text("\n".join([*reads, "hierarchy -top tt_um_x", "write_verilog -noattr out.v"]) + "\n")
    done = subprocess.run(
        [str(tools.tool("yosys")), "-q", "-s", str(ys)],
        cwd=tmp_path, env=tools.env(), capture_output=True, text=True, check=False, timeout=120,
    )  # fmt: skip
    return done, tmp_path / "out.v"


def test_a_define_given_to_yosys_changes_the_netlist(tmp_path):
    ok, out = yosys(tmp_path, ("VAL=8'h5A",))
    assert ok.returncode == 0, ok.stderr
    assert "8'h5a" in out.read_text().lower()


def test_without_the_define_yosys_fails(tmp_path):
    bad, _out = yosys(tmp_path, ())
    assert bad.returncode != 0


def test_the_synthetic_vga_design_builds_with_the_real_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    src = tmp_path / "repo" / "src"
    src.mkdir(parents=True)
    (src / "tt_um_synth_vga.v").write_text((Path(__file__).parent / "data" / "tt_um_synth_vga.v").read_text())
    base = Design(
        id="tt99/tt_um_synth_vga", shuttle="tt99", macro="tt_um_synth_vga", title="", author="", repo="",
        commit="0" * 40, top_module="tt_um_synth_vga", source_files=("tt_um_synth_vga.v",),
        clock_hz=25_175_000, clock_declared=True, pinout={}, pmods=(), tiles="1x1", render_verdict=None,
        override={}, patch=None, qspi=None, ui_in=0, uio_in=0, scripted=False, candidate=False,
    )  # fmt: skip
    d = dataclasses.replace(base)
    fetched = fetch.resolve(d, tmp_path / "repo", tmp_path / "repo", tmp_path / "fetch.log")
    assert fetched.status == "ok", fetched.reason
    r = build.build(d, fetched, max_attempts=2)
    assert r.status in ("ok", "timing-fail"), r.reason
    data = (paths.design_dir("build", d.id) / r.bitstream).read_bytes()
    assert len(data) == 104_090
    assert data[4:8] == bytes([0x7E, 0xAA, 0x99, 0x7E])
    assert r.clocks  # nextpnr reported at least one clock

# SPDX-License-Identifier: Apache-2.0
"""Tests that run the real pinned yosys, nextpnr-ice40 and icepack. Skipped when they are absent."""

import subprocess

import pytest

from tthfv import build, tools

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
    script = build._yosys_script(
        [v], [tmp_path], tmp_path / "o.json", sv=True, synth="default", defs=defs
    )
    reads = script.splitlines()[:-1]  # everything but the synth_ice40 line
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

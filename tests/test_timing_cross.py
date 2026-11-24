# SPDX-License-Identifier: Apache-2.0
"""Paths between two different clock domains are judged too: nextpnr reports them under `critical_paths` and
prints a FAIL for them, but they are not in `fmax`, which has one figure per clock. Real toolchain."""

import json

import pytest
from test_timing_honest import _MISSING, PORTS, build_verilog

from tthfv import paths

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]

# Source registers on clk, destination registers on a gated or LUT copy of it: both clocks' own fmax pass, but
# the 24x24 multiply-accumulate between the domains does not fit one period.
CROSS = """module tt_um_x %(ports)s
  wire gclk = %(gate)s;
  reg [23:0] a, b;
  reg [47:0] acc;
  always @(posedge clk) begin
    if (!rst_n) begin a <= 24'd1; b <= 24'd3; end
    else begin a <= a + {16'd0, ui_in}; b <= b ^ {16'd0, uio_in}; end
  end
  always @(posedge gclk) acc <= acc + a * b;
  assign uo_out = acc[47:40];
  assign uio_out = 8'd0;
  assign uio_oe = 8'd0;
endmodule
"""

# Only I/O registers: the report has no clock-to-clock path at all and an empty fmax object may be genuine.
PASS_THROUGH = """module tt_um_x %(ports)s
  reg [7:0] q;
  always @(posedge clk) q <= ui_in;
  assign uo_out = q;
  assign uio_out = 8'd0;
  assign uio_oe = 8'd0;
endmodule
"""


@pytest.fixture(autouse=True)
def _work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")


@pytest.mark.parametrize("gate", ["clk & ui_in[7]", "clk ^ ui_in[7]"], ids=["and", "xor"])
def test_a_slow_path_between_two_clock_domains_is_a_timing_fail(tmp_path, gate):
    r = build_verilog(tmp_path, CROSS % {"ports": PORTS, "gate": gate})
    assert all(c["pass"] for c in r.clocks.values()), r.clocks  # each clock alone is fast enough
    assert r.status == "timing-fail", (r.status, r.reason)
    assert "posedge clk" in r.reason and "gclk" in r.reason and "ns" in r.reason
    (bad,) = [c for c in r.cross_paths if not c["pass"]]
    assert bad["delay_ns"] > bad["limit_ns"] > 39 and bad["from"].startswith("posedge clk")
    on_disk = json.loads((paths.design_dir("build", "tt99/tt_um_x") / "build.json").read_text())
    assert on_disk["cross_paths"] == r.cross_paths


def test_a_design_with_only_io_registers_is_ok_and_says_it_has_no_clock_paths(tmp_path):
    r = build_verilog(tmp_path, PASS_THROUGH % {"ports": PORTS})
    assert r.status == "ok", r.reason
    assert r.clocks == {} and r.cross_paths == [] and "no clock-to-clock path" in r.timing_note

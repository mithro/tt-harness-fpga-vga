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


# Edges and domains in every combination: registers a, b launch on `se` of `sc`, the accumulator captures on `de`
# of `dc`; gclk is clk gated by an input, so it is another clock to nextpnr.
EDGES = """module tt_um_x %(ports)s
  wire gclk = clk & ui_in[7];
  reg [%(w)d-1:0] a, b;
  reg [2*%(w)d-1:0] acc;
  always @(%(se)s %(sc)s) begin
    if (!rst_n) begin a <= 1; b <= 3; end
    else begin a <= a + %(ui)s; b <= b ^ %(uio)s; end
  end
  always @(%(de)s %(dc)s) acc <= acc + %(expr)s;
  assign uo_out = acc[2*%(w)d-1:2*%(w)d-8];
  assign uio_out = 8'd0;
  assign uio_oe = 8'd0;
endmodule
"""


def edges(w, se, sc, de, dc, expr):
    pad = f"{{{w - 8}'d0, %s}}" if w > 8 else "%s"
    return EDGES % {
        "ports": PORTS, "w": w, "se": se, "sc": sc, "de": de, "dc": dc, "expr": expr,
        "ui": pad % "ui_in", "uio": pad % "uio_in",
    }  # fmt: skip


def test_a_negative_clock_skew_segment_does_not_make_a_good_design_unusable(tmp_path):
    # a gated clock back into clk: nextpnr reports a clk-to-clk segment of about -1.2 ns on the path
    r = build_verilog(tmp_path, edges(8, "posedge", "gclk", "posedge", "clk", "a + b"))
    assert r.status == "ok", (r.status, r.reason)
    assert r.cross_paths and all(c["pass"] for c in r.cross_paths)


def test_a_slow_path_from_a_gated_clock_back_into_clk_is_a_timing_fail_not_a_pnr_fail(tmp_path):
    r = build_verilog(tmp_path, edges(24, "posedge", "gclk", "posedge", "clk", "a * b"))
    assert r.status == "timing-fail", (r.status, r.reason)
    (bad,) = [c for c in r.cross_paths if not c["pass"]]
    assert bad["from"].startswith("posedge user_project.gclk") and bad["to"].startswith("posedge clk")
    assert "judged against 39.72 ns (one period: the same kind of edge of two clocks" in r.reason
    assert "nextpnr also marks" not in r.reason  # nextpnr marks no clock failing here


@pytest.mark.parametrize(
    "case",
    [(12, "posedge", "clk", "negedge", "gclk"), (16, "negedge", "clk", "posedge", "gclk")],
    ids=["pos-clk-to-neg-gclk", "neg-clk-to-pos-gclk"],
)
def test_opposite_edges_of_different_clocks_get_half_a_period(tmp_path, case):
    w, se, sc, de, dc = case
    r = build_verilog(tmp_path, edges(w, se, sc, de, dc, "a * b"))  # 31-37 ns: inside one period, over half
    assert r.status == "timing-fail", (r.status, r.reason)
    (bad,) = [c for c in r.cross_paths if not c["pass"]]
    assert 19 < bad["limit_ns"] < 20 and bad["limit_ns"] < bad["delay_ns"] < 39.7
    assert "half a period: launch and capture are on different edges" in r.reason


def test_the_same_edges_of_different_clocks_keep_a_full_period(tmp_path):
    r = build_verilog(tmp_path, edges(12, "posedge", "clk", "posedge", "gclk", "a * b"))
    assert r.status == "ok", (r.status, r.reason)
    (c,) = r.cross_paths
    assert c["pass"] and c["limit_ns"] > 39 and c["delay_ns"] > 19.9


# A chain of LUT buffers on one clock makes nextpnr report a large clock skew (a clk-to-clk segment of about -41 ns on
# a path from the delayed clock back into clk). In hardware the data arrives after the next capture edge.
SKEW = """module tt_um_x %(ports)s
  wire [32:0] w;
  assign w[0] = clk;
  genvar g;
  generate for (g = 0; g < 32; g = g + 1) begin : buffers
    (* keep *) SB_LUT4 #(.LUT_INIT(16'hAAAA)) b (.O(w[g+1]), .I0(w[g]), .I1(1'b0), .I2(1'b0), .I3(1'b0));
  end endgenerate
  wire cclk = w[32];
  reg [%(w)d-1:0] a, b;
  reg [2*%(w)d-1:0] acc;
  always @(posedge %(sc)s) begin
    if (!rst_n) begin a <= 1; b <= 3; end
    else begin a <= a + %(ui)s; b <= b ^ %(uio)s; end
  end
  always @(posedge %(dc)s) acc <= acc + %(expr)s;
  assign uo_out = acc[2*%(w)d-1:2*%(w)d-8];
  assign uio_out = 8'd0;
  assign uio_oe = 8'd0;
endmodule
"""


def skew(w, sc, dc, expr):
    pad = f"{{{w - 8}'d0, %s}}" if w > 8 else "%s"
    return SKEW % {
        "ports": PORTS, "w": w, "sc": sc, "dc": dc, "expr": expr, "ui": pad % "ui_in", "uio": pad % "uio_in",
    }  # fmt: skip


def test_manufactured_clock_skew_cannot_make_a_slow_path_look_fast(tmp_path):
    r = build_verilog(
        tmp_path, skew(24, "cclk", "clk", "a * b")
    )  # nextpnr prints under 1 ns for 41.8 ns of logic
    assert r.status == "timing-fail", (r.status, r.reason)
    (bad,) = [c for c in r.cross_paths if not c["pass"]]
    assert bad["nextpnr_delay_ns"] < 5 and bad["delay_ns"] > 39.7 and bad["failed_by"] == "path-rule"
    assert "negative clock skew not credited" in r.reason and "nextpnr prints" in r.reason


def test_a_small_path_behind_the_same_skew_is_still_ok(tmp_path):
    r = build_verilog(tmp_path, skew(8, "cclk", "clk", "a + b"))
    assert r.status == "ok", (r.status, r.reason)
    (c,) = r.cross_paths
    assert c["nextpnr_delay_ns"] < 0 < c["delay_ns"] < 39.7 and c["pass"]


def test_a_design_whose_only_clock_goes_through_luts_has_no_skew_to_hide_behind(tmp_path):
    # every register on the buffered clock: one clock domain, judged by fmax, no clk-to-clk segment in its paths
    r = build_verilog(tmp_path, skew(24, "cclk", "cclk", "a * b"))
    assert r.status == "timing-fail" and r.cross_paths == [], (r.status, r.reason)
    assert len(r.clocks) == 1 and not next(iter(r.clocks.values()))["pass"]

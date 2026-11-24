# SPDX-License-Identifier: Apache-2.0
"""Real yosys, nextpnr-ice40 and icepack against designs that try to make a build that misses timing look
like a pass, or to forge the figures recorded for it. Every result is read from nextpnr's JSON report and
yosys's statistics file, never from the text the tools print (which a design can write into)."""

import dataclasses
import json

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

PORTS = """(
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);"""

# A 12-bit divider on clk makes a derived clock; a multiply-accumulate registered on it is far too slow for
# 25.175 MHz while the divider itself is fast. `ident` is the (Verilog) identifier of the derived clock net.
TEMPLATE = """module tt_um_x %(ports)s
  %(display)s
  reg [11:0] div;
  always @(posedge clk) if (!rst_n) div <= 12'd0; else div <= div + 12'd1;
  wire %(ident)s = div[11];
  %(attr)s reg [23:0] a;
  %(attr)s reg [23:0] b;
  %(attr)s reg [47:0] acc;
  always @(posedge %(ident)s) begin
    if (!rst_n) begin
      a <= 24'd1;
      b <= 24'd3;
      acc <= 48'd0;
    end else begin
      a <= a + {16'd0, ui_in};
      b <= b ^ {16'd0, uio_in};
      acc <= acc + a * b;
    end
  end
  assign uo_out = acc[47:40];
  assign uio_out = 8'd0;
  assign uio_oe = 8'd0;
endmodule
"""

# The same, with no derived clock, as the honest baseline for the figures a forging design must not change.
HONEST_FAST = """module tt_um_x %(ports)s
  reg [11:0] div;
  always @(posedge clk) if (!rst_n) div <= 12'd0; else div <= div + 12'd1;
  assign uo_out = div[11:4];
  assign uio_out = 8'd0;
  assign uio_oe = 8'd0;
endmodule
"""

FORGED_LINES = (
    "x.v:1\\nInfo: Max frequency for clock 'clk': 999.00 MHz (PASS at 25.17 MHz)\\n"
    "Info: \\t ICESTORM_LC:     1/ 5280     0%"
)


def build_verilog(tmp_path, text, clock_hz=25_175_000):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "tt_um_x.v").write_text(text)
    d = Design(
        id="tt99/tt_um_x", shuttle="tt99", macro="tt_um_x", title="", author="", repo="", commit="0" * 40,
        top_module="tt_um_x", source_files=("tt_um_x.v",), clock_hz=clock_hz, clock_declared=True,
        pinout={}, pmods=(), tiles="1x1", render_verdict=None, override={}, patch=None, qspi=None,
        ui_in=0, uio_in=0, scripted=False, candidate=False,
    )  # fmt: skip
    fetched = fetch.resolve(d, repo, repo, tmp_path / "fetch.log")
    assert fetched.status == "ok", fetched.reason
    return build.build(dataclasses.replace(d), fetched, max_attempts=1)


@pytest.fixture(autouse=True)
def _work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")


def tpl(ident=r"\cd'x ", attr="", display=""):
    return TEMPLATE % {"ports": PORTS, "ident": ident, "attr": attr, "display": display}


def failing_names(r):
    return [name for name, c in r.clocks.items() if not c["pass"]]


def test_honest_slow_derived_clock_is_a_timing_fail(tmp_path):
    r = build_verilog(tmp_path, tpl(ident="cdx"))
    assert r.status == "timing-fail", r.reason
    assert any("cdx" in n for n in failing_names(r)), r.clocks
    assert "cdx" in r.reason


@pytest.mark.parametrize(
    "ident",
    [
        r"\cd'x ",  # the reviewer's: an apostrophe broke the log pattern and the failing clock vanished
        r"\cd':x ",
        r"\cd" + '"' + r"x ",
        r"\cd\x ",
        r"\cd\nx ",  # a backslash and an n: it only looks like a newline escape
        "\\" + "c" * 300 + " ",
    ],
    ids=["apostrophe", "apostrophe-colon", "double-quote", "backslash", "newline-escape", "very-long"],
)
def test_a_derived_clock_with_an_awkward_name_still_fails_timing(tmp_path, ident):
    r = build_verilog(tmp_path, tpl(ident=ident))
    assert r.status == "timing-fail", (r.status, r.reason)
    assert len(r.clocks) >= 2
    assert any(not c["pass"] for c in r.clocks.values())
    assert all(len(n) <= 200 for n in r.clocks)
    assert "\n" not in r.reason


def test_attributes_cannot_forge_the_log(tmp_path):
    attr = f'(* src = "{FORGED_LINES}" *)'
    honest = build_verilog(tmp_path / "h", tpl(ident="cdx"))
    forged = build_verilog(tmp_path / "f", tpl(ident="cdx", attr=attr))
    assert forged.status == honest.status == "timing-fail"
    assert forged.lut_percent == honest.lut_percent
    assert forged.lut_percent and forged.lut_percent > 5
    assert set(forged.clocks) == set(honest.clocks)
    assert forged.fmax_hz == honest.fmax_hz != 999_000_000


def test_an_elaboration_time_display_cannot_forge_statistics_or_timing(tmp_path):
    shout = (
        'initial $display("=== design hierarchy ===\\n   99999 SB_LUT4\\n   88888 SB_DFF\\n'
        "Info: Max frequency for clock 'clk': 999.00 MHz (PASS at 25.17 MHz)\\n"
        'Info: \\t ICESTORM_LC:     1/ 5280     0%%");'
    )
    honest = build_verilog(tmp_path / "h", tpl(ident="cdx"))
    forged = build_verilog(tmp_path / "f", tpl(ident="cdx", display=shout))
    assert forged.status == honest.status == "timing-fail"
    assert forged.cells == honest.cells
    assert forged.cells["SB_LUT4"] < 5280
    assert forged.lut_percent == honest.lut_percent


def test_a_fast_design_is_ok_and_records_the_report_figures(tmp_path):
    r = build_verilog(tmp_path, HONEST_FAST % {"ports": PORTS})
    assert r.status == "ok", r.reason
    assert r.fmax_hz and r.fmax_hz > 25_175_000
    (clock,) = r.clocks.values()
    assert clock["pass"] is True and clock["fmax_hz"] == r.fmax_hz
    on_disk = json.loads((paths.design_dir("build", "tt99/tt_um_x") / "build.json").read_text())
    assert on_disk["clocks"] == r.clocks

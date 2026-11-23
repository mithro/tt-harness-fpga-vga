# SPDX-License-Identifier: Apache-2.0
"""The testbench models the pad: a design that reads its own uio outputs sees them, as on the FPGA."""

import pytest
from test_reference import make

from tthfv import paths, reference, tools

try:
    tools.tool("verilator")
    _MISSING = ""
except Exception as e:  # noqa: BLE001 - any failure to locate the tool means skip
    _MISSING = f"verilator not available: {e}"

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]

# uio[3:0] are outputs showing a counter that starts counting when reset ends; uio[7:4] are inputs.
HEADER = """module tt_um_readback (
    input  wire [7:0] ui_in, output wire [7:0] uo_out,
    input  wire [7:0] uio_in, output wire [7:0] uio_out, output wire [7:0] uio_oe,
    input  wire ena, input  wire clk, input  wire rst_n
);
  reg [7:0] count;
  always @(posedge clk) if (!rst_n) count <= 0; else count <= count + 1;
  assign uio_out = {4'b0, count[3:0]};
  assign uio_oe  = 8'h0F;
"""
REGISTERED = (
    HEADER
    + """
  reg [7:0] seen;
  always @(posedge clk) seen <= uio_in;
  assign uo_out = seen;
endmodule
"""
)
COMBINATIONAL = (
    HEADER
    + """
  assign uo_out = uio_in;
endmodule
"""
)


@pytest.fixture(autouse=True)
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")


def counter(i: int) -> int:
    """count after the rising edge of clock i: reset is released before clock 10."""
    return max(0, i - 9)


def run(tmp_path, text, uio_in=0xA5, clocks=40):
    d, fr = make(tmp_path, text, name="tt_um_readback", uio_in=uio_in)
    r = reference.simulate(d, fr, clocks=clocks)
    assert r.status == "ok", r.log.read_text()
    return r.dump.read_bytes(), r.uio.read_bytes()


def test_a_registered_reader_sees_the_outputs_driven_after_the_previous_edge(tmp_path):
    uo, uio = run(tmp_path, REGISTERED)
    # pad = (oe & out) | (~oe & fixed): the low nibble is the design's own output, the high one the fixed input
    expected = [0xA0 | (counter(i - 1) & 0xF if i else 0) for i in range(40)]
    assert list(uo) == expected
    assert list(uio[0::2]) == [counter(i) & 0xF for i in range(40)]  # uio_out after each edge
    assert set(uio[1::2]) == {0x0F}  # uio_oe


def test_a_combinational_reader_sees_the_settled_pad_in_the_same_clock(tmp_path):
    uo, _uio = run(tmp_path, COMBINATIONAL)
    assert list(uo) == [0xA0 | (counter(i) & 0xF) for i in range(40)]


def test_the_fixed_input_is_what_undriven_pins_read(tmp_path):
    uo, _uio = run(tmp_path, COMBINATIONAL, uio_in=0x5A)
    assert list(uo) == [0x50 | (counter(i) & 0xF) for i in range(40)]


def test_a_design_that_never_drives_uio_still_sees_the_fixed_input(tmp_path):
    text = REGISTERED.replace("assign uio_oe  = 8'h0F;", "assign uio_oe  = 8'h00;")
    uo, uio = run(tmp_path, text, uio_in=0x3C)
    assert set(uo) == {0x3C}
    assert set(uio[1::2]) == {0}

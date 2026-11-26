// SPDX-License-Identifier: Apache-2.0
//
// Behavioural models of the SkyWater SKY130 high-density standard cells (sky130_fd_sc_hd) that Tiny Tapeout
// VGA designs instantiate directly in their RTL. Written for tthfv; not copied from the PDK. Each cell's
// function is the one documented by the PDK:
//   https://skywater-pdk.readthedocs.io/en/main/contents/libraries/sky130_fd_sc_hd/cells/<cell>/README.html
// and its functional model, cells/<cell>/sky130_fd_sc_hd__<cell>.functional.v, in
//   https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd
// Only the logic function is modelled: no timing, no power pins (VPWR, VGND, VPB, VNB), two-state values.
// The drive-strength suffix (_1, _4, ...) does not change a cell's function.
//
// Format (read by tthfv/pdkcells.py): every module is preceded by a line `// @cell <name> <kind>`, where kind is
// logic, delay (a buffer whose purpose on the chip is its delay) or latch (a level-sensitive storage cell,
// including the latch inside a clock gate). Nothing but such blocks follows this header.

// @cell sky130_fd_sc_hd__and2_1 logic
// and2: 2-input AND. X = A & B
module sky130_fd_sc_hd__and2_1 (input A, input B, output X);
  assign X = A & B;
endmodule

// @cell sky130_fd_sc_hd__buf_1 logic
// buf: buffer. X = A
module sky130_fd_sc_hd__buf_1 (input A, output X);
  assign X = A;
endmodule

// @cell sky130_fd_sc_hd__buf_12 logic
// buf: buffer. X = A
module sky130_fd_sc_hd__buf_12 (input A, output X);
  assign X = A;
endmodule

// @cell sky130_fd_sc_hd__clkbuf_8 logic
// clkbuf: clock tree buffer. X = A
module sky130_fd_sc_hd__clkbuf_8 (input A, output X);
  assign X = A;
endmodule

// @cell sky130_fd_sc_hd__clkbuf_16 logic
// clkbuf: clock tree buffer. X = A
module sky130_fd_sc_hd__clkbuf_16 (input A, output X);
  assign X = A;
endmodule

// @cell sky130_fd_sc_hd__clkdlybuf4s25_1 delay
// clkdlybuf4s25: clock delay buffer, 4 stages, 0.25 um length inner stage gates. X = A
module sky130_fd_sc_hd__clkdlybuf4s25_1 (input A, output X);
  assign X = A;
endmodule

// @cell sky130_fd_sc_hd__dlclkp_1 latch
// dlclkp: clock gate. A latch, transparent while CLK is low, holds GATE; GCLK = CLK & latched GATE
module sky130_fd_sc_hd__dlclkp_1 (input CLK, input GATE, output GCLK);
  reg m0;
  always @(CLK or GATE) if (!CLK) m0 = GATE;
  assign GCLK = CLK & m0;
endmodule

// @cell sky130_fd_sc_hd__dlclkp_2 latch
// dlclkp: clock gate. A latch, transparent while CLK is low, holds GATE; GCLK = CLK & latched GATE
module sky130_fd_sc_hd__dlclkp_2 (input CLK, input GATE, output GCLK);
  reg m0;
  always @(CLK or GATE) if (!CLK) m0 = GATE;
  assign GCLK = CLK & m0;
endmodule

// @cell sky130_fd_sc_hd__dlclkp_4 latch
// dlclkp: clock gate. A latch, transparent while CLK is low, holds GATE; GCLK = CLK & latched GATE
module sky130_fd_sc_hd__dlclkp_4 (input CLK, input GATE, output GCLK);
  reg m0;
  always @(CLK or GATE) if (!CLK) m0 = GATE;
  assign GCLK = CLK & m0;
endmodule

// @cell sky130_fd_sc_hd__dlxtn_1 latch
// dlxtn: delay latch, inverted enable, single output. Transparent (Q = D) while GATE_N is low, else holds
module sky130_fd_sc_hd__dlxtn_1 (input D, input GATE_N, output reg Q);
  always @(D or GATE_N) if (!GATE_N) Q = D;
endmodule

// @cell sky130_fd_sc_hd__dlxtp_1 latch
// dlxtp: delay latch, non-inverted enable, single output. Transparent (Q = D) while GATE is high, else holds
module sky130_fd_sc_hd__dlxtp_1 (input D, input GATE, output reg Q);
  always @(D or GATE) if (GATE) Q = D;
endmodule

// @cell sky130_fd_sc_hd__dlygate4sd3_1 delay
// dlygate4sd3: delay buffer, 4-stage, 0.50 um length inner stage gates. X = A
module sky130_fd_sc_hd__dlygate4sd3_1 (input A, output X);
  assign X = A;
endmodule

// @cell sky130_fd_sc_hd__fa_1 logic
// fa: full adder. SUM = A ^ B ^ CIN; COUT = (A & B) | (A & CIN) | (B & CIN)
module sky130_fd_sc_hd__fa_1 (input A, input B, input CIN, output COUT, output SUM);
  assign SUM = A ^ B ^ CIN;
  assign COUT = (A & B) | (A & CIN) | (B & CIN);
endmodule

// @cell sky130_fd_sc_hd__inv_1 logic
// inv: inverter. Y = !A
module sky130_fd_sc_hd__inv_1 (input A, output Y);
  assign Y = ~A;
endmodule

// @cell sky130_fd_sc_hd__mux4_1 logic
// mux4: 4-input multiplexer. X = S1 ? (S0 ? A3 : A2) : (S0 ? A1 : A0)
module sky130_fd_sc_hd__mux4_1 (input A0, input A1, input A2, input A3, input S0, input S1, output X);
  assign X = S1 ? (S0 ? A3 : A2) : (S0 ? A1 : A0);
endmodule

// @cell sky130_fd_sc_hd__nand4_1 logic
// nand4: 4-input NAND. Y = !(A & B & C & D)
module sky130_fd_sc_hd__nand4_1 (input A, input B, input C, input D, output Y);
  assign Y = ~(A & B & C & D);
endmodule

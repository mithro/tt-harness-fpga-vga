// SPDX-License-Identifier: Apache-2.0
//
// Behavioural models of the IHP SG13CMOS5L standard cells (sg13cmos5l_stdcell) that Tiny Tapeout VGA designs
// instantiate directly in their RTL. Written for tthfv; not copied from the PDK. Each cell's function is the one
// in the PDK's cell library Verilog, ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_stdcell.v, in
//   https://github.com/IHP-GmbH/IHP-Open-PDK
// (the same functions and port names as the SG13G2 cells of the same name). Only the logic function is
// modelled: no timing, no power pins, two-state values. The drive-strength suffix does not change a function.
//
// Format: see sky130_fd_sc_hd.v (`// @cell <name> <kind>` before every module).

// @cell sg13cmos5l_buf_8 logic
// buf: buffer. X = A
module sg13cmos5l_buf_8 (input A, output X);
  assign X = A;
endmodule

// @cell sg13cmos5l_inv_1 logic
// inv: inverter. Y = !A
module sg13cmos5l_inv_1 (input A, output Y);
  assign Y = ~A;
endmodule

// @cell sg13cmos5l_inv_2 logic
// inv: inverter. Y = !A
module sg13cmos5l_inv_2 (input A, output Y);
  assign Y = ~A;
endmodule

// @cell sg13cmos5l_inv_4 logic
// inv: inverter. Y = !A
module sg13cmos5l_inv_4 (input A, output Y);
  assign Y = ~A;
endmodule

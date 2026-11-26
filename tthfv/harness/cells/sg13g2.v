// SPDX-License-Identifier: Apache-2.0
//
// Behavioural models of the IHP SG13G2 standard cells (sg13g2_stdcell) that Tiny Tapeout VGA designs
// instantiate directly in their RTL. Written for tthfv; not copied from the PDK. Each cell's function is the one
// in the PDK's cell library Verilog, ihp-sg13g2/libs.ref/sg13g2_stdcell/verilog/sg13g2_stdcell.v, in
//   https://github.com/IHP-GmbH/IHP-Open-PDK
// Only the logic function is modelled: no timing, no power pins, two-state values. The drive-strength suffix
// does not change a cell's function.
//
// Format: see sky130_fd_sc_hd.v (`// @cell <name> <kind>` before every module).

// @cell sg13g2_inv_1 logic
// inv: inverter. Y = !A
module sg13g2_inv_1 (input A, output Y);
  assign Y = ~A;
endmodule

// SPDX-License-Identifier: Apache-2.0
//
// Behavioural models of the GlobalFoundries GF180MCU 7-track 5 V standard cells (gf180mcu_fd_sc_mcu7t5v0) that
// Tiny Tapeout VGA designs instantiate directly in their RTL. Written for tthfv; not copied from the PDK. Each
// cell's function is the one documented by the PDK:
//   https://gf180mcu-pdk.readthedocs.io/en/latest/digital/standard_cells/gf180mcu_fd_sc_mcu7t5v0/cells/<cell>/
// and its functional model in https://github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_sc_mcu7t5v0
// (cells/<cell>/gf180mcu_fd_sc_mcu7t5v0__<cell>_func.v). Only the logic function is modelled: no timing, no
// power pins (VDD, VSS), two-state values. The drive-strength suffix does not change a cell's function.
//
// Format: see sky130_fd_sc_hd.v (`// @cell <name> <kind>` before every module).

// @cell gf180mcu_fd_sc_mcu7t5v0__clkbuf_8 logic
// clkbuf: clock buffer. Z = I
module gf180mcu_fd_sc_mcu7t5v0__clkbuf_8 (input I, output Z);
  assign Z = I;
endmodule

// @cell gf180mcu_fd_sc_mcu7t5v0__inv_1 logic
// inv: inverter. ZN = !I
module gf180mcu_fd_sc_mcu7t5v0__inv_1 (input I, output ZN);
  assign ZN = ~I;
endmodule

// @cell gf180mcu_fd_sc_mcu7t5v0__inv_2 logic
// inv: inverter. ZN = !I
module gf180mcu_fd_sc_mcu7t5v0__inv_2 (input I, output ZN);
  assign ZN = ~I;
endmodule

// @cell gf180mcu_fd_sc_mcu7t5v0__inv_4 logic
// inv: inverter. ZN = !I
module gf180mcu_fd_sc_mcu7t5v0__inv_4 (input I, output ZN);
  assign ZN = ~I;
endmodule

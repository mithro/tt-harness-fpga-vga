// SPDX-License-Identifier: Apache-2.0
// Test design: an 8-bit counter on uo_out, synchronously reset to 0.
`default_nettype none
module tt_um_counter_vga (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  reg [7:0] count;
  always @(posedge clk) count <= rst_n ? count + 8'd1 : 8'd0;
  assign uo_out  = count;
  assign uio_out = {count[3:0], 4'hA};
  assign uio_oe  = uio_in;
  wire _unused = &{ena, ui_in, 1'b0};
endmodule

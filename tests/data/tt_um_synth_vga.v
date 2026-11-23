// SPDX-License-Identifier: Apache-2.0
// Test design: 640x480@60 timing generator with a simple pattern, active-low syncs.
// A line is 800 clocks: back porch 48, active 640, front porch 16, hsync low for the last 96.
// A frame is 525 lines: back porch 33, active 480, front porch 10, vsync low for the last 2.
`default_nettype none
module tt_um_synth_vga (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  reg [9:0] hc;
  reg [9:0] vc;
  always @(posedge clk) begin
    if (!rst_n) begin
      hc <= 10'd0;
      vc <= 10'd0;
    end else if (hc == 10'd799) begin
      hc <= 10'd0;
      vc <= (vc == 10'd524) ? 10'd0 : vc + 10'd1;
    end else begin
      hc <= hc + 10'd1;
    end
  end

  wire [9:0] x = hc - 10'd48;
  wire [9:0] y = vc - 10'd33;
  wire active = (hc >= 10'd48) && (hc < 10'd688) && (vc >= 10'd33) && (vc < 10'd513);
  wire hsync = ~(hc >= 10'd704);
  wire vsync = ~(vc >= 10'd523);
  wire [1:0] r = active ? x[6:5] : 2'd0;
  wire [1:0] g = active ? y[5:4] : 2'd0;
  wire [1:0] b = active ? (x[8:7] ^ y[8:7]) : 2'd0;

  assign uo_out  = {hsync, b[0], g[0], r[0], vsync, b[1], g[1], r[1]};
  assign uio_out = 8'd0;
  assign uio_oe  = 8'd0;
  wire _unused = &{ena, ui_in, uio_in, 1'b0};
endmodule

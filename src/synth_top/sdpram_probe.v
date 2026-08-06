// Synthesis probe for kohaku_sdpram: registers on every side so the reported
// path and the primitive count are the memory's own, not the testbench's.
//
// PRIM is an integer, not a string, because Vivado's -generic does not carry
// Verilog string parameters reliably, and a ternary over "distributed" /
// "block" / "ultra" pads the short ones with NULs to the width of the longest
// -- which XPM then rejects. A generate with literal strings avoids both.
//
//   .\tests\run_synth_check.ps1 -Only sdpram_probe -Generics "WIDTH:352+DEPTH:512+PRIM:1"

`default_nettype none

module sdpram_probe #(
    parameter integer WIDTH    = 352,
    parameter integer DEPTH    = 512,
    parameter integer PRIM     = 1,     // 0 distributed, 1 block, 2 ultra
    parameter integer READ_LAT = 1
)(
    input  wire                     clk,
    input  wire                     wr_en,
    input  wire [$clog2(DEPTH)-1:0] wr_addr,
    input  wire [WIDTH-1:0]         wr_data,
    input  wire                     rd_en,
    input  wire [$clog2(DEPTH)-1:0] rd_addr,
    output reg  [WIDTH-1:0]         rd_data
);
    localparam integer AW = $clog2(DEPTH);

    reg                we_r, re_r;
    reg [AW-1:0]       wa_r, ra_r;
    reg [WIDTH-1:0]    wd_r;
    wire [WIDTH-1:0]   q;

    always @(posedge clk) begin
        we_r <= wr_en;  re_r <= rd_en;
        wa_r <= wr_addr; ra_r <= rd_addr; wd_r <= wr_data;
        rd_data <= q;
    end

    generate
    if (PRIM == 0) begin : g_dist
        kohaku_sdpram #(.WIDTH(WIDTH), .DEPTH(DEPTH),
                        .MEM_PRIM("distributed"), .READ_LAT(READ_LAT)) u (
            .clk(clk), .wr_en(we_r), .wr_addr(wa_r), .wr_data(wd_r),
            .rd_en(re_r), .rd_addr(ra_r), .rd_data(q));
    end else if (PRIM == 1) begin : g_block
        kohaku_sdpram #(.WIDTH(WIDTH), .DEPTH(DEPTH),
                        .MEM_PRIM("block"), .READ_LAT(READ_LAT)) u (
            .clk(clk), .wr_en(we_r), .wr_addr(wa_r), .wr_data(wd_r),
            .rd_en(re_r), .rd_addr(ra_r), .rd_data(q));
    end else begin : g_ultra
        kohaku_sdpram #(.WIDTH(WIDTH), .DEPTH(DEPTH),
                        .MEM_PRIM("ultra"), .READ_LAT(READ_LAT)) u (
            .clk(clk), .wr_en(we_r), .wr_addr(wa_r), .wr_data(wd_r),
            .rd_en(re_r), .rd_addr(ra_r), .rd_data(q));
    end
    endgenerate

endmodule

`default_nettype wire

// Cluster with the EXACT fixed-point accumulator: the bring-up configuration.
// mx_cluster_core -> mx_acu, so mx_cluster_tb can check the whole 4x32x4
// datapath bit-for-bit with no rounding to blur a failure. A real machine pairs
// the same core with mx_acu_fp instead -- see mx_matmul_cu.v.
//
// 4 x 32 x 4 per cycle. Latency, operands to acc_valid: 17 + 2 = 19 cycles.

`default_nettype none

module mx_cluster #(
    parameter integer S     = 19,
    parameter integer ACCW  = 48,
    parameter integer MODEL = 0
)(
    input  wire         clk,
    input  wire         rst,
    input  wire         en,

    input  wire [895:0] a_in,
    input  wire [895:0] b_in,

    input  wire [31:0]  sa,
    input  wire [31:0]  sb,
    input  wire [7:0]   anchor,
    input  wire         in_valid,
    input  wire         acc_clear,

    output wire [16*ACCW-1:0] acc_out,
    output wire               acc_valid
);
    wire [383:0] part_out;
    wire         part_valid, part_first;

    mx_cluster_core #(.S(S), .MODEL(MODEL)) u_core (
        .clk(clk), .rst(rst), .en(en),
        .a_in(a_in), .b_in(b_in),
        .in_valid(in_valid), .in_first(acc_clear),
        .part_out(part_out), .part_valid(part_valid), .part_first(part_first)
    );

    mx_acu #(.ACCW(ACCW)) u_acu (
        .clk(clk), .rst(rst), .en(en),
        .part_in(part_out),
        .sa(sa), .sb(sb), .anchor(anchor),
        .in_valid(part_valid),
        .acc_clear(part_first),
        .acc_out(acc_out), .acc_valid(acc_valid)
    );

endmodule

`default_nettype wire

// mx_cluster_core under a quiescence gate. A feed-forward pipeline, 17 deep
// (operand to part_valid); docs/models.md.

`default_nettype none

module mx_cluster_core #(
    parameter integer S     = 19,
    parameter integer MODEL = 0
)(
    input  wire         clk,
    input  wire         rst,
    input  wire         en,
    input  wire [895:0] a_in,
    input  wire [895:0] b_in,
    input  wire         in_valid,
    input  wire         in_first,
    output wire [383:0] part_out,
    output wire         part_valid,
    output wire         part_first
);
    wire gclk;
    vlt_qgate #(.W(2 + 2*896 + 2), .SETTLE(32)) u_gate (
        .clk(clk), .in({rst, en, a_in, b_in, in_valid, in_first}), .idle(1'b1),
        .gclk(gclk)
    );

    mx_cluster_core__rtl #(.S(S), .MODEL(MODEL)) u_rtl (
        .clk(gclk), .rst(rst), .en(en),
        .a_in(a_in), .b_in(b_in), .in_valid(in_valid), .in_first(in_first),
        .part_out(part_out), .part_valid(part_valid), .part_first(part_first)
    );
endmodule

`default_nettype wire

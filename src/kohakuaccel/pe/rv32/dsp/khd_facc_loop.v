// khd_facc_loop -- the question tier 2 turns on, as a synthesisable loop.
//
// A float accumulate is `acc <= acc + x`, and whether that closes in ONE cycle
// decides the whole float tier: at one cycle it is tier 1's structure with a
// different adder and `vfmacc` needs no rules at all; at more, `vfmacc` needs
// rotating accumulators and every kernel author has to know it.
//
// mx_fpacc_add is the matmul cluster's UNSPLIT reference add -- correct, and
// not instantiated by the cluster, which splits the same work across three
// stages because it sits on that design's critical path with everything else.
// Standalone, in a PE whose own ceiling is ~340 MHz, it may be a different
// answer. This is the loop and nothing else: flop, add, flop.

`default_nettype none

module khd_facc_loop #(
    parameter integer MW = 14                 // S1 E7 M14, the cluster's default
)(
    input  wire              clk,
    input  wire              rst,
    input  wire              en,
    input  wire [MW+7:0]     x,
    output wire [MW+7:0]     acc_o
);
    reg [MW+7:0] x_q, acc;
    wire [MW+7:0] sum;

    mx_fpacc_add #(.MW(MW)) u_add (.a(acc), .b(x_q), .s(sum));

    always @(posedge clk) begin
        x_q <= x;
        if (rst) acc <= {(MW+8){1'b0}};
        else if (en) acc <= sum;
    end

    assign acc_o = acc;

endmodule

`default_nettype wire

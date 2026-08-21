// khd_f16_lane -- one FP16 multiply-accumulate element of the float tier.
//
// FP16 in the register, E8M15 in the datapath: the vector core's own load-edge
// idiom, and the conversion is exact in this direction so nothing is lost going
// in. `vec_alu` does the arithmetic with its operation tied to FMA, which is
// what makes the tier need no new float arithmetic at all.
//
// LATENCY IS A CONTRACT, NOT A DETAIL. `ALAT` here must equal what vec_alu
// actually is, because the accumulator above rotates its partials by exactly
// this number -- get it wrong and a write lands on a partial that has already
// been read again. vec_lanes.v derives its own copy the same way and says the
// same thing: the ALU and the delay must share one localparam or they will
// disagree.

`default_nettype none

module khd_f16_lane #(
    parameter integer PIPE_MUX = 1,
    // 1 swaps DSP48E2 for vec_dsp's behavioural model, so a simulation failure
    // is attributable to the arithmetic or to the DSP configuration, never to
    // both. Synthesis uses 0.
    parameter integer MODEL    = 0
)(
    input  wire        clk,
    input  wire        rst,

    input  wire        in_valid,
    input  wire [15:0] a,          // FP16
    input  wire [15:0] b,          // FP16
    input  wire [23:0] c,          // E8M15, the partial being accumulated into

    // The FOLD multiplies a partial -- already E8M15 -- by one, so it needs the
    // operand path without the conversion in front of it. Same lane, same
    // rounding: combining partials through a second adder would round
    // differently from accumulating into them.
    input  wire        raw_e8,
    input  wire [23:0] a_e8,

    output wire        out_valid,
    output wire [23:0] out         // E8M15
);
    // vec_alu's own depth. 14, or 15 at PIPE_MUX=1 which is what ships.
    localparam integer ALAT = 14 + ((PIPE_MUX != 0) ? 1 : 0);
    localparam [4:0]   OP_FMA = 5'd6;

    localparam [23:0] E8_ONE = {1'b0, 8'd127, 15'd0};

    wire [23:0] ae8, be8;
    vec_cvt_f16_to_e8 u_ca (.f16(a), .e8(ae8));
    vec_cvt_f16_to_e8 u_cb (.f16(b), .e8(be8));

    wire [23:0] a_sel = raw_e8 ? a_e8   : ae8;
    wire [23:0] b_sel = raw_e8 ? E8_ONE : be8;

    vec_alu #(.MODEL(MODEL), .PIPE_MUX(PIPE_MUX)) u_alu (
        .clk(clk), .rst(rst),
        .in_valid(in_valid), .op(OP_FMA),
        .a(a_sel), .b(b_sel), .c(c),
        .out_valid(out_valid), .out(out), .out_pred()
    );

`ifndef SYNTHESIS
    // An unconnected `raw_e8` is `z`, a_sel goes X, and every result reads as a
    // dead accumulator rather than as a missing port. It cost two benches a run.
    always @(posedge clk) if (!rst && in_valid && (raw_e8 === 1'bx || raw_e8 === 1'bz))
        $display("%0t ERROR khd_f16_lane: raw_e8 is %b -- the port is not driven",
                 $time, raw_e8);
`endif

endmodule

`default_nettype wire

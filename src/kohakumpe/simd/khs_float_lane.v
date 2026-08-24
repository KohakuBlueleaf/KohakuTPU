// khs_float_lane -- one element of the float datapath. FP32 or FP16 on the same
// port, `wide` picks which, and E8M15 through the middle.
//
// BOTH INPUT FORMATS AND THE ONE COMPUTE FORMAT ARE THE CONTRACT, not options:
// there is no parameter here that removes either edge. FP16 -> E8M15 is exact
// and FP32 -> E8M15 keeps the exponent verbatim, so neither direction invents
// arithmetic. `vec_alu` does the work, so this tier adds no float arithmetic.
//
// THE OPERATION IS AN INPUT, NOT A CONSTANT, and that is the whole of what the
// elementwise group cost. Tied to FMA the tool constant-folded sixteen of
// vec_alu's seventeen opcodes away; passing `op` brings back exactly the ones a
// build enables. vec_alu already routes the operands per opcode -- ADD and SUB
// force the multiplier to 1.0 and take the addend on `c`, MUL forces the addend
// to zero, MIN/MAX/CMP select at cycle 1 and pass the winner through as
// winner*1.0+0 -- so a caller supplies a, b, c and the opcode and nothing else.
//
// LATENCY IS A CONTRACT, NOT A DETAIL. `ALAT` here must equal what vec_alu
// actually is, because the accumulator above rotates its partials by exactly
// this number -- get it wrong and a write lands on a partial that has already
// been read again. vec_lanes.v derives its own copy the same way and says the
// same thing: the ALU and the delay must share one localparam or they will
// disagree.

`default_nettype none

module khs_float_lane #(
    parameter integer PIPE_MUX = 1,
    // 1 swaps DSP48E2 for vec_dsp's behavioural model, so a simulation failure
    // is attributable to the arithmetic or to the DSP configuration, never to
    // both. Synthesis uses 0.
    parameter integer MODEL    = 0,
    // Forwarded to vec_alu: which opcodes the CALLER's decode can produce, and
    // how deep a control delay stays flops. Permissive here so an existing
    // instantiation is unchanged.
    parameter integer HAS_UNARY = 1,
    parameter integer HAS_FNMA  = 1,
    parameter integer HAS_SEL   = 1,
    // The seeds. This port was MISSING, so vec_alu took its own default of 1 and
    // every float lane in both PEs built the coefficient ROM and DSP-P whether
    // or not the build could issue a seed. A quarter-rate SFU passes 0 on the
    // lanes that are not seed lanes; refusing the encoding stays the caller's.
    parameter integer HAS_POLY  = 1,
    // WHICH MEMORY FORMATS THE EDGE CARRIES; both on is every build before these
    // existed. vec_cvt.v prices the pieces: f16->e8 and f32->e8 are 46 LUT each,
    // e8->f16 is 161 (a 48-bit subnormal shifter) and e8->f32 is wiring.
    parameter integer HAS_F16   = 1,
    parameter integer HAS_F32   = 1,
    parameter integer DLY_FF    = 6
)(
    input  wire        clk,
    input  wire        rst,

    input  wire        in_valid,
    // vec_alu's own opcode. KHS_FOP_* in khs_isa.vh name the ones this machine
    // issues; the accumulator path passes FMA and nothing else.
    input  wire [4:0]  op,
    // ONE PORT, TWO FORMATS: `wide` picks the conversion, the datapath below is
    // E8M15 either way. FP16 sits in the low half, where a register keeps it.
    input  wire        wide,
    input  wire [31:0] a,          // FP32, or FP16 in [15:0]
    input  wire [31:0] b,          // FP32, or FP16 in [15:0]
    input  wire [23:0] c,          // E8M15, the partial being accumulated into

    // The FOLD multiplies a partial -- already E8M15 -- by one, so it needs the
    // operand path without the conversion in front of it. Same lane, same
    // rounding: combining partials through a second adder would round
    // differently from accumulating into them.
    input  wire        raw_e8,
    input  wire [23:0] a_e8,

    output wire        out_valid,
    output wire [23:0] out,        // E8M15
    // The compare bit, aligned with `out`. A compare's `out` is 1.0 or 0.0;
    // the caller splats THIS into the all-ones mask the ISA promises.
    output wire        out_pred
);
    // vec_alu's own depth. 14, or 15 at PIPE_MUX=1 which is what ships.
    localparam integer ALAT = 14 + ((PIPE_MUX != 0) ? 1 : 0);

    localparam [23:0] E8_ONE = {1'b0, 8'd127, 15'd0};

    wire [23:0] ae8, be8;
    generate
    if ((HAS_F16 != 0) && (HAS_F32 != 0)) begin : g_dual
        wire [23:0] ah8, bh8, aw8, bw8;
        vec_cvt_f16_to_e8 u_ca   (.f16(a[15:0]), .e8(ah8));
        vec_cvt_f16_to_e8 u_cb   (.f16(b[15:0]), .e8(bh8));
        vec_cvt_f32_to_e8 u_ca32 (.f32(a),       .e8(aw8));
        vec_cvt_f32_to_e8 u_cb32 (.f32(b),       .e8(bw8));
        assign ae8 = wide ? aw8 : ah8;
        assign be8 = wide ? bw8 : bh8;
    end else if (HAS_F16 != 0) begin : g_narrow
        vec_cvt_f16_to_e8 u_ca (.f16(a[15:0]), .e8(ae8));
        vec_cvt_f16_to_e8 u_cb (.f16(b[15:0]), .e8(be8));
        wire _unused_w = &{1'b0, wide, a[31:16], b[31:16], 1'b0};
    end else begin : g_wide
        vec_cvt_f32_to_e8 u_ca32 (.f32(a), .e8(ae8));
        vec_cvt_f32_to_e8 u_cb32 (.f32(b), .e8(be8));
        wire _unused_w = &{1'b0, wide, 1'b0};
    end
    if ((HAS_F16 == 0) && (HAS_F32 == 0)) begin : g_bad_fmt
        khs_float_lane_needs_HAS_F16_or_HAS_F32 u_bad ();
    end
    endgenerate

    wire [23:0] a_sel = raw_e8 ? a_e8   : ae8;
    wire [23:0] b_sel = raw_e8 ? E8_ONE : be8;

    vec_alu #(.MODEL(MODEL), .PIPE_MUX(PIPE_MUX),
              .HAS_UNARY(HAS_UNARY), .HAS_FNMA(HAS_FNMA), .HAS_SEL(HAS_SEL),
              .HAS_POLY(HAS_POLY), .DLY_FF(DLY_FF)) u_alu (
        .clk(clk), .rst(rst),
        .in_valid(in_valid), .op(op),
        .a(a_sel), .b(b_sel), .c(c),
        .out_valid(out_valid), .out(out), .out_pred(out_pred)
    );

`ifndef SYNTHESIS
    // An unconnected `raw_e8` is `z`, a_sel goes X, and every result reads as a
    // dead accumulator rather than as a missing port. It cost two benches a run.
    always @(posedge clk) if (!rst && in_valid && (raw_e8 === 1'bx || raw_e8 === 1'bz))
        $display("%0t ERROR khs_float_lane: raw_e8 is %b -- the port is not driven",
                 $time, raw_e8);
`endif

endmodule

`default_nettype wire

// kht_imul -- the SIMT PE's per-thread integer multiplier: RV32M `mul`,
// `mulh`, `mulhsu`, `mulhu`, one product per lane.
//
// ONE 33x33 SIGNED MULTIPLY SERVES ALL FOUR. Only the extension bits differ --
// `mulh` sign-extends both operands, `mulhsu` only the first, `mulhu` neither --
// and `mul`'s low 32 bits do not depend on the extension at all. So this is one
// multiplier per lane with two extension bits in front of it, not four.
//
// LATENCY MATCHES THE FLOAT TIER ON PURPOSE. Both units retire through
// kht_unit's single vector-file write port, and two results can only want that
// port on the same cycle if they were issued on the same cycle -- which cannot
// happen, because one instruction issues per cycle. Equal latency makes the
// collision STRUCTURALLY impossible instead of arbitrated, and the wave is
// blocked by `fpend` either way, so the extra cycles cost nothing at occupancy.
// The pad is held in FLIP-FLOPS, not SRLs; `g_pad` below has the measurement.
//
// This is NOT the SIMD tier's multiplier. `khs_mul`'s four-per-lane arrangement
// exists for SWAR `vdot` and its DSP48 count is forced by the B port being 18
// bits signed; a dot product's operands both vary, so the two-int8-MACs-per-DSP
// packing cannot apply. This wants plain per-thread 32x32 and `vdot` stays on
// the SIMD PE.
//
// div and rem are NOT here and must keep faulting: ~35 cycles against a software
// routine's 60-80 is a 2x on a rare instruction, where `mul` is 8-13x on a
// common one. Once `mul` exists, divide-by-a-constant strength-reduces to
// `mulhu`, which is the case a shader actually meets.

`default_nettype none

// ITS OWN UNIT COUNT, NOT THE THREAD COUNT. `mul` is an ADD-ON to the integer
// lane and its width is a separate purchase: MUNITS units serve LANES threads in
// LANES/MUNITS passes, sequenced by kht_unit and counted here only through
// `pass`. The ISA carries no unit count, so a kernel is unchanged by the choice.
module kht_imul #(
    parameter integer LANES  = 8,
    parameter integer MUNITS = 8,
    // Derived, and in the parameter list because the port list needs it: a
    // localparam in the body is declared too late to size a port.
    parameter integer PSW = ((LANES / MUNITS) > 1) ? $clog2(LANES / MUNITS) : 1,
    // Cycles from `in_valid` to `vy`. Must equal the float tier's, so both
    // retire through the same slot without arbitration.
    parameter integer MLAT  = 15
)(
    input  wire                clk,
    input  wire                rst,

    input  wire                in_valid,
    // funct3: 0 mul, 1 mulh, 2 mulhsu, 3 mulhu.
    input  wire [1:0]          op,
    // Which MUNITS-sized slice of the thread array this pass drives.
    input  wire [PSW-1:0]      pass,
    input  wire [32*LANES-1:0] va,
    input  wire [32*LANES-1:0] vb,

    // ONE SLOT PER UNIT, NOT PER THREAD: the caller places it, because where a
    // product belongs depends on the pass that is retiring rather than on the
    // pass being issued.
    output wire [32*MUNITS-1:0] vy
);
    // Three real stages: extend, multiply, select. The rest is pad.
    localparam integer RLAT = 3;
    localparam integer PAD  = MLAT - RLAT;
    localparam integer LNW  = (LANES > 1) ? $clog2(LANES) : 1;

    // `a` is signed for everything except mulhu; `b` only for mulh.
    wire a_sgn = (op != 2'd3);
    wire b_sgn = (op == 2'd1);
    wire hi_sel = (op != 2'd0);

    reg        s1_hi, s2_hi;
    always @(posedge clk) begin
        s1_hi <= hi_sel;
        s2_hi <= s1_hi;
    end

    genvar L;
    generate
    for (L = 0; L < MUNITS; L = L + 1) begin : g_lane
        // NOT `MUNITS[PSW-1:0]`: a parameter sliced to the pass index's own
        // width is 8 truncated to two bits, which is ZERO.
        wire [LNW-1:0] e_idx = pass * MUNITS + L;
        wire [31:0] la = va[32*e_idx +: 32];
        wire [31:0] lb = vb[32*e_idx +: 32];

        reg signed [32:0] ae, be;
        always @(posedge clk) begin
            ae <= {a_sgn & la[31], la};
            be <= {b_sgn & lb[31], lb};
        end

        // 66 EVEN THOUGH 64 IS EXACT -- both operands are a 32-bit value in a
        // 33-bit field, so the product fits 64. Narrowing it measured +0 LUT and
        // +0 DSP: the cascade is sized by the operands, not by the destination.
        reg signed [65:0] prod;
        always @(posedge clk) begin
            prod <= ae * be;
        end

        reg [31:0] res;
        always @(posedge clk) begin
            res <= s2_hi ? prod[63:32] : prod[31:0];
        end

        // FLOPS, NOT AN SRL. An SRL16E is one LUT per bit at any depth and this
        // PE is LUT-bound, so the pad belongs in the half of the CLB that is
        // idle. Measured at the 2.857 ns ask: 21,842 -> 21,586 LUT at an
        // IDENTICAL 365.6 MHz. At 2.500 ns the same change looked like -15.6
        // MHz -- an artifact of asking for timing the design was not going to
        // meet, not a property of the pad.
        if (PAD > 0) begin : g_pad
            (* srl_style = "register" *) reg [32*PAD-1:0] dly;
            always @(posedge clk) begin
                dly <= {dly[32*(PAD-1)-1:0], res};
            end
            assign vy[32*L +: 32] = dly[32*(PAD-1) +: 32];
        end else begin : g_nopad
            assign vy[32*L +: 32] = res;
        end
    end
    if ((MUNITS != 0) && ((LANES / MUNITS) * MUNITS != LANES)) begin : g_bad_mu
        kht_imul_requires_MUNITS_to_divide_LANES u_bad ();
    end
    endgenerate

`ifndef SYNTHESIS
    initial if (MLAT < RLAT)
        $display("ERROR kht_imul: MLAT %0d is shorter than the %0d real stages",
                 MLAT, RLAT);
`endif

    wire _unused = &{1'b0, in_valid, rst, 1'b0};

endmodule

`default_nettype wire

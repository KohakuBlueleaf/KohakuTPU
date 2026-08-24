// khs_lane -- one 32-bit lane of the SIMD datapath: the packed ALU, the packed
// shifter, the bitwise unit, and the multipliers that serve both `vdot` and
// `vmul`.
//
// A LANE IS 32 BITS BECAUSE THE ACCUMULATOR IS. `vdot` reduces the elements
// WITHIN a lane -- four int8 pairs or two int16 pairs -- into one int32, so an
// accumulator register is SIMD int32 lanes and is exactly one vector register
// wide. That is what makes `vaccrd` a move rather than a narrowing, and it is
// the ARM SDOT / x86 VPDPBUSD shape rather than a whole-vector dot.
//
// FOUR MULTIPLIERS, AND ONLY TWO OF THEM ARE WIDE. A 4-way int8 dot needs four
// products; a 2-way int16 dot needs two, and those two need 16x16. So
// multipliers 0 and 1 are 16x16 and carry a mux on their operands, and 2 and 3
// are 8x8 and exist only for int8.
//
// There is no cheaper arrangement on this device, and it is worth saying why
// rather than leaving it to be rediscovered: a DSP48E2's B port is 18 bits
// SIGNED, which holds one int8 operand and not two, so the well-known
// two-int8-MACs-per-DSP packing needs the two products to SHARE an operand.
// A dot product's operands both vary, so they cannot. `MULS = 2` drops int8 to
// two passes and is the configuration that prices that choice.
//
// THE ROUNDING SHIFT HAS ITS OWN ADDER, and that is the second-largest timing
// decision in the unit. `vsrari` is (x >>> s) plus bit s-1 per element, and
// since it reads only one source vector the main adder's second input looked
// free -- so it borrowed it. Measured, that put the shifter in series with the
// adder on EVERY instruction, because the mux in front of the adder is in its
// cone whether or not a shift is issued: ~0.8 ns of a 4.72 ns path, paid by
// add, min, max and every compare. A second SWAR adder is four CARRY8 and a
// handful of LUTs, which is cheaper than what sharing cost.

`default_nettype none

module khs_lane #(
    parameter integer MULS      = 4,        // 4 = int8 dot at II=1, 2 = int16 only
    // Refusing the ENCODING is not removing the hardware: with the shifter
    // still instantiated, a build "without" it measured 32 LUT LARGER than the
    // one with it, because the only thing that changed was a decode term.
    parameter integer HAS_SHIFT = 1,
    // 1 keeps the dot sum INSIDE the DSP48 column. `vmul` needs the individual
    // products, and that single fanout is what stops the tool folding the sum
    // into the post-adder -- so this form pays for a second set of multipliers
    // and takes the adder back. Probed at 8 lanes: 256 LUT + 32 CARRY -> 0 + 0.
    parameter integer DOT_DSP   = 0,
    parameter         USE_DSP   = "yes"
)(
    input  wire        clk,

    input  wire [31:0] a,
    input  wire [31:0] b,
    input  wire [1:0]  et,

    // ---- combinational ALU, one cycle ----
    input  wire [2:0]  alu_op,      // see the localparams below
    input  wire        cmp_sub,     // subtract, INCLUDING for a min/max compare
    input  wire        alu_sat,
    input  wire [4:0]  sh_rot,
    input  wire [31:0] sh_keep,
    input  wire [31:0] sh_rmask,
    input  wire        sh_arith,
    input  wire        sh_left,
    input  wire        sh_round,
    // Each element's MSB, built once in EX and registered: deriving it per lane
    // would put a mux in front of the adder in the cycle that decides Fmax.
    input  wire [31:0] el_mask,
    output wire [31:0] y,

    // ---- the multipliers: products at +1, the dot sum at +2 or +4 ----
    input  wire        mul_en,
    // WHICH BYTE PAIR THIS PASS SERVES, at MULS < 4 and int8 only. The header's
    // "MULS = 2 drops int8 to two passes" was never built -- khs_unit FAULTED the
    // encoding instead, a narrower instruction set rather than more cycles.
    input  wire        mpass,
    // Registered. +2 through the fabric adder; down the DSP48 cascade it is one
    // cycle per PCIN hop, so +4 at MULS 4. khs_unit's DOT_LAT is the same
    // expression and the two must not drift.
    output wire [33:0] dot_sum,
    output wire [31:0] mul_lo       // packed low halves, valid ONE cycle after
);
    localparam [2:0] OP_ADD = 3'd0, OP_MIN = 3'd1, OP_MAX = 3'd2, OP_AND = 3'd3;
    localparam [2:0] OP_OR = 3'd4, OP_XOR = 3'd5, OP_ANDN = 3'd6, OP_SH = 3'd7;

    localparam [1:0] ET_S8 = 2'd0;

    // ---- the shifter -----------------------------------------------------
    wire [31:0] sh_y, sh_round_bit;
    generate
    if (HAS_SHIFT != 0) begin : g_shift
        khs_pshift32 u_sh (
            .x(a), .et(et), .rot(sh_rot), .keep(sh_keep), .rmask(sh_rmask),
            .arith(sh_arith), .left(sh_left),
            .y(sh_y), .round_bit(sh_round_bit)
        );
    end else begin : g_noshift
        assign sh_y         = 32'd0;
        assign sh_round_bit = 32'd0;
    end
    endgenerate

    // ---- the main adder --------------------------------------------------
    // Its inputs are the OPERANDS and nothing else; see the header.
    // `cmp_sub` arrives ALREADY DECIDED. Deriving it here -- min and max need
    // a - b whatever the opcode asked for -- put a LUT between the decode
    // register and the carry chain, per lane, at the head of the binding path.
    // It is the same argument the shift masks are built in EX for.
    wire [31:0] add_y;
    wire [3:0]  lt, lt_s, top;
    khs_padd32 u_add (
        .a(a), .b(b), .sub(cmp_sub), .et(et), .mask(el_mask),
        .sat(alu_sat),
        .y(add_y), .lt(lt), .lt_s(lt_s), .top(top)
    );

    // ---- the rounding shift's own increment ------------------------------
    // One more SWAR add per lane, off the main path. It is only four CARRY8
    // and a handful of LUTs now that the adder is a single native chain, which
    // is cheaper than what sharing cost in delay.
    // THE ROUND BIT IS GATED, NOT THE RESULT. `sh_round_bit` is at most one bit
    // per element and the rest of it is a constant zero the tool has already
    // folded, so masking it is four LUTs -- where choosing between `rnd_y` and
    // `sh_y` afterwards is a 32-bit mux in the lane's output chain.
    wire [31:0] rnd_in = sh_round ? sh_round_bit : 32'd0;
    wire [31:0] rnd_y;
    khs_padd32 u_rnd (
        .a(sh_y), .b(rnd_in), .sub(1'b0), .et(et), .mask(el_mask),
        .sat(1'b0),
        .y(rnd_y), .lt(), .lt_s(), .top()
    );

    // `sel_y` picks b where the flag is set, so a MIN takes b where a is NOT
    // less than b. Written the other way round it computes max for min and min
    // for max, which agrees with itself on every equal-operand test.
    wire [3:0] pick_b = (alu_op == OP_MIN) ? ~lt_s : lt_s;
    reg [31:0] sel_y;
    integer i;
    always @(*) begin
        for (i = 0; i < 4; i = i + 1) begin
            sel_y[i*8 +: 8] = pick_b[i] ? b[i*8 +: 8] : a[i*8 +: 8];
        end
    end

    reg [31:0] y_r;
    always @(*) begin
        case (alu_op)
            OP_MIN, OP_MAX: y_r = sel_y;
            OP_AND:         y_r = a & b;
            OP_OR:          y_r = a | b;
            OP_XOR:         y_r = a ^ b;
            OP_ANDN:        y_r = a & ~b;
            OP_SH:          y_r = rnd_y;
            default:        y_r = add_y;
        endcase
    end
    assign y = y_r;

    // ---- the multipliers -------------------------------------------------
    // 0 and 1 are wide and muxed; 2 and 3 exist only for int8.
    // At MULS >= 4 `mpass` is tied 0 by the caller and these fold to the original
    // constant slices; at MULS < 4 the pass selects the byte pair.
    wire [7:0] a0 = mpass ? a[23:16] : a[7:0];
    wire [7:0] b0 = mpass ? b[23:16] : b[7:0];
    wire [7:0] a1 = mpass ? a[31:24] : a[15:8];
    wire [7:0] b1 = mpass ? b[31:24] : b[15:8];

    wire signed [16:0] m0a = (et == ET_S8) ? {{9{a0[7]}}, a0} : {a[15], a[15:0]};
    wire signed [16:0] m0b = (et == ET_S8) ? {{9{b0[7]}}, b0} : {b[15], b[15:0]};
    wire signed [16:0] m1a = (et == ET_S8) ? {{9{a1[7]}}, a1} : {a[31], a[31:16]};
    wire signed [16:0] m1b = (et == ET_S8) ? {{9{b1[7]}}, b1} : {b[31], b[31:16]};
    wire signed [8:0]  m2a = {a[23], a[23:16]};
    wire signed [8:0]  m2b = {b[23], b[23:16]};
    wire signed [8:0]  m3a = {a[31], a[31:24]};
    wire signed [8:0]  m3b = {b[31], b[31:24]};

    wire signed [33:0] p0, p1;
    wire signed [17:0] p2, p3;

    khs_mul #(.AW(17), .BW(17), .PW(34), .USE_DSP(USE_DSP)) u_m0 (
        .clk(clk), .en(mul_en), .a(m0a), .b(m0b), .p(p0));
    khs_mul #(.AW(17), .BW(17), .PW(34), .USE_DSP(USE_DSP)) u_m1 (
        .clk(clk), .en(mul_en), .a(m1a), .b(m1b), .p(p1));

    reg [1:0] et_r;
    always @(posedge clk) begin
        if (mul_en) begin
            et_r <= et;
        end
    end

    // The pass, and whether a pass issued, delayed to meet the products -- which
    // register one cycle behind `mul_en`. BOTH dot-sum paths need them, so they
    // live here rather than inside either branch.
    reg mp_d, men_d;
    always @(posedge clk) begin
        mp_d  <= mpass;
        men_d <= mul_en;
    end

    wire signed [33:0] hi;
    generate
    if (MULS >= 4) begin : g_int8
        khs_mul #(.AW(9), .BW(9), .PW(18), .USE_DSP(USE_DSP)) u_m2 (
            .clk(clk), .en(mul_en), .a(m2a), .b(m2b), .p(p2));
        khs_mul #(.AW(9), .BW(9), .PW(18), .USE_DSP(USE_DSP)) u_m3 (
            .clk(clk), .en(mul_en), .a(m3a), .b(m3b), .p(p3));
        // Adding at 19 bits and sign-extending after -- so the carry chain is
        // not 15 bits of replicated sign -- measured 14,579 -> 14,611 LUT.
        // Synthesis already collapses the extension; the explicit form only
        // adds a mux.
        assign hi = (et_r == ET_S8)
                  ? ({{16{p2[17]}}, p2} + {{16{p3[17]}}, p3}) : 34'sd0;
    end else begin : g_no_int8
        // MULS = 2 has no int8 dot at all; khs_unit refuses the encoding, so
        // there is nothing to leave half-computed here.
        assign p2 = 18'sd0;
        assign p3 = 18'sd0;
        assign hi = 34'sd0;
    end
    endgenerate

    generate
    if (DOT_DSP == 0) begin : g_sumfab
        reg signed [33:0] sum_r;
        if (MULS >= 4) begin : g_one
            always @(posedge clk) begin
                sum_r <= p0 + p1 + hi;
            end
        end else begin : g_two
            // THE LANE ACCUMULATES ITS OWN PASSES, so DOT_LAT and the II=1
            // accumulator above are untouched. GATED ON A REGISTERED `mul_en`:
            // the products lag it a cycle, and ungated an idle cycle re-adds
            // stale products and a back-to-back dot reads a DOUBLED sum.
            always @(posedge clk) begin
                if (men_d) begin
                    sum_r <= (mp_d ? sum_r : 34'sd0) + p0 + p1;
                end
            end
        end
        assign dot_sum = sum_r;
    end else begin : g_sumdsp
        // A SECOND set of multipliers, deliberately. p0..p3 must surface for
        // `mul_lo`, and an operand with two consumers cannot be cascaded.
        wire signed [8:0] d2a = (et == ET_S8) ? m2a : 9'sd0;
        wire signed [8:0] d2b = (et == ET_S8) ? m2b : 9'sd0;
        wire signed [8:0] d3a = (et == ET_S8) ? m3a : 9'sd0;
        wire signed [8:0] d3b = (et == ET_S8) ? m3b : 9'sd0;

        wire signed [16:0] ta0 = m0a, tb0 = m0b;
        wire signed [16:0] ta1 = m1a, tb1 = m1b;
        wire signed [16:0] ta2 = {{8{d2a[8]}}, d2a}, tb2 = {{8{d2b[8]}}, d2b};
        wire signed [16:0] ta3 = {{8{d3a[8]}}, d3a}, tb3 = {{8{d3b[8]}}, d3b};

        // ONE MULTIPLY PER STAGE. Folding several terms into one register makes
        // the DSP48 do multiply-then-post-add combinationally into PREG -- an
        // unpipelined column, which cost 12 MHz when this was written that way.
        // Each PCIN hop is a cycle, so term k's operands wait k cycles; flops
        // are the cheap resource here and an unpipelined DSP48 is not.
        // FREE-RUNNING, NOT `mul_en` GATED. `mul_en` is a per-instruction pulse
        // -- the fabric path gates only the products with it and lets `sum_r`
        // flow -- so gating a multi-stage cascade freezes hops 2..N and the
        // result never arrives. `acc_pipe` free-runs on the same clock, which
        // is what keeps the two aligned.
        reg signed [16:0] a1d, b1d;
        (* use_dsp = "yes" *) reg signed [33:0] c0, c1;
        always @(posedge clk) begin
            a1d <= ta1; b1d <= tb1;
            c0  <= ta0 * tb0;
            // THE SECOND int8 PASS ACCUMULATES INTO P, at MULS < 4 only; the term
            // is a constant zero at MULS >= 4 and this is the original cascade.
            // The cascade stays FREE-RUNNING -- gating it freezes hops 2..N, which
            // this file's header records costing 12 MHz.
            c1  <= (((MULS < 4) && mp_d) ? c1 : 34'sd0) + c0 + a1d * b1d;
        end

        if (MULS >= 4) begin : g_int8sum
            reg signed [16:0] a2d, b2d, a2dd, b2dd;
            reg signed [16:0] a3d, b3d, a3dd, b3dd, a3ddd, b3ddd;
            (* use_dsp = "yes" *) reg signed [33:0] c2, c3;
            always @(posedge clk) begin
                a2d <= ta2;  b2d <= tb2;  a2dd  <= a2d;  b2dd  <= b2d;
                a3d <= ta3;  b3d <= tb3;  a3dd  <= a3d;  b3dd  <= b3d;
                a3ddd <= a3dd; b3ddd <= b3dd;
                c2 <= c1 + a2dd  * b2dd;
                c3 <= c2 + a3ddd * b3ddd;
            end
            assign dot_sum = c3;
        end else begin : g_pairsum
            assign dot_sum = c1;
        end
    end
    endgenerate

    // vmul keeps the low half of each element's product, so it reads the SAME
    // registered products one cycle earlier than the dot sum.
    // AT MULS < 4 AN int8 PASS CARRIES TWO BYTES, in the low half, and khs_unit
    // places them by pass. At MULS >= 4 all four are live and this is unchanged.
    generate
    if (MULS >= 4) begin : g_mlo4
        assign mul_lo = (et_r == ET_S8)
                      ? {p3[7:0], p2[7:0], p1[7:0], p0[7:0]}
                      : {p1[15:0], p0[15:0]};
    end else begin : g_mlo2
        assign mul_lo = (et_r == ET_S8)
                      ? {16'd0, p1[7:0], p0[7:0]}
                      : {p1[15:0], p0[15:0]};
    end
    endgenerate

endmodule

`default_nettype wire

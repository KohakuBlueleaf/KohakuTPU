// FP32 fused multiply-add: a*b + c at IEEE-754 binary32, round-to-nearest-even.
//
// IN THE FRAMEWORK because RV32F is a STANDARD extension over IEEE binary32.
// E8M15 stayed in a project precisely because it is one project's number
// format; binary32 is nobody's. `vec_alu` cannot serve here -- 15 mantissa
// bits is 256x coarser than FP32 and misses D3D11's 0.5 ULP on add/mul.
//
// DENORMALS FLUSH TO ZERO on input and output. That is not a shortcut: D3D11's
// functional spec REQUIRES it ("Denorms MUST be flushed to sign-preserved zero
// on input and output of any floating point mathematical operation"), so the
// gradual-underflow hardware would be non-conformant as well as expensive.
//
// EVERY OPCODE GOES THROUGH THE FMA, as vec_alu does: mov/neg/abs/min/max/sel
// and the compares pick an operand up front and pass it as `winner*1.0 + 0`,
// which is bit-exact and costs one operand mux instead of a second datapath.

`default_nettype none

module rv_fpu #(
    // 1 keeps the operand mux in its own stage, as vec_alu's PIPE_MUX does.
    parameter integer PIPE_MUX = 1
)(
    input  wire        clk,
    input  wire        rst,
    input  wire        in_valid,
    input  wire [4:0]  op,
    input  wire [31:0] a,
    input  wire [31:0] b,
    input  wire [31:0] c,
    output wire        out_valid,
    output wire [31:0] y,
    // The compare bit, aligned with `y`, for the caller's mask.
    output wire        out_pred
);
    localparam [4:0] OP_MOV = 5'd0,  OP_NEG = 5'd1,  OP_ABS = 5'd2;
    localparam [4:0] OP_ADD = 5'd3,  OP_SUB = 5'd4,  OP_MUL = 5'd5;
    localparam [4:0] OP_FMA = 5'd6,  OP_FNMA = 5'd7;
    localparam [4:0] OP_MAX = 5'd8,  OP_MIN = 5'd9,  OP_SEL = 5'd10;
    localparam [4:0] OP_CMPLT = 5'd11, OP_CMPGT = 5'd12, OP_CMPEQ = 5'd13;

    localparam [31:0] F32_ONE = 32'h3F80_0000;
    localparam [31:0] F32_QNAN = 32'h7FC0_0000;

    // ---- stage 1: unpack, flush, specials, operand select -----------------
    function [23:0] sig_of;
        input [31:0] f;
        // The implicit one, and ZERO for a denormal or a true zero -- the flush
        // is here so nothing downstream ever sees a subnormal significand.
        sig_of = (f[30:23] == 8'd0) ? 24'd0 : {1'b1, f[22:0]};
    endfunction

    wire        a_s = a[31], b_s = b[31], c_s = c[31];
    wire [7:0]  a_e = a[30:23], b_e = b[30:23], c_e = c[30:23];
    wire [22:0] a_m = a[22:0],  b_m = b[22:0],  c_m = c[22:0];

    wire a_nan = (a_e == 8'hFF) && (a_m != 23'd0);
    wire b_nan = (b_e == 8'hFF) && (b_m != 23'd0);
    wire c_nan = (c_e == 8'hFF) && (c_m != 23'd0);
    wire a_inf = (a_e == 8'hFF) && (a_m == 23'd0);
    wire b_inf = (b_e == 8'hFF) && (b_m == 23'd0);
    wire c_inf = (c_e == 8'hFF) && (c_m == 23'd0);
    wire a_z   = (a_e == 8'd0);
    wire b_z   = (b_e == 8'd0);
    wire c_z   = (c_e == 8'd0);

    // The compare, on the ORIGINAL bits. IEEE ordering is the integer ordering
    // once the sign is folded, so this is one adder rather than a float unit.
    // A NEGATIVE IS INVERTED AND NOTHING MORE. OR-ing the top bit back on after
    // the inversion puts every negative above every positive, which made `max`
    // return the smaller operand.
    wire [31:0] a_key = a[31] ? ~a : (a | 32'h8000_0000);
    wire [31:0] b_key = b[31] ? ~b : (b | 32'h8000_0000);
    wire cmp_lt = (a_key < b_key) && !(a_z && b_z);
    wire cmp_eq = (a_key == b_key) || (a_z && b_z);
    wire cmp_gt = !cmp_lt && !cmp_eq;
    wire any_nan_ab = a_nan || b_nan;

    wire is_cmp = (op == OP_CMPLT) || (op == OP_CMPGT) || (op == OP_CMPEQ);
    wire pred   = (op == OP_CMPLT) ? cmp_lt
                : (op == OP_CMPGT) ? cmp_gt
                : cmp_eq;
    wire pred_q = pred && !any_nan_ab;

    // `a` wins for min/max/sel unless the other operand does.
    // IEEE maxNum/minNum: a NaN operand LOSES, so one NaN returns the other
    // and two return a quiet NaN through the ordinary specials path.
    wire take_b = ((op == OP_MAX) && (cmp_lt || a_nan) && !b_nan)
               || ((op == OP_MIN) && (cmp_gt || a_nan) && !b_nan)
               // THE FLUSH APPLIES TO THE CONDITION TOO: a denormal `c` is
               // zero once flushed, so testing the raw mantissa selects the
               // wrong operand.
               || ((op == OP_SEL) && (c[30:23] == 8'd0));

    reg [31:0] s1_a, s1_b, s1_c;
    reg [4:0]  s1_op;
    reg        s1_v, s1_pred, s1_cmp;
    always @(posedge clk) begin
        s1_v    <= !rst && in_valid;
        s1_op   <= op;
        s1_pred <= pred_q;
        s1_cmp  <= is_cmp;
        // The unary and select opcodes become `winner * 1.0 + 0`.
        case (op)
            OP_MOV, OP_MAX, OP_MIN, OP_SEL: begin
                s1_a <= take_b ? b : a; s1_b <= F32_ONE; s1_c <= 32'd0;
            end
            OP_NEG: begin
                s1_a <= {~a[31], a[30:0]}; s1_b <= F32_ONE; s1_c <= 32'd0;
            end
            OP_ABS: begin
                s1_a <= {1'b0, a[30:0]}; s1_b <= F32_ONE; s1_c <= 32'd0;
            end
            OP_ADD: begin s1_a <= a; s1_b <= F32_ONE; s1_c <= c; end
            OP_SUB: begin
                s1_a <= a; s1_b <= F32_ONE; s1_c <= {~c[31], c[30:0]};
            end
            OP_MUL: begin s1_a <= a; s1_b <= b; s1_c <= 32'd0; end
            OP_FNMA: begin
                s1_a <= {~a[31], a[30:0]}; s1_b <= b; s1_c <= c;
            end
            default: begin  // OP_FMA and the compares
                s1_a <= a; s1_b <= b; s1_c <= c;
            end
        endcase
    end

    // ---- stage 2: product, alignment ---------------------------------------
    wire [23:0] pa = sig_of(s1_a), pb = sig_of(s1_b), pc = sig_of(s1_c);
    wire        ps = s1_a[31] ^ s1_b[31];
    wire        cs_sign = s1_c[31];

    wire pa_z = (s1_a[30:23] == 8'd0), pb_z = (s1_b[30:23] == 8'd0);
    wire pz   = pa_z || pb_z;
    wire cz   = (s1_c[30:23] == 8'd0);

    // e_ab is the product's biased exponent before normalisation. The product
    // of two 24-bit significands sits in [1,4), so bit 47 may or may not be set
    // and the leading-one search below settles it.
    wire signed [10:0] e_ab =
        $signed({3'b0, s1_a[30:23]}) + $signed({3'b0, s1_b[30:23]}) - 11'sd127;
    wire signed [10:0] e_c = $signed({3'b0, s1_c[30:23]});

    // THE ADDEND SITS AT THE TOP OF A 72-BIT FIELD and shifts DOWN, so one
    // shifter serves both directions. 25 bits of headroom is the addend's own
    // width plus the product's possible carry.
    wire signed [11:0] sh_raw = 12'sd25 + (e_ab - e_c);
    // TWO CLAMPS, AND NEITHER IS COSMETIC. Below 0 the addend is more than 25
    // binades above the product: the addend IS the rounded answer, so the
    // product is zeroed and the exponent rebases on the addend -- vec_alu's
    // `byp`. Above 96 the addend is entirely below the product and survives
    // only as a sticky bit, which the shifter would otherwise drop.
    wire byp = (sh_raw < 12'sd0) && !cz;
    wire tiny_c = (sh_raw > 12'sd96);
    wire [6:0] sh = byp ? 7'd0 : (tiny_c ? 7'd96 : sh_raw[6:0]);

    (* use_dsp = "yes" *)
    wire [47:0] prod = pa * pb;

    reg  [23:0] s2_pc;
    reg  [6:0]  s2_sh;
    reg  [47:0] s2_prod;
    reg         s2_v, s2_ps, s2_cs, s2_stk, s2_pz, s2_cz, s2_pred, s2_cmp;
    reg  signed [10:0] s2_eab;
    reg  [31:0] s2_spec;
    reg         s2_has_spec;

    // A specials result short-circuits the datapath: NaN in, inf*0, inf-inf.
    wire spec_nan = (s1_a[30:23] == 8'hFF && s1_a[22:0] != 0)
                 || (s1_b[30:23] == 8'hFF && s1_b[22:0] != 0)
                 || (s1_c[30:23] == 8'hFF && s1_c[22:0] != 0)
                 || ((s1_a[30:23] == 8'hFF || s1_b[30:23] == 8'hFF) && pz);
    wire p_inf = (s1_a[30:23] == 8'hFF) || (s1_b[30:23] == 8'hFF);
    wire c_inf_s = (s1_c[30:23] == 8'hFF) && (s1_c[22:0] == 0);
    wire inf_sub = p_inf && c_inf_s && (ps != cs_sign);

    // THE SHIFT AMOUNT IS REGISTERED, NOT THE SHIFTED DATA. `e_ab` is an 11-bit
    // add, `sh_raw` a 12-bit add, then two clamps -- and putting the 7 mux
    // levels of the barrel shifter after all of that in one cycle is what held
    // the lane at 291 MHz against a 300 ask.
    always @(posedge clk) begin
        s2_v    <= !rst && s1_v;
        s2_ps   <= ps;
        s2_cs   <= cs_sign;
        s2_prod <= prod;
        s2_pc   <= pc;
        s2_sh   <= sh;
        // A `tiny_c` addend is entirely below the field and must still set the
        // sticky the shifter can no longer carry.
        s2_stk  <= tiny_c && !cz;
        s2_pz   <= pz || byp;
        s2_cz   <= cz;
        // Rebased on the addend when the product is bypassed: with pc landing
        // at field bit 48 its leading one is at 71, and 71 + base - 46 = e_c.
        s2_eab  <= byp ? (e_c - 11'sd25) : e_ab;
        s2_pred <= s1_pred;
        s2_cmp  <= s1_cmp;
        s2_has_spec <= spec_nan || inf_sub || p_inf || c_inf_s;
        s2_spec <= (spec_nan || inf_sub) ? F32_QNAN
                 : p_inf   ? {ps, 8'hFF, 23'd0}
                           : {cs_sign, 8'hFF, 23'd0};
    end

    // ---- stage 3: align, add, leading one ----------------------------------
    // A 72-BIT ALIGNER, NOT 96. Only 72 bits can land in the field, so the
    // wider shifter was 24 bits x 7 mux levels of pure waste. What it was
    // carrying was the sticky, and that is a 24-bit masked OR instead: the
    // addend's low bits only leave the field once sh passes 48.
    wire [71:0] algn = {s2_pc, 48'b0} >> s2_sh;
    wire [4:0]  lost = (s2_sh > 7'd48)
                     ? ((s2_sh > 7'd72) ? 5'd24 : (s2_sh - 7'd48))
                     : 5'd0;
    wire [23:0] lost_mask = ~({24{1'b1}} << lost);
    wire        algn_stk  = |(s2_pc & lost_mask);

    // MEASURED: align-shift + 72-bit CARRY8 subtract + lead1 encode in ONE
    // cycle is 18 logic levels, s2_pc_reg -> s3_pos_reg, and held the lane at
    // 261 MHz. Each of the three now gets its own stage; FF is abundant here
    // and LUT is not.
    reg  [71:0] s3_algn, s3_prod;
    reg         s3_v, s3_ps, s3_cs, s3_stk, s3_pz, s3_cz, s3_cmp, s3_pred;
    reg         s3_has_spec;
    reg  signed [10:0] s3_eab;
    reg  [31:0] s3_spec;
    always @(posedge clk) begin
        s3_v <= !rst && s2_v;
        s3_algn <= algn;
        s3_prod <= {24'd0, s2_prod};
        s3_ps <= s2_ps; s3_cs <= s2_cs; s3_pz <= s2_pz; s3_cz <= s2_cz;
        s3_stk <= s2_stk | (algn_stk && !s2_cz);
        s3_eab <= s2_eab; s3_cmp <= s2_cmp; s3_pred <= s2_pred;
        s3_has_spec <= s2_has_spec; s3_spec <= s2_spec;
    end

    // ---- stage 4: the add ---------------------------------------------------
    wire [71:0] pterm = s3_pz ? 72'd0 : s3_prod;
    wire [71:0] cterm = s3_cz ? 72'd0 : s3_algn;
    wire        sub   = (s3_ps != s3_cs);

    wire [72:0] sum_a = {1'b0, cterm} + {1'b0, pterm};
    wire [72:0] sum_s = {1'b0, cterm} - {1'b0, pterm};
    wire [72:0] sum_r = {1'b0, pterm} - {1'b0, cterm};
    wire        c_ge  = (cterm >= pterm);

    wire [71:0] mag  = sub ? (c_ge ? sum_s[71:0] : sum_r[71:0]) : sum_a[71:0];
    wire        rsgn = sub ? (c_ge ? s3_cs : s3_ps) : s3_ps;

    reg  [71:0] sA_mag;
    reg         sA_v, sA_sgn, sA_stk, sA_cmp, sA_pred, sA_has_spec;
    reg  signed [10:0] sA_eab;
    reg  [31:0] sA_spec;
    always @(posedge clk) begin
        sA_v <= !rst && s3_v; sA_mag <= mag; sA_sgn <= rsgn;
        sA_stk <= s3_stk; sA_eab <= s3_eab;
        sA_cmp <= s3_cmp; sA_pred <= s3_pred;
        sA_has_spec <= s3_has_spec; sA_spec <= s3_spec;
    end

    // ---- stage 5: the leading one -------------------------------------------
    wire [6:0] lz;
    wire       lz_nz;
    rv_lead1_72 u_l1 (.x(sA_mag), .pos(lz), .nz(lz_nz));

    reg  [71:0] s3_mag;
    reg  [6:0]  s3_pos;
    reg         s3_nz, s3_sgn;
    reg         s3b_v, s3b_stk, s3b_cmp, s3b_pred, s3b_has_spec;
    reg  signed [10:0] s3b_eab;
    reg  [31:0] s3b_spec;
    always @(posedge clk) begin
        s3b_v <= !rst && sA_v; s3_mag <= sA_mag; s3_pos <= lz; s3_nz <= lz_nz;
        s3_sgn <= sA_sgn; s3b_stk <= sA_stk; s3b_eab <= sA_eab;
        s3b_cmp <= sA_cmp; s3b_pred <= sA_pred;
        s3b_has_spec <= sA_has_spec; s3b_spec <= sA_spec;
    end

    // ---- stage 6: normalise ------------------------------------------------
    wire [71:0] nrm_c = s3_mag << (7'd71 - s3_pos);

    reg  [71:0] s4_nrm;
    reg         s4_v, s4_sgn, s4_stk, s4_cmp, s4_pred, s4_has_spec, s4_nz;
    reg  [6:0]  s4_pos;
    reg  signed [10:0] s4_eab;
    reg  [31:0] s4_spec;
    always @(posedge clk) begin
        s4_v   <= !rst && s3b_v;
        s4_nrm <= nrm_c;
        s4_sgn <= s3_sgn;  s4_stk <= s3b_stk; s4_pos <= s3_pos;
        s4_eab <= s3b_eab; s4_cmp <= s3b_cmp; s4_pred <= s3b_pred;
        s4_has_spec <= s3b_has_spec; s4_spec <= s3b_spec; s4_nz <= s3_nz;
    end

    // ---- stage 5: round, pack ----------------------------------------------
    // The leading one is at bit 71, so the 24 significand bits sit at [71:48],
    // the guard at [47] and everything below is sticky.
    wire [23:0] keep  = s4_nrm[71:48];
    wire        guard = s4_nrm[47];
    wire        stick = (|s4_nrm[46:0]) | s4_stk;

    wire        rnd_up = guard & (stick | keep[0]);
    wire [24:0] sig_r  = {1'b0, keep} + {24'b0, rnd_up};
    wire        carry  = sig_r[24];
    wire [22:0] frac   = carry ? sig_r[23:1] : sig_r[22:0];

    // The product of two 24-bit significands is 2^46 * (1.ma * 1.mb), so a
    // field value F is F * 2^(eab-127-46). Normalising puts the leading one at
    // 71 and `keep` at [71:48], which leaves E = pos + eab - 46. Checked
    // against 1.0*1.0: 46 + 127 - 46 = 127.
    // SIGN-EXTENDED, not zero-extended: `{1'b0, s3_eab}` on a signed value
    // turns a negative product exponent into a large positive one, and a
    // multiply that must underflow to zero returns an infinity instead.
    wire signed [11:0] e_fin = $signed({s4_eab[10], s4_eab})
                             + $signed({5'b0, s4_pos})
                             - 12'sd46 + (carry ? 12'sd1 : 12'sd0);

    wire over  = (e_fin >= 12'sd255);
    wire under = (e_fin <= 12'sd0) || !s4_nz;

    // THE COMPARE OUTRANKS THE SPECIALS: its answer is a boolean, and an inf or
    // NaN operand is an ordinary input to it, not a special result.
    assign y = s4_cmp      ? (s4_pred ? F32_ONE : 32'd0)
             : s4_has_spec ? s4_spec
             : over        ? {s4_sgn, 8'hFF, 23'd0}
             : under       ? {s4_sgn, 31'd0}
                           : {s4_sgn, e_fin[7:0], frac};
    assign out_valid = s4_v;
    assign out_pred  = s4_pred;

    wire _unused = &{1'b0, PIPE_MUX, a_nan, b_nan, c_nan,
                     a_inf, b_inf, c_inf, a_z, b_z, c_z, s4_nrm[0], 1'b0};
endmodule


// Leading-one over 72 bits: smear, isolate, encode. A `found`-flag search loop
// synthesises as a 72-deep LUT chain, the shape mx_fpacc.v records as costing
// this design ~68 MHz once already.
module rv_lead1_72 (
    input  wire [71:0] x,
    output reg  [6:0]  pos,
    output wire        nz
);
    reg [71:0] sm;
    integer i;
    always @(*) begin
        sm = x;
        sm = sm | (sm >> 1);   sm = sm | (sm >> 2);
        sm = sm | (sm >> 4);   sm = sm | (sm >> 8);
        sm = sm | (sm >> 16);  sm = sm | (sm >> 32);
        pos = 7'd0;
        for (i = 0; i < 72; i = i + 1) begin
            if (sm[i]) begin
                pos = i[6:0];
            end
        end
    end
    assign nz = |x;
endmodule

`default_nettype wire

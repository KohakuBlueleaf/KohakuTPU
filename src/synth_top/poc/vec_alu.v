// The vector ALU: one E8M15 lane, three DSPs, II = 1, latency 14.
// Design and the three DSP bit maps: docs/compute/vector-core.md s3.1, s3.2, s4.3.
//
//     S1 E8 M15,  sig = {1'b1, M}  -- 16 bits, ALWAYS normalised
//     E = 0    zero        E = 255  inf (M==0) / NaN (M!=0)
//     NO SUBNORMALS: neither source format can produce one, so there is no
//     implicit-bit mux and no denormal fixup anywhere in the lane.
//
// EVERY OPCODE GOES THROUGH THE FMA. max/min/sel/cmp/mov/neg/abs pick an operand
// at cycle 1 and send it through as `winner * 1.0 + 0`, which is bit-exact. The
// four seeds share the normaliser too: each produces a fixed-point magnitude and
// a signed exponent base, and `e_out = lead1(mag) + ebase` closes for all of
// them as well as for the FMA, so the lane holds one leading-one search, one
// normalising shift and one rounder.
//
//     FMA     mag = |C +/- M|            ebase = u - 286
//     exp2    mag = 2^20 + F             ebase = k + 107
//     log2    mag = |(E-127)*2^20 + F|   ebase = 107
//     inv     mag = F                    ebase = 234 - e_a
//     rsqrt   mag = F                    ebase = 107 - K
//
// PIPELINE. Cycle numbers below are cycles after the input register and they are
// load-bearing: vec_dsp's contract is A/B/D at N-3 and C/ALUMODE at N-2 for a
// result in cycle N. Every cross-cycle signal travels in a named vec_delay whose
// D is (consume cycle - produce cycle).
//
//   1  unpack, operand select, specials, compare; exp2 range reduction SHIFT
//        DSP-E A/B/D driven                                -> P in cycle 4
//   2  DSP-E C driven; exp2 round + negate; table address
//   3  coefficients looked up; DSP-P A/B driven            -> P in cycle 6
//   4  DSP-P C driven; DSP-E result -> u, shift amount, ebase
//   5  alignment shift (48-bit, ONE direction) + sticky
//   6  DSP-P result -> h
//   7  DSP-M A/B driven  (sig_b,sig_a) or (h,u)            -> P in cycle 10
//   8  DSP-M C driven    (aligned addend, or c0 + the per-function term)
//  10  DSP-M result -> magnitude and sign
//  11  leading-one search
//  12  normalising shift
//  13  round, exponent bounds, assemble
//  14  out_valid
//
// MODEL = 1 uses vec_dsp's behavioural model instead of DSP48E2, so a failure is
// attributable to the maths or to the DSP configuration but never to both.

`default_nettype none

// Value-less, because xvlog.bat splits arguments on '='.
`ifdef VEC_PIPE_MUX
  `define PM_INIT 1
`else
  `define PM_INIT 0
`endif

module vec_alu #(
    parameter integer MODEL = 0,
    parameter integer PIPE_MUX = `PM_INIT
)(
    input  wire        clk,
    input  wire        rst,

    input  wire        in_valid,
    input  wire [4:0]  op,
    input  wire [23:0] a,
    input  wire [23:0] b,
    input  wire [23:0] c,

    output reg         out_valid,
    output reg  [23:0] out,
    output reg         out_pred      // the compare bit, for predication
);
    // Opcodes. tests/vector/vec_alu_tb.v redeclares the same numbers.
    localparam [4:0] OP_MOV   = 5'd0,  OP_NEG   = 5'd1,  OP_ABS   = 5'd2;
    localparam [4:0] OP_ADD   = 5'd3,  OP_SUB   = 5'd4,  OP_MUL   = 5'd5;
    localparam [4:0] OP_FMA   = 5'd6,  OP_FNMA  = 5'd7;
    localparam [4:0] OP_MAX   = 5'd8,  OP_MIN   = 5'd9,  OP_SEL   = 5'd10;
    localparam [4:0] OP_CMPLT = 5'd11, OP_CMPGT = 5'd12, OP_CMPEQ = 5'd13;
    localparam [4:0] OP_EXP2  = 5'd16, OP_LOG2  = 5'd17;
    localparam [4:0] OP_INV   = 5'd18, OP_RSQRT = 5'd19;

    localparam [23:0] E8_ONE  = 24'h3F8000;      // 1.0 = {0, 127, 0}
    localparam [23:0] E8_ZERO = 24'h000000;
    localparam [23:0] E8_NAN  = 24'h7FC000;      // quiet

    // ---- cycle 0 -> 1 : input registers ----------------------------------
    reg [4:0]  s1_op;
    reg [23:0] s1_a, s1_b, s1_c;
    // Zero-exponent flags taken BEFORE the register: as `(va_e == 0)` after the
    // operand mux this put an 8-bit compare in the DSP-E A cone, which is the
    // ship top's critical path.
    reg        s1_az, s1_bz;
    // vpipe[k] is readable in cycle k+1, so the instruction sampled at cycle 0
    // is at vpipe[12] during cycle 13, the cycle registered into `out`.
    // One deeper under PIPE_MUX: the registered operand mux adds a stage, so the
    // whole datapath is 15 cycles rather than 14.
    reg [13:0] vpipe;

    always @(posedge clk) begin
        if (rst) vpipe <= 14'd0;
        else     vpipe <= {vpipe[12:0], in_valid};
        s1_op <= op;
        s1_a  <= a;
        s1_b  <= b;
        s1_c  <= c;
        s1_az <= (a[22:15] == 8'd0);
        s1_bz <= (b[22:15] == 8'd0);
    end

    // ---- cycle 1 : unpack, select operands, specials, compare, range reduce
    wire        ra_s = s1_a[23];
    wire [7:0]  ra_e = s1_a[22:15];
    wire [14:0] ra_m = s1_a[14:0];
    wire [7:0]  rb_e = s1_b[22:15];
    wire [7:0]  rc_e = s1_c[22:15];

    wire ra_z = (ra_e == 8'd0);
    wire ra_x = (ra_e == 8'hFF);
    wire ra_n = ra_x &  (|ra_m);
    wire ra_i = ra_x & ~(|ra_m);
    wire rb_n = (rb_e == 8'hFF) &  (|s1_b[14:0]);

    // ---- ordered compare -------------------------------------------------
    // Sign-magnitude with the exponent above the mantissa, so ordering by
    // magnitude is the plain unsigned order of bits [22:0]. The equal-magnitude
    // case has to be split out or two equal negatives report a < b, and then
    // max(x,x) != x.
    wire [22:0] cmp_a = s1_a[22:0], cmp_b = s1_b[22:0];
    wire        cmp_nan = ra_n | rb_n;
    wire        both_z  = (cmp_a == 23'd0) && (cmp_b == 23'd0);
    wire        cmp_eq  = both_z || ((cmp_a == cmp_b) && (ra_s == s1_b[23]));
    wire        cmp_lt  = (cmp_nan | cmp_eq) ? 1'b0
                        : (ra_s ^ s1_b[23])  ? ra_s
                        : (ra_s ? (cmp_a > cmp_b) : (cmp_a < cmp_b));
    wire        cmp_gt  = ~cmp_nan & ~cmp_eq & ~cmp_lt;

    wire pred = (s1_op == OP_CMPLT) ? cmp_lt
              : (s1_op == OP_CMPGT) ? cmp_gt
              : (s1_op == OP_CMPEQ) ? (cmp_eq & ~cmp_nan)
              : 1'b0;

    // Pre-mux signals, registered under PIPE_MUX to cross the new stage with the
    // operands. The mux and compare keep the unregistered ones.
    reg  [4:0]  opD_r;
    reg         raD_s_r, predD_r;
    reg  [7:0]  raD_e_r;
    reg  [14:0] raD_m_r;
    always @(posedge clk) begin
        opD_r <= s1_op; raD_s_r <= ra_s; raD_e_r <= ra_e;
        raD_m_r <= ra_m; predD_r <= pred;
    end
    wire [4:0]  opD   = (PIPE_MUX != 0) ? opD_r   : s1_op;
    wire        raD_s = (PIPE_MUX != 0) ? raD_s_r : ra_s;
    wire [7:0]  raD_e = (PIPE_MUX != 0) ? raD_e_r : ra_e;
    wire [14:0] raD_m = (PIPE_MUX != 0) ? raD_m_r : ra_m;
    wire        predD = (PIPE_MUX != 0) ? predD_r : pred;

    wire raD_z = (raD_e == 8'd0);
    wire raD_x = (raD_e == 8'hFF);
    wire raD_n = raD_x &  (|raD_m);
    wire raD_i = raD_x & ~(|raD_m);

    wire is_poly = (opD == OP_EXP2) | (opD == OP_LOG2)
                 | (opD == OP_INV)  | (opD == OP_RSQRT);
    wire is_cmp  = (opD == OP_CMPLT) | (opD == OP_CMPGT) | (opD == OP_CMPEQ);

    // ---- operand selection ----------------------------------------------
    // va feeds the multiplier and the exponent path, vb is 1.0 unless this is a
    // real product, vc is the addend. Negation is a sign flip on an operand, so
    // sub and fnma need no datapath mode.
    reg [23:0] va, vb, vc;
    always @(*) begin
        case (s1_op)
            OP_NEG:   va = {~ra_s, s1_a[22:0]};
            OP_ABS:   va = { 1'b0, s1_a[22:0]};
            OP_FNMA:  va = {~ra_s, s1_a[22:0]};
            OP_MAX:   va = cmp_lt ? s1_b : s1_a;
            OP_MIN:   va = cmp_gt ? s1_b : s1_a;
            OP_SEL:   va = (|s1_c[22:0]) ? s1_a : s1_b;
            OP_CMPLT,
            OP_CMPGT,
            OP_CMPEQ: va = pred ? E8_ONE : E8_ZERO;
            default:  va = s1_a;
        endcase

        case (s1_op)
            OP_MUL, OP_FMA, OP_FNMA: vb = s1_b;
            default:                 vb = E8_ONE;
        endcase

        case (s1_op)
            OP_ADD:          vc = s1_c;
            OP_SUB:          vc = {~s1_c[23], s1_c[22:0]};
            OP_FMA, OP_FNMA: vc = s1_c;
            default:         vc = E8_ZERO;
        endcase
    end

    // PIPE_MUX=1 is a TIMING PROBE ONLY, one cycle off against the delay lines:
    // it cuts s1_b -> compare -> va mux -> DSP preadd, the 377 MHz limiter.
    reg [23:0] va_r, vb_r, vc_r;
    always @(posedge clk) begin va_r <= va; vb_r <= vb; vc_r <= vc; end
    wire [23:0] vaU = (PIPE_MUX != 0) ? va_r : va;
    wire [23:0] vbU = (PIPE_MUX != 0) ? vb_r : vb;
    wire [23:0] vcU = (PIPE_MUX != 0) ? vc_r : vc;

    wire [7:0]  va_e = vaU[22:15], vb_e = vbU[22:15], vc_e = vcU[22:15];
    wire [15:0] va_g = {1'b1, vaU[14:0]};
    wire [15:0] vb_g = {1'b1, vbU[14:0]};
    wire [15:0] vc_g = {1'b1, vcU[14:0]};
    wire        vc_z = (vc_e == 8'd0);

    // Same values as `(va_e == 0)` / `(vb_e == 0)`, selected from the flags
    // registered above rather than compared after the mux. E8_ONE's exponent is
    // 7F and E8_ZERO's is 0, so a compare result yields ~pred.
    reg va_z, vb_z;
    reg va_zr, vb_zr;
    always @(posedge clk) begin va_zr <= va_z; vb_zr <= vb_z; end
    wire vaUz = (PIPE_MUX != 0) ? va_zr : va_z;
    wire vbUz = (PIPE_MUX != 0) ? vb_zr : vb_z;
    always @(*) begin
        case (s1_op)
            OP_MAX:   va_z = cmp_lt ? s1_bz : s1_az;
            OP_MIN:   va_z = cmp_gt ? s1_bz : s1_az;
            OP_SEL:   va_z = (|s1_c[22:0]) ? s1_az : s1_bz;
            OP_CMPLT,
            OP_CMPGT,
            OP_CMPEQ: va_z = ~pred;
            default:  va_z = s1_az;
        endcase
        case (s1_op)
            OP_MUL, OP_FMA, OP_FNMA: vb_z = s1_bz;
            default:                 vb_z = 1'b0;
        endcase
    end

    // A zero factor must not let a large exponent drag the addend out of range,
    // so a zero product forces both exponents to their minimum. The shift amount
    // is then deeply negative, which IS the "addend dominates" condition, and the
    // addend passes through unchanged.
    wire       pz     = vaUz | vbUz;
    wire [7:0] ea_eff = pz ? 8'd1 : va_e;
    wire [7:0] eb_eff = pz ? 8'd1 : vb_e;

    wire sign_ab = vaU[23] ^ vbU[23];
    wire fma_neg = sign_ab ^ vcU[23];

    // x - x is +0, not -0. The sign otherwise comes from whichever term won, and
    // on exact cancellation neither did. Needs both terms genuinely nonzero: an
    // underflow to zero, or a zero operand, must still keep its sign.
    wire cancels = fma_neg & ~pz & ~vc_z;

    // ---- specials --------------------------------------------------------
    wire va_n = (va_e == 8'hFF) &  (|vaU[14:0]);
    wire vb_n = (vb_e == 8'hFF) &  (|vbU[14:0]);
    wire vc_n = (vc_e == 8'hFF) &  (|vcU[14:0]);
    wire va_i = (va_e == 8'hFF) & ~(|vaU[14:0]);
    wire vb_i = (vb_e == 8'hFF) & ~(|vbU[14:0]);
    wire vc_i = (vc_e == 8'hFF) & ~(|vcU[14:0]);

    wire p_nan = va_n | vb_n | (va_i & vbUz) | (vaUz & vb_i);
    wire p_inf = (va_i | vb_i) & ~p_nan;
    wire f_nan = p_nan | vc_n | (p_inf & vc_i & (sign_ab != vcU[23]));
    wire f_inf = (p_inf | vc_i) & ~f_nan;

    // vec_cu's limiter is s1_b_reg -> u_d_inf's SRL, 9 levels: this case block.
    // Free to register -- the delay lines below give back the stage it costs.
    reg spec_nan_c, spec_inf_c, spec_zero_c, spec_sign_c;
    // shreg_extract=no or the SRL swallows these back and the 9 levels stay put.
    (* shreg_extract = "no" *) reg spec_nan, spec_inf, spec_zero, spec_sign;
    always @(posedge clk) begin
        spec_nan  <= spec_nan_c;
        spec_inf  <= spec_inf_c;
        spec_zero <= spec_zero_c;
        spec_sign <= spec_sign_c;
    end

    always @(*) begin
        case (opD)
            OP_EXP2: begin
                // e_a > 134 is |x| >= 128, which leaves E8's exponent range in
                // one direction or the other; which one is the sign of x.
                spec_nan_c  = raD_n;
                spec_inf_c  = ~raD_n & ~raD_s & (raD_i | (raD_e > 8'd134));
                spec_zero_c = ~raD_n &  raD_s & (raD_i | (raD_e > 8'd134));
                spec_sign_c = 1'b0;
            end
            OP_LOG2: begin
                spec_nan_c  = raD_n | (raD_s & ~raD_z);
                spec_inf_c  = ~raD_n & ~(raD_s & ~raD_z) & (raD_i | raD_z);
                spec_zero_c = 1'b0;
                spec_sign_c = raD_z;                    // log2(0) = -inf
            end
            OP_INV: begin
                spec_nan_c  = raD_n;
                spec_inf_c  = ~raD_n & raD_z;
                spec_zero_c = ~raD_n & raD_i;
                spec_sign_c = raD_s;
            end
            OP_RSQRT: begin
                spec_nan_c  = raD_n | (raD_s & ~raD_z);
                spec_inf_c  = ~raD_n & ~(raD_s & ~raD_z) & raD_z;
                spec_zero_c = ~raD_n & ~(raD_s & ~raD_z) & raD_i;
                spec_sign_c = 1'b0;
            end
            default: begin                            // the FMA family
                spec_nan_c  = f_nan;
                spec_inf_c  = f_inf;
                spec_zero_c = 1'b0;
                spec_sign_c = p_inf ? sign_ab : vcU[23];
            end
        endcase
    end

    // ---- exp2 range reduction, part 1 : the shift ------------------------
    // R = round(sig_a * 2^(e_a-125)) as s8.17 fixed point. ONE right shift,
    // because the bias absorbs the direction. Seventeen fraction bits and not
    // fifteen: rounding the exponent argument to 2^-16 would cost ln2*2^-16 =
    // 0.35 ulp, four times the table's own error. See scripts/py/vec_tables.py.
    //
    // Split across two cycles with the seam after the shift -- whole, this was
    // the critical path of the entire ALU.
    wire [7:0]  rr_raw  = 8'd134 - raD_e;
    wire [5:0]  rr_sh   = (raD_e > 8'd134)  ? 6'd0
                        : (rr_raw > 8'd26)  ? 6'd26 : rr_raw[5:0];
    wire [25:0] rr_out  = {1'b1, raD_m, 10'b0} >> rr_sh;

    wire [1:0]  fsel    = (opD == OP_EXP2) ? 2'd0
                        : (opD == OP_LOG2) ? 2'd1
                        : (opD == OP_INV)  ? 2'd2 : 2'd3;

    // rsqrt: K = floor((e_a-127)/2). An arithmetic shift is floor for both
    // signs, which is what makes odd and even exponents one expression.
    wire signed [9:0] rs_k = ($signed({2'b0, raD_e}) - 10'sd127) >>> 1;

    // ---- cycle 2 : finish the reduction, form the table address ----------
    wire [25:0] r2_out;
    wire [14:0] r2_m;
    wire [7:0]  r2_e;
    wire        r2_s, r2_exp2, r2_rsq;
    vec_delay #(.W(26), .D(1)) u_d_rr (.clk(clk), .d(rr_out), .q(r2_out));
    vec_delay #(.W(15), .D(1)) u_d_rm (.clk(clk), .d(raD_m),  .q(r2_m));
    vec_delay #(.W(8),  .D(1)) u_d_re (.clk(clk), .d(raD_e),  .q(r2_e));
    vec_delay #(.W(1),  .D(1)) u_d_rs (.clk(clk), .d(raD_s),  .q(r2_s));
    vec_delay #(.W(1),  .D(1)) u_d_x2 (.clk(clk), .d(opD == OP_EXP2),  .q(r2_exp2));
    vec_delay #(.W(1),  .D(1)) u_d_rq (.clk(clk), .d(opD == OP_RSQRT), .q(r2_rsq));

    // Round and negate in PARALLEL, not in series: -(R+g) is ~R + ~g for a
    // one-bit g. Negating the whole s8.17 word IS the floor/frac split for a
    // negative argument -- 2^x = 2^floor(x) * 2^frac(x) needs frac in [0,1), and
    // the two's complement produces exactly that pair with no separate compare.
    wire [24:0] rr_pos = r2_out[25:1] + {24'b0,  r2_out[0]};
    wire [24:0] rr_neg = ~r2_out[25:1] + {24'b0, ~r2_out[0]};
    wire signed [24:0] rr_v = r2_s ? $signed(rr_neg) : $signed(rr_pos);
    // Arithmetic shift, not the bit field rr_v[24:17], which would drop the
    // sign extension.
    wire signed [8:0]  exp2_k = rr_v >>> 17;
    wire        [16:0] exp2_f = rr_v[16:0];

    // log2/inv/rsqrt index the significand directly and zero-pad u's bottom two
    // bits, which is exact. rsqrt's octave parity is the top index bit, so one
    // table covers [1,2) and [2,4) with no second shifter.
    wire par = ~r2_e[0];                              // (e_a - 127) is odd
    wire [5:0]  tab_idx = r2_exp2 ? {1'b0, exp2_f[16:12]}
                        : r2_rsq  ? {par,  r2_m[14:10]}
                                  : {1'b0, r2_m[14:10]};
    wire [11:0] tab_u   = r2_exp2 ? exp2_f[11:0] : {r2_m[9:0], 2'b00};

    // The identity cases -- exp2(k), log2(2^k), inv(2^k), rsqrt(2^even) -- all
    // sit at the origin of segment 0. Constraining the polynomial through that
    // point costs 1.5 bits on the whole function, so the table is fitted freely
    // and the exact value is substituted here.
    wire tab_ident = (tab_idx[4:0] == 5'd0) & (tab_u == 12'd0)
                   & (~r2_rsq | ~par);

    // ---- DSP-E : exponent sum and alignment shift ------------- doc s3.1 --
    //   A = e_a, D = e_b, B = 2^12+1, C[23:12] = 129, C[11:0] = 385 - e_c
    //   P[23:12] = e_a+e_b+129 = u      P[11:0] = e_a+e_b+385-e_c
    // The two fields cannot collide: the low one spans [133, 892], so it never
    // carries into bit 12 and never borrows out of bit 0.
    wire [7:0] d2_ec;
    vec_delay #(.W(8), .D(1)) u_d_ec (.clk(clk), .d(vc_e), .q(d2_ec));

    wire signed [47:0] dspe_p;
    vec_dsp #(.PREADD(1), .MODEL(MODEL)) u_dsp_e (
        .clk(clk), .rst(rst), .en(1'b1),
        .a({22'b0, ea_eff}), .b(18'b000001000000000001),
        .c({24'b0, 12'd129, (12'd385 - {4'b0, d2_ec})}),
        .d({19'b0, eb_eff}),
        .alumode(4'b0000), .p(dspe_p)
    );

    // ---- cycle 3 : coefficient lookup, DSP-P A/B -------------- doc s4.3 --
    wire [1:0]  d2_fsel, d3_fsel;
    wire [11:0] d3_u;
    wire        d3_ident;
    wire [7:0]  d3_ea;
    vec_delay #(.W(2),  .D(1)) u_d_fs (.clk(clk), .d(fsel),     .q(d2_fsel));
    vec_delay #(.W(2),  .D(1)) u_d_fs3(.clk(clk), .d(d2_fsel),  .q(d3_fsel));
    vec_delay #(.W(12), .D(1)) u_d_u3 (.clk(clk), .d(tab_u),    .q(d3_u));
    vec_delay #(.W(1),  .D(1)) u_d_id (.clk(clk), .d(tab_ident), .q(d3_ident));
    vec_delay #(.W(8),  .D(2)) u_d_ea (.clk(clk), .d(raD_e),    .q(d3_ea));

    // u_d_ix is GONE: the ROM is synchronous, so its own address register is
    // that stage. tab_idx goes in raw and c0/c1/c2 still land on cycle 3.
    wire signed [21:0] tc0, tc1, tc2;
    vec_tables u_tab (.clk(clk), .fsel(d2_fsel), .idx(tab_idx),
                      .c0(tc0), .c1(tc1), .c2(tc2));

    // exp2's 1.0 and log2's integer part are exact integers at F's own weight,
    // so they ride in beside c0 rather than needing an adder.
    wire signed [29:0] c0_pin  = (d3_fsel[1] == 1'b0) ? 30'sd0 : 30'sd1048576;
    wire signed [29:0] c0_base = d3_ident ? c0_pin : {{8{tc0[21]}}, tc0};
    wire signed [29:0] c0_ext  = (d3_fsel == 2'd0) ? 30'sd1048576
                               : (d3_fsel == 2'd1)
                                 ? (($signed({{22{1'b0}}, d3_ea}) - 30'sd127) <<< 20)
                                 : 30'sd0;
    wire signed [29:0] c0_full = c0_base + c0_ext;

    wire signed [47:0] dspp_p;
    wire signed [21:0] d4_c1;
    vec_delay #(.W(22), .D(1)) u_d_c1 (.clk(clk), .d(tc1), .q(d4_c1));

    // (c1 << 16) + 2^15 is a CONCATENATION, not an add: the low sixteen bits of
    // the shifted coefficient are zero, so the rounding constant is simply bit
    // 15. Written as `+ 48'sd32768` it is a 48-bit carry chain in front of a DSP
    // input.
    vec_dsp #(.PREADD(0), .MODEL(MODEL)) u_dsp_p (
        .clk(clk), .rst(rst), .en(1'b1),
        .a({{8{tc2[21]}}, tc2}), .b({6'b0, d3_u}),
        .c({{10{d4_c1[21]}}, d4_c1, 1'b1, 15'b0}),
        .d(27'd0), .alumode(4'b0000), .p(dspp_p)
    );

    // ---- cycle 4 : DSP-E result -> shift amount and ebase -----------------
    //   cs = P[11:0] - 512 and s = 17 + cs, so s = P[11:0] - 495 directly.
    //   s < 0 IS the "addend dominates and the product is below half an ulp"
    //   case: the addend is then the correctly rounded answer, and forcing s to
    //   zero with a zeroed product makes the ordinary path emit it.
    wire [7:0]  d4_ec, d4_ea;
    wire [1:0]  d4_fsel;
    wire        d4_poly, d4_cz;
    wire [8:0]  d4_k;
    wire [9:0]  d4_rsk;
    vec_delay #(.W(8),  .D(3)) u_d_ec4 (.clk(clk), .d(vc_e),   .q(d4_ec));
    vec_delay #(.W(8),  .D(3)) u_d_ea4 (.clk(clk), .d(raD_e),  .q(d4_ea));
    vec_delay #(.W(2),  .D(3)) u_d_fs4 (.clk(clk), .d(fsel),   .q(d4_fsel));
    vec_delay #(.W(1),  .D(3)) u_d_pl4 (.clk(clk), .d(is_poly), .q(d4_poly));
    vec_delay #(.W(1),  .D(3)) u_d_cz4 (.clk(clk), .d(vc_z),   .q(d4_cz));
    vec_delay #(.W(9),  .D(2)) u_d_k4  (.clk(clk), .d(exp2_k), .q(d4_k));
    vec_delay #(.W(10), .D(3)) u_d_rk4 (.clk(clk), .d(rs_k),   .q(d4_rsk));

    wire signed [12:0] s_raw = $signed({1'b0, dspe_p[11:0]}) - 13'sd495;
    wire        byp   = s_raw[12] & ~d4_cz;                 // s_raw < 0
    wire [6:0]  s_amt = d4_cz             ? 7'd48
                      : byp               ? 7'd0
                      : (s_raw > 13'sd48) ? 7'd48 : s_raw[6:0];

    // Every one of these is a SIGNED 12-bit number, and every concatenation
    // feeding them is sign-extended with $signed(): a bare concatenation is
    // unsigned in Verilog, which would turn `107 - K` into a huge positive
    // number for negative K.
    wire signed [11:0] k_ext   = $signed({{3{d4_k[8]}},   d4_k});
    wire signed [11:0] rsk_ext = $signed({{2{d4_rsk[9]}}, d4_rsk});

    wire signed [11:0] eb_fma  = $signed({1'b0, dspe_p[23:12]}) - 12'sd286;
    wire signed [11:0] eb_byp  = $signed({4'b0, d4_ec}) - 12'sd47;
    wire signed [11:0] eb_poly = (d4_fsel == 2'd0) ? (k_ext + 12'sd107)
                               : (d4_fsel == 2'd1) ? 12'sd107
                               : (d4_fsel == 2'd2)
                                   ? (12'sd234 - $signed({4'b0, d4_ea}))
                                   : (12'sd107 - rsk_ext);
    wire signed [11:0] ebase = d4_poly ? eb_poly : (byp ? eb_byp : eb_fma);

    // ---- cycle 5 : alignment shift, ONE direction, 48 bits ---- doc s3.2 --
    // {sig_c, 32'b0} >> s. Biasing the shift by the 17 bits of headroom is what
    // turns "shift left or right depending on the sign" into a single barrel
    // shifter, and it works only because 16 significand bits plus a 32-bit
    // product is exactly the 48 bits the C port has.
    wire [15:0] d5_gc;
    wire [6:0]  d5_s;
    vec_delay #(.W(16), .D(4)) u_d_gc (.clk(clk), .d(vc_g),  .q(d5_gc));
    vec_delay #(.W(7),  .D(1)) u_d_s  (.clk(clk), .d(s_amt), .q(d5_s));

    wire [47:0] algn_in  = {d5_gc, 32'b0};
    wire [47:0] algn_out = algn_in >> d5_s;

    // The bits shifted out can only ever be d5_gc's: algn_in[31:0] is zero, so
    // nothing leaves until s > 32 and then it is exactly d5_gc[s-33:0]. Worth
    // only 34 LUT -- the 48-bit form ANDed 32 constant zeros and synthesis
    // already folded them. Kept because it says what it means.
    wire [15:0] stk_mask = ~(16'hFFFF << (d5_s - 7'd32));
    wire        algn_stk = (d5_s >= 7'd33) && |(d5_gc & stk_mask);

    // ---- cycle 7 : DSP-M A/B ---------------------------- doc s3.2 / s4.3 --
    // The FMA path is padded out to the polynomial path's depth so both retire
    // in the same cycle and the lane has one latency instead of two.
    wire signed [21:0] poly_h = dspp_p[37:16];
    wire signed [21:0] d7_h;
    wire [15:0] d7_ga, d7_gb;
    wire [11:0] d7_u;
    wire        d7_poly, d7_byp;
    vec_delay #(.W(22), .D(1)) u_d_h  (.clk(clk), .d(poly_h),  .q(d7_h));
    vec_delay #(.W(16), .D(6)) u_d_ga (.clk(clk), .d(pz ? 16'd0 : va_g), .q(d7_ga));
    vec_delay #(.W(16), .D(6)) u_d_gb (.clk(clk), .d(vb_g),    .q(d7_gb));
    vec_delay #(.W(12), .D(5)) u_d_u7 (.clk(clk), .d(tab_u),   .q(d7_u));
    vec_delay #(.W(1),  .D(6)) u_d_pl7(.clk(clk), .d(is_poly), .q(d7_poly));
    vec_delay #(.W(1),  .D(3)) u_d_by7(.clk(clk), .d(byp),     .q(d7_byp));

    wire signed [29:0] dspm_a = d7_poly ? {{8{d7_h[21]}}, d7_h} : {14'b0, d7_gb};
    wire signed [17:0] dspm_b = d7_poly ? {6'b0, d7_u}
                                        : (d7_byp ? 18'sd0 : {2'b0, d7_ga});

    // ---- cycle 8 : DSP-M C and ALUMODE ------------------------------------
    // The aligned addend goes on C as a raw 48-bit pattern. It can set bit 47,
    // which reads as negative -- that is fine: the ALU is modular, the sum is
    // bounded below 2^48, and the sign is recovered in cycle 10 from `s` rather
    // than from the bit.
    wire [47:0] d8_algn;
    wire signed [29:0] d8_c0;
    wire        d8_poly, d8_neg;
    vec_delay #(.W(48), .D(3)) u_d_al (.clk(clk), .d(algn_out), .q(d8_algn));
    vec_delay #(.W(30), .D(5)) u_d_c0 (.clk(clk), .d(c0_full),  .q(d8_c0));
    vec_delay #(.W(1),  .D(7)) u_d_pl8(.clk(clk), .d(is_poly),  .q(d8_poly));
    vec_delay #(.W(1),  .D(7)) u_d_ng8(.clk(clk), .d(fma_neg),  .q(d8_neg));

    wire signed [47:0] dspm_p;
    vec_dsp #(.PREADD(0), .MODEL(MODEL)) u_dsp_m (
        .clk(clk), .rst(rst), .en(1'b1),
        .a(dspm_a), .b(dspm_b),
        .c(d8_poly ? {{2{d8_c0[29]}}, d8_c0, 1'b1, 15'b0} : $signed(d8_algn)),
        .d(27'd0),
        .alumode((~d8_poly & d8_neg) ? 4'b0011 : 4'b0000),
        .p(dspm_p)
    );

    // ---- cycle 10 : magnitude and sign out of DSP-M ----------- doc s3.2 --
    // res_neg = neg && (s != 0) && P[47]. The `s != 0` term is the subtlety: at
    // s == 0 the addend sits at [47:32] and is necessarily larger than the
    // 32-bit product, so P[47] is a value bit there, not a sign bit. Wherever it
    // IS a sign bit the magnitude is below 2^32, so recovering it is a 33-bit
    // two's complement rather than a 48-bit one.
    wire        dA_poly, dA_neg, dA_sab, dA_scc, dA_snz, dA_stk;
    wire [1:0]  dA_fsel;
    wire        dA_ssign;
    wire signed [11:0] dA_eb;
    vec_delay #(.W(1),  .D(9)) u_d_plA(.clk(clk), .d(is_poly), .q(dA_poly));
    vec_delay #(.W(1),  .D(9)) u_d_ngA(.clk(clk), .d(fma_neg), .q(dA_neg));
    vec_delay #(.W(1),  .D(9)) u_d_sbA(.clk(clk), .d(sign_ab), .q(dA_sab));
    vec_delay #(.W(1),  .D(9)) u_d_scA(.clk(clk), .d(vcU[23]),  .q(dA_scc));
    // D one shorter: spec_* is now registered, so this line gives back the flop.
    vec_delay #(.W(1),  .D(8)) u_d_ssA(.clk(clk), .d(spec_sign), .q(dA_ssign));
    vec_delay #(.W(2),  .D(9)) u_d_fsA(.clk(clk), .d(fsel),    .q(dA_fsel));
    vec_delay #(.W(1),  .D(6)) u_d_snA(.clk(clk), .d(|s_amt),  .q(dA_snz));
    vec_delay #(.W(1),  .D(5)) u_d_stA(.clk(clk), .d(algn_stk), .q(dA_stk));
    vec_delay #(.W(12), .D(6)) u_d_ebA(.clk(clk), .d(ebase),   .q(dA_eb));

    wire        res_neg  = ~dA_poly & dA_neg & dA_snz & dspm_p[47];
    wire [47:0] fma_mag  = res_neg ? {15'b0, (~dspm_p[32:0] + 33'd1)} : dspm_p;

    wire signed [29:0] poly_f   = dspm_p[45:16];
    wire        poly_neg = poly_f[29];
    wire [29:0] poly_mag = poly_neg ? (~poly_f + 30'd1) : poly_f;

    reg        s10_sign, s10_stk;
    reg [47:0] s10_mag;
    reg signed [11:0] s10_eb;

    always @(posedge clk) begin
        s10_mag  <= dA_poly ? {18'b0, poly_mag} : fma_mag;
        s10_eb   <= dA_eb;
        s10_stk  <= dA_poly ? 1'b0 : dA_stk;
        s10_sign <= dA_poly
                  ? ((dA_fsel == 2'd1) ? poly_neg : dA_ssign)
                  : (dA_neg ? (res_neg ? dA_sab : dA_scc) : dA_sab);
    end

    // ---- cycle 11 : lead1  ->  12 : normalise  ->  13 : round -------------
    // mx_lead1 is smear-isolate-encode, log depth. A search loop carrying a
    // `found` flag over 48 bits synthesises as a 48-deep LUT chain -- the shape
    // that cost this design ~68 MHz once already. See mx_fpacc.v.
    wire [5:0] lz_pos;
    wire       lz_nz;
    mx_lead1 #(.W(48)) u_l1 (.x(s10_mag), .pos(lz_pos), .nz(lz_nz));

    reg        s11_sign, s11_stk, s11_nz;
    reg [5:0]  s11_pos;
    reg [47:0] s11_mag;
    reg signed [11:0] s11_eb;

    always @(posedge clk) begin
        s11_mag <= s10_mag; s11_pos <= lz_pos;  s11_nz   <= lz_nz;
        s11_eb  <= s10_eb;  s11_stk <= s10_stk; s11_sign <= s10_sign;
    end

    wire [47:0] nrm = s11_mag << (6'd47 - s11_pos);

    reg        s12_sign, s12_g, s12_s, s12_nz;
    reg [15:0] s12_sig;
    reg [5:0]  s12_pos;
    reg signed [11:0] s12_eb;

    always @(posedge clk) begin
        s12_sig <= nrm[47:32];
        s12_g   <= nrm[31];
        s12_s   <= (|nrm[30:0]) | s11_stk;
        s12_pos <= s11_pos; s12_eb <= s11_eb;
        s12_nz  <= s11_nz;  s12_sign <= s11_sign;
    end

    // ---- round to nearest even, then the exponent bounds -----------------
    wire        rnd_up = s12_g & (s12_s | s12_sig[0]);
    wire [16:0] sig_r  = {1'b0, s12_sig} + {16'b0, rnd_up};
    wire        rcarry = sig_r[16];
    wire [14:0] frac   = rcarry ? 15'd0 : sig_r[14:0];
    wire signed [11:0] e_fin = $signed({6'b0, s12_pos}) + s12_eb
                             + (rcarry ? 12'sd1 : 12'sd0);

    wire d12_nan, d12_inf, d12_zero, d12_ssign, d12_pred, d12_canc;
    vec_delay #(.W(1), .D(12)) u_d_can (.clk(clk), .d(cancels),   .q(d12_canc));
    vec_delay #(.W(1), .D(11)) u_d_nan (.clk(clk), .d(spec_nan),  .q(d12_nan));
    vec_delay #(.W(1), .D(11)) u_d_inf (.clk(clk), .d(spec_inf),  .q(d12_inf));
    vec_delay #(.W(1), .D(11)) u_d_zro (.clk(clk), .d(spec_zero), .q(d12_zero));
    // 11 like nan/inf/zero: spec_sign is registered too, and at 12 it arrived a
    // cycle late -- infinity and zero came out with the wrong sign.
    vec_delay #(.W(1), .D(11)) u_d_ssg (.clk(clk), .d(spec_sign), .q(d12_ssign));
    vec_delay #(.W(1), .D(12)) u_d_prd (.clk(clk), .d(predD & is_cmp), .q(d12_pred));

    always @(posedge clk) begin
        if (rst) begin
            out_valid <= 1'b0;
            out       <= 24'd0;
            out_pred  <= 1'b0;
        end else begin
            out_valid <= (PIPE_MUX != 0) ? vpipe[13] : vpipe[12];
            out_pred  <= d12_pred;
            if (d12_nan)                 out <= E8_NAN;
            else if (d12_inf)            out <= {d12_ssign, 8'hFF, 15'd0};
            else if (d12_zero)           out <= {d12_ssign, 23'd0};
            else if (~s12_nz)            out <= {s12_sign & ~d12_canc, 23'd0};
            else if (e_fin >= 12'sd255)  out <= {s12_sign, 8'hFF, 15'd0};
            else if (e_fin <= 12'sd0)    out <= {s12_sign, 23'd0};
            else                         out <= {s12_sign, e_fin[7:0], frac};
        end
    end

endmodule

`default_nettype wire

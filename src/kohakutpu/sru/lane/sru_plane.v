// One SRU pool lane: per issue either an E8M15 FMA (op 0: out = a*b + c) or a
// table function (op 1: out = f(x), x = a, k = b; c ignored), on one DSP48E2,
// with ONE latency for both so a modulo schedule sees a single lane kind and
// never needs a bubble. References: scripts/py/sru/e8.py (fma), fn.py (lane).
//
// E8M15: S1 E8 M15, sig = {1, M}; E = 0 zero, E = 255 inf (M == 0) / NaN. No
// subnormals: a result below 2^-126 after rounding is a signed zero, one at or
// above 2^128 a signed infinity; NaN is 0x7FC000. One rounding, to nearest even.
//
// FMA WINDOW. The DSP holds the 32-bit product at bits [31:0] and the addend at
// {sig_c, 32'b0} >> s, s = ea + eb - ec - 110 (the addend's exponent gap to the
// product, biased by 17). s in [0, 48] runs through the window; s > 48 leaves
// only a sticky. s < 0 (addend dominant) is exact as the addend alone, except at
// s = -1 with an effective subtract and a power-of-two addend, where the product
// can pull the result one binade down: there the addend is 2^48, which the
// 48-bit DSP subtracts modulo 2^48 as C = 0, so P = 2^48 - product exactly.
// NEGATION. P < 0 only when the product wins a subtraction, |P| < 2^33. When
// addend bits were shifted out below the window the true magnitude is |P| minus
// a fraction, so the floor is ~P and the fraction becomes the sticky: the +1 of
// the two's complement is dropped exactly then, which keeps ties exact.
//
// FUNCTION: y = (-1)^sg (c0 + c1*t) 2^(es + n) from a table segment.
//   v = trunc(x * 2^pre), Q9.24, magnitude clamped to 2^33 - 1
//   EXP  (fm 0): w = v - (floor(k) << 24), floor(k) clamped to [-512, 511];
//                n = w >> 24, segment = fraction[23:14], t = fraction[13:0]
//   FIX  (fm 1): v in [-16, 16): segment = (v + 16) in 1/32 steps, n = 0;
//                below: segment 0, t 0; above: segment 1023, t max
//   MANT (fm 2): x = m 2^e: segment = m[14:5], t = m[4:0] << 9, n = +-e;
//                the result's sign is x's times the entry's
//   HALF (fm 3): e = 2q + r: segment = {r, m[14:6]}, t = m[5:0] << 8,
//                n = +-q; a negative x is NaN
//   (neg: n = -e / -q.) MANT / HALF: a zero is infinite with neg, else zero;
//   an infinity is zero with neg, else infinite; both keep x's sign.
// HAS_MANT builds MANT / HALF on the RAM table. MROM builds the mantissa ROM
// (sru_mant_rom.vh: 1/m, 1/sqrt(m 2^r), 128 segments each, LUT) read when
// `mrom` is set: index {fm[0], r, m[14:8]}, t = m[7:0] << 6, on any lane kind
// (HAS_FN = 0 too) -- the finalise's RCP / RSQRT on pool lane 0.
// Entry: [35] sg, [34:29] es (signed; -32 = a zero segment), [28:13] c0's
// fraction (c0 = 1.f), [12:0] c1 (signed, unit 2^(cs-15)). c0 + c1*t runs on
// the FMA's DSP (A = t << cs, B = c1, C = c0 << 13, through the addend shifter
// at a fixed 18); Y in [2^28, 2^31) goes through the FMA's normalise and round
// with its lead-one at 28..30 and exponent field pos + 98 + es + n.
//
// The function front end (map, segment, table read) reaches the DSP 4 cycles
// after the FMA's (decode, gap), so the FMA's decoded operands wait 4 cycles in
// flops. Every register carries shreg_extract "no": under sru_cu the module
// attribute alone let the lane's fixed delay chains become SRLs (~92 LUT a
// lane measured), and LUT outranks FF.
//
// HAS_FN = 0 builds the FMA front end alone (op ignored, tab_addr 0).
// TAB "ram": the 1,024 x 36 table is outside (a RAMB36 per lane pair);
// tab_addr leaves registered in cycle 5, tab_q is read in cycle 7. TAB "rom":
// exp2 alone, 128 segments in LUT (sru_exp2_rom.vh), fix and cs ignored.
//
// PIPELINE (registers valid in cycle n; issue in cycle 0, out in cycle 16):
//   FMA   1 inputs  2 decode, ea+eb  3 gap s  4..7 wait  [7: DSP A/B]
//   FN    1 inputs  2 decode  3 |v|  4 w  5 segment  7 entry  [7: DSP A/B]
//   both  8 addend / c0 [8: DSP C]  10 DSP P, negate  11 magnitude  12 smear
//         13 lead-one, sticky  14 normalise  15 round  16 exponent, assemble

`default_nettype none

(* shreg_extract = "no" *)
module sru_plane #(
    parameter integer MODEL    = 0,
    parameter integer HAS_FN   = 1,
    parameter         TAB      = "ram",
    parameter integer HAS_MANT = 0,     // MANT / HALF on the RAM table
    parameter integer MROM     = 0      // the mantissa ROM (RCP / RSQRT)
)(
    input  wire              clk,
    input  wire              rst,

    input  wire              in_valid,
    input  wire              op,        // 0 FMA, 1 function
    input  wire       [23:0] a,         // FMA a; function x
    input  wire       [23:0] b,         // FMA b; function k
    input  wire       [23:0] c,
    input  wire        [1:0] fm,        // function mode: EXP FIX MANT HALF
    input  wire              neg,       // MANT / HALF: n negated
    input  wire              mrom,      // MANT / HALF: the mantissa ROM
    input  wire signed [5:0] pre,
    input  wire        [1:0] cs,

    output wire        [9:0] tab_addr,
    input  wire       [35:0] tab_q,

    output reg               out_valid,
    output reg        [23:0] out
);
    localparam integer LAT = 16;
    localparam [23:0] E8_NAN = 24'h7FC000;
    localparam integer ROM = (TAB == "rom") ? 1 : 0;
    localparam integer FNP = ((HAS_FN != 0) || (MROM != 0)) ? 1 : 0;
    localparam integer MNT = (((HAS_FN != 0) && (HAS_MANT != 0) && (ROM == 0)) || (MROM != 0)) ? 1 : 0;

    integer i;

    (* shreg_extract = "no" *) reg [LAT-2:0] vp;
    always @(posedge clk) begin
        if (rst) begin
            vp        <= {(LAT-1){1'b0}};
            out_valid <= 1'b0;
        end else begin
            vp        <= {vp[LAT-3:0], in_valid};
            out_valid <= vp[LAT-2];
        end
    end

    // ---- 1: inputs ---------------------------------------------------------
    (* shreg_extract = "no" *) reg [23:0]       a1, b1, c1;
    (* shreg_extract = "no" *) reg             fix1, mt1, half1, neg1, mr1;
    (* shreg_extract = "no" *) reg signed [5:0] pre1;
    (* shreg_extract = "no" *) reg [1:0]        cs1;
    (* shreg_extract = "no" *) reg             op1, op2, op3, op4, op5, op6, op7;
    always @(posedge clk) begin
        a1    <= a;
        b1    <= b;
        c1    <= c;
        mt1   <= (MNT != 0) & fm[1];
        fix1  <= (ROM == 0) & ~fm[1] & fm[0];
        half1 <= fm[0];
        neg1  <= neg;
        mr1   <= (MROM != 0) & mrom;
        pre1  <= pre;
        cs1   <= (ROM == 0) ? cs : 2'd0;
        op1   <= (FNP != 0) & op;
        op2   <= op1; op3 <= op2; op4 <= op3; op5 <= op4; op6 <= op5; op7 <= op6;
    end

    // ======================================================== FMA front end
    // ---- 2: decode ---------------------------------------------------------
    wire [7:0] ea1 = a1[22:15];
    wire [7:0] eb1 = b1[22:15];
    wire [7:0] ec1 = c1[22:15];
    wire za1  = (ea1 == 8'd0);
    wire zb1  = (eb1 == 8'd0);
    wire zc1  = (ec1 == 8'd0);
    wire na1  = (&ea1) &  (|a1[14:0]);
    wire ia1  = (&ea1) & ~(|a1[14:0]);
    wire nb1  = (&eb1) &  (|b1[14:0]);
    wire ib1  = (&eb1) & ~(|b1[14:0]);
    wire nc1  = (&ec1) &  (|c1[14:0]);
    wire ic1  = (&ec1) & ~(|c1[14:0]);
    wire sp1  = a1[23] ^ b1[23];
    wire sc1  = c1[23];
    wire pn1  = na1 | nb1 | (ia1 & zb1) | (za1 & ib1);
    wire pi1  = (ia1 | ib1) & ~pn1;
    wire fn1  = pn1 | nc1 | (pi1 & ic1 & (sp1 != sc1));
    wire fi1  = (pi1 | ic1) & ~fn1;

    (* shreg_extract = "no" *) reg [15:0] ga2, gb2, gc2;
    (* shreg_extract = "no" *) reg [8:0]  ep2;
    (* shreg_extract = "no" *) reg [7:0]  ec2;
    (* shreg_extract = "no" *) reg       sp2, sc2, pz2, zc2, mc0_2, zs2, fn2, fi2, fs2;
    always @(posedge clk) begin
        ga2   <= {1'b1, a1[14:0]};
        gb2   <= {1'b1, b1[14:0]};
        gc2   <= {1'b1, c1[14:0]};
        ep2   <= {1'b0, ea1} + {1'b0, eb1};
        ec2   <= ec1;
        sp2   <= sp1;
        // A zero addend takes the product's sign: the lane never subtracts 0.
        sc2   <= zc1 ? sp1 : sc1;
        pz2   <= za1 | zb1;
        zc2   <= zc1;
        mc0_2 <= (c1[14:0] == 15'd0);
        zs2   <= sp1 & sc1;              // sign of (+-0)*x + (+-0)
        fn2   <= fn1;
        fi2   <= fi1;
        fs2   <= pi1 ? sp1 : sc1;
    end

    // ---- 3: alignment amount -----------------------------------------------
    (* shreg_extract = "no" *) reg signed [10:0] s3;
    (* shreg_extract = "no" *) reg [15:0] ga3, gb3, gc3;
    (* shreg_extract = "no" *) reg [8:0]  ep3;
    (* shreg_extract = "no" *) reg [7:0]  ec3;
    (* shreg_extract = "no" *) reg       sp3, sc3, pz3, zc3, mc0_3, zs3, fn3, fi3, fs3;
    always @(posedge clk) begin
        s3    <= $signed({2'b00, ep2}) - $signed({3'b000, ec2}) - 11'sd110;
        ga3   <= ga2;
        gb3   <= gb2;
        gc3   <= gc2;
        ep3   <= ep2;
        ec3   <= ec2;
        sp3   <= sp2;
        sc3   <= sc2;
        pz3   <= pz2;
        zc3   <= zc2;
        mc0_3 <= mc0_2;
        zs3   <= zs2;
        fn3   <= fn2;
        fi3   <= fi2;
        fs3   <= fs2;
    end

    wire esub3 = sp3 ^ sc3;
    wire neg3  = s3[10];
    wire spec3 = ~pz3 & ~zc3 & esub3 & mc0_3 & (s3 == -11'sd1);
    wire byp3  = pz3 | (~zc3 & neg3 & ~spec3);
    wire [5:0] sh3 = (byp3 | neg3)     ? 6'd0
                   : (s3 > 11'sd48)    ? 6'd48
                                       : s3[5:0];
    // Exponent field of window bit 0: e_out = lead-one position + eb.
    wire signed [11:0] eb3 = byp3 ? ($signed({4'b0000, ec3}) - 12'sd47)
                                  : ($signed({3'b000, ep3}) - 12'sd157);

    // Flags carried to the end: {zz, zs, fn, fi, fs, sp, sc, esub, top, fz, eb}.
    localparam integer FW = 22;
    wire [FW-1:0] ff3 = {pz3 & zc3, zs3, fn3, fi3, fs3, sp3, sc3, esub3,
                         (sh3 == 6'd0), 1'b0, eb3};

    // ---- 4..7: wait for the function front end ------------------------------
    localparam integer DW = 16 + 16 + 16 + 6 + 1 + 1 + FW;
    (* shreg_extract = "no" *) reg [DW-1:0] dl [4:7];
    always @(posedge clk) begin
        dl[4] <= {ga3, gb3, gc3, sh3, byp3 | pz3, zc3 | spec3, ff3};
        for (i = 5; i <= 7; i = i + 1) begin
            dl[i] <= dl[i-1];
        end
    end
    wire [15:0]   ga7  = dl[7][DW-1    -: 16];
    wire [15:0]   gb7  = dl[7][DW-17   -: 16];
    wire [15:0]   gc7  = dl[7][DW-33   -: 16];
    wire [5:0]    sh7  = dl[7][DW-49   -: 6];
    wire          za7  = dl[7][FW+1];
    wire          zcd7 = dl[7][FW];
    wire [FW-1:0] ff7  = dl[7][FW-1:0];

    // =================================================== function front end
    wire [35:0]        q7;
    wire [13:0]        t7;
    wire signed [10:0] n7;
    wire               nan7, inf7, zo7, fl7, sx7;
    wire [1:0]         cs7;

    generate
    if (FNP != 0) begin : g_fn
        `include "sru_mant_rom.vh"
        // mode, ROM select and slope scale to the merge (5) and the entry (7)
        (* shreg_extract = "no" *) reg mt2, mt3, mt4, mr2, mr3, mr4, mr5, mr6, mr7;
        (* shreg_extract = "no" *) reg [1:0] cs2, cs3, cs4;
        always @(posedge clk) begin
            mt2 <= mt1; mt3 <= mt2; mt4 <= mt3;
            mr2 <= mr1; mr3 <= mr2; mr4 <= mr3; mr5 <= mr4; mr6 <= mr5; mr7 <= mr6;
            cs2 <= cs1; cs3 <= cs2; cs4 <= cs3;
        end

        // EXP / FIX (HAS_FN) and MANT / HALF (MNT): segment, t, n, flags in 4
        wire [9:0]         fa_4, ma_4;
        wire [13:0]        ft_4, mt_4;
        wire signed [10:0] fn_4, mn_4;
        wire               fnan_4, mnan_4, minf_4, mzo_4, mfl_4, msx_4;

        if (MNT != 0) begin : g_mant
            // ---- 2: x = m 2^e, e = 2q + r in HALF -------------------------
            wire [7:0]         mx1  = a1[22:15];
            wire               mz1  = (mx1 == 8'd0);
            wire               mi1  = (&mx1) & ~(|a1[14:0]);
            wire               mn1  = (&mx1) &  (|a1[14:0]);
            wire signed [8:0]  me1  = $signed({1'b0, mx1}) - 9'sd127;
            wire signed [8:0]  mq1  = half1 ? (me1 >>> 1) : me1;
            wire signed [10:0] mqx1 = {{2{mq1[8]}}, mq1};
            wire               mrr1 = half1 & me1[0];
            (* shreg_extract = "no" *) reg [9:0]         ma2, ma3, ma4;
            (* shreg_extract = "no" *) reg [13:0]        mt2_, mt3_, mt4_;
            (* shreg_extract = "no" *) reg signed [10:0] mn2, mn3, mn4;
            (* shreg_extract = "no" *) reg [4:0]         mf2, mf3, mf4;   // nan inf zero flip sign
            always @(posedge clk) begin
                ma2  <= mr1   ? {1'b0, half1, mrr1, a1[14:8]}
                      : half1 ? {mrr1, a1[14:6]} : a1[14:5];
                mt2_ <= mr1   ? {a1[7:0], 6'd0}
                      : half1 ? {a1[5:0], 8'd0} : {a1[4:0], 9'd0};
                mn2  <= neg1 ? -mqx1 : mqx1;
                mf2  <= {mn1 | (half1 & a1[23] & ~mz1),
                         mz1 ? neg1 : (mi1 & ~neg1),
                         mz1 ? ~neg1 : (mi1 & neg1),
                         ~half1 & a1[23], a1[23]};
                ma3 <= ma2;  mt3_ <= mt2_;  mn3 <= mn2;  mf3 <= mf2;
                ma4 <= ma3;  mt4_ <= mt3_;  mn4 <= mn3;  mf4 <= mf3;
            end
            assign ma_4 = ma4;
            assign mt_4 = mt4_;
            assign mn_4 = mn4;
            assign {mnan_4, minf_4, mzo_4, mfl_4, msx_4} = mf4;
        end else begin : g_nomant
            assign ma_4 = 10'd0;
            assign mt_4 = 14'd0;
            assign mn_4 = 11'sd0;
            assign {mnan_4, minf_4, mzo_4, mfl_4, msx_4} = 5'd0;
        end

        if (HAS_FN != 0) begin : g_ef
        // ---- 2: decode x and floor(k) --------------------------------------
        wire [7:0] ex1  = a1[22:15];
        wire       nx1  = (&ex1) &  (|a1[14:0]);
        wire       ix1  = (&ex1) & ~(|a1[14:0]);
        // r = 17 - (ex + pre - 118): the right shift of {sig, 17'b0} that gives v.
        wire signed [9:0] rr1 = 10'sd135 - $signed({2'b00, ex1}) - $signed({{4{pre1[5]}}, pre1});

        wire [7:0]  ek1  = b1[22:15];
        wire [15:0] gk1  = {1'b1, b1[14:0]};
        wire [3:0]  ksh1 = 8'd142 - ek1;                    // 7..15 inside [127, 135]
        wire [8:0]  kip1 = gk1 >> ksh1;
        wire        kfr1 = |(gk1 & ~(16'hFFFF << ksh1));
        (* shreg_extract = "no" *) reg signed [9:0] kf1;
        always @(*) begin
            if (ek1 == 8'd0) begin
                kf1 = 10'sd0;
            end else if ((ek1 > 8'd135) || (&ek1)) begin
                kf1 = b1[23] ? -10'sd512 : 10'sd511;
            end else if (ek1 < 8'd127) begin
                kf1 = b1[23] ? -10'sd1 : 10'sd0;
            end else begin
                kf1 = b1[23] ? -$signed({1'b0, kip1} + {9'd0, kfr1}) : $signed({1'b0, kip1});
            end
        end
        wire nk1 = (&ek1) & (|b1[14:0]);

        (* shreg_extract = "no" *) reg [15:0]       g2;
        (* shreg_extract = "no" *) reg [5:0]        r2;
        (* shreg_extract = "no" *) reg             sat2, zero2, sx2, nan2, fix2;
        (* shreg_extract = "no" *) reg signed [9:0] kf2;
        always @(posedge clk) begin
            g2    <= {1'b1, a1[14:0]};
            r2    <= rr1[5:0];
            sat2  <= ix1 | (rr1 < 10'sd0);
            zero2 <= (ex1 == 8'd0) | (rr1 > 10'sd33);
            sx2   <= a1[23];
            nan2  <= nx1 | (~fix1 & nk1);
            fix2  <= fix1;
            kf2   <= kf1;
        end

        // ---- 3: |v|, and -floor(k) (0 in FIX) ------------------------------
        (* shreg_extract = "no" *) reg [32:0]        vab3;
        (* shreg_extract = "no" *) reg              sx3, nan3, fix3;
        (* shreg_extract = "no" *) reg signed [10:0] nk3;
        always @(posedge clk) begin
            vab3 <= sat2 ? {33{1'b1}} : zero2 ? 33'd0 : ({g2, 17'd0} >> r2);
            sx3  <= sx2;
            nan3 <= nan2;
            fix3 <= fix2;
            nk3  <= fix2 ? 11'sd0 : -$signed({kf2[9], kf2});
        end

        // ---- 4: w = v - (floor(k) << 24), v = +-|v| through the carry-in ----
        (* shreg_extract = "no" *) reg signed [34:0] w4;
        (* shreg_extract = "no" *) reg              nan4, fix4;
        always @(posedge clk) begin
            w4   <= ({2'b00, vab3} ^ {35{sx3}}) + {nk3, 24'd0} + {34'd0, sx3};
            nan4 <= nan3;
            fix4 <= fix3;
        end

        // ---- segment, t, n ------------------------------------------------
        wire        in4 = (w4[34:28] == {7{w4[28]}});
        wire [9:0]  fa4 = in4 ? {~w4[28], w4[27:19]} : (w4[34] ? 10'd0 : 10'd1023);
        wire [13:0] ft4 = in4 ? w4[18:5] : (w4[34] ? 14'd0 : 14'h3FFF);
        wire [9:0]  ea4 = (ROM != 0) ? {3'd0, w4[23:17]} : w4[23:14];
        wire [13:0] et4 = (ROM != 0) ? w4[16:3] : w4[13:0];
        assign fa_4   = fix4 ? fa4 : ea4;
        assign ft_4   = fix4 ? ft4 : et4;
        assign fn_4   = fix4 ? 11'sd0 : w4[34:24];
        assign fnan_4 = nan4;
        end else begin : g_noef
            assign fa_4   = 10'd0;
            assign ft_4   = 14'd0;
            assign fn_4   = 11'sd0;
            assign fnan_4 = 1'b0;
        end

        // ---- 5: the mode's segment, t, n and flags --------------------------
        (* shreg_extract = "no" *) reg [9:0]         ta5;
        (* shreg_extract = "no" *) reg [13:0]        t5, t6, tt7;
        (* shreg_extract = "no" *) reg signed [10:0] n5, n6, nn7;
        (* shreg_extract = "no" *) reg [4:0]         x5, x6, x7;     // nan inf zero flip sign
        (* shreg_extract = "no" *) reg [1:0]         cs5, cs6, cc7;
        always @(posedge clk) begin
            ta5 <= mt4 ? ma_4 : fa_4;
            t5  <= mt4 ? mt_4 : ft_4;
            n5  <= mt4 ? mn_4 : fn_4;
            x5  <= mt4 ? {mnan_4, minf_4, mzo_4, mfl_4, msx_4} : {fnan_4, 4'd0};
            cs5 <= mr4 ? MANT_ROM_CS : cs4;
            t6  <= t5;   n6  <= n5;   x6 <= x5;   cs6 <= cs5;
            tt7 <= t6;   nn7 <= n6;   x7 <= x6;   cc7 <= cs6;
        end
        assign tab_addr = ta5;
        assign t7   = tt7;
        assign n7   = nn7;
        assign {nan7, inf7, zo7, fl7, sx7} = x7;
        assign cs7  = cc7;

        // ---- 7: the entry --------------------------------------------------
        // LUT ROMs, not RAMB18s: the ROM kinds exist to cost no block RAM.
        wire [35:0] eq7;
        if ((HAS_FN != 0) && (ROM != 0)) begin : g_rom
            `include "sru_exp2_rom.vh"
            (* rom_style = "distributed" *) reg [35:0] rq6;
            (* shreg_extract = "no" *) reg [35:0] rq7;
            always @(posedge clk) begin
                rq6 <= exp2_rom(ta5[6:0]);
                rq7 <= rq6;
            end
            assign eq7 = rq7;
        end else begin : g_ram
            assign eq7 = tab_q;
        end
        if (MROM != 0) begin : g_mrom
            (* rom_style = "distributed" *) reg [35:0] mq6;
            (* shreg_extract = "no" *) reg [35:0] mq7;
            always @(posedge clk) begin
                mq6 <= mant_rom(ta5[8:0]);
                mq7 <= mq6;
            end
            assign q7 = mr7 ? mq7 : eq7;
        end else begin : g_nomrom
            assign q7 = eq7;
        end
    end else begin : g_nofn
        assign tab_addr = 10'd0;
        assign q7   = 36'd0;
        assign t7   = 14'd0;
        assign n7   = 11'sd0;
        assign {nan7, inf7, zo7, fl7, sx7} = 5'd0;
        assign cs7  = 2'd0;
    end
    endgenerate

    // Function flags: no sticky, never a subtract; a zero segment forces a
    // signed zero (fz), its sign the entry's. MANT / HALF: the entry's sign
    // flipped by x's (fl); a special's sign is x's.
    wire               sg7  = q7[35] ^ fl7;
    wire signed [5:0]  es7  = q7[34:29];
    wire signed [11:0] ebf7 = 12'sd98 + {{6{es7[5]}}, es7} + {n7[10], n7};
    wire [FW-1:0] gf7 = {1'b1, zo7 ? sx7 : sg7, nan7, inf7, sx7, sg7, sg7, 1'b0,
                         1'b0, (es7 == -6'sd32) | zo7, ebf7};

    // ===================================================== the shared lane
    // ---- 7 -> 8: DSP A/B (cycle 7), addend and flags registered ------------
    wire [16:0] dsp_a = op7 ? {1'b0, {2'd0, t7} << cs7} : {1'b0, ga7};
    wire [16:0] dsp_b = op7 ? {{4{q7[12]}}, q7[12:0]}  : {1'b0, gb7};

    (* shreg_extract = "no" *) reg [16:0] gc8;
    (* shreg_extract = "no" *) reg [5:0]  sh8;
    (* shreg_extract = "no" *) reg       zcd8, sub8;
    (* shreg_extract = "no" *) reg [FW-1:0] fl [8:15];
    always @(posedge clk) begin
        gc8   <= op7 ? {1'b1, q7[28:13]} : {gc7, 1'b0};
        sh8   <= op7 ? 6'd18 : sh7;
        zcd8  <= ~op7 & zcd7;
        sub8  <= ~op7 & ff7[FW-8];
        fl[8] <= op7 ? gf7 : ff7;
        for (i = 9; i <= 15; i = i + 1) begin
            fl[i] <= fl[i-1];
        end
    end
    `define F_ZZ(n)  fl[n][21]
    `define F_ZS(n)  fl[n][20]
    `define F_FN(n)  fl[n][19]
    `define F_FI(n)  fl[n][18]
    `define F_FS(n)  fl[n][17]
    `define F_SP(n)  fl[n][16]
    `define F_SC(n)  fl[n][15]
    `define F_SUB(n) fl[n][14]
    `define F_TOP(n) fl[n][13]
    `define F_FZ(n)  fl[n][12]
    `define F_EB(n)  fl[n][11:0]

    // ---- 8: the addend {g, 31'b0} >> sh (DSP C) -----------------------------
    // FMA: g = {sig_c, 0}; function: g = c0 at sh 18, i.e. c0 << 13.
    wire [47:0] al8 = {gc8, 31'd0} >> sh8;
    wire [5:0]  sx8 = sh8 - 6'd31;
    wire [16:0] lm8 = ~(17'h1FFFF << sx8);
    wire        stk8 = (sh8 > 6'd31) & (|(gc8 & lm8)) & ~zcd8;

    wire [47:0] p10;
    sru_dsp #(.MODEL(MODEL)) u_dsp (
        .clk(clk), .rst(rst),
        .a(dsp_a), .b(dsp_b), .zero_a(~op7 & za7),
        .c(al8), .zero_c(zcd8), .sub(sub8),
        .p(p10)
    );

    (* shreg_extract = "no" *) reg stk9, stk10, stk11, stk12;
    always @(posedge clk) begin
        stk9  <= stk8;
        stk10 <= stk9;
        stk11 <= stk10;
        stk12 <= stk11;
    end

    // ---- 10 -> 11: magnitude and sign --------------------------------------
    wire        neg10 = `F_SUB(10) & ~`F_TOP(10) & p10[47];
    wire [32:0] n10   = (p10[32:0] ^ {33{neg10}}) + {32'd0, neg10 & ~stk10};

    (* shreg_extract = "no" *) reg [47:0] mag11;
    (* shreg_extract = "no" *) reg       sg11;
    always @(posedge clk) begin
        mag11 <= neg10 ? {15'd0, n10} : p10;
        sg11  <= neg10 ? `F_SP(10) : `F_SC(10);
    end

    // ---- 11 .. 14: lead-one, sticky, normalise ------------------------------
    // Smear-isolate-encode, never a search loop (mx_fpacc.v, mx_lead1).
    (* shreg_extract = "no" *) reg [47:0] y12, mag12, mag13;
    (* shreg_extract = "no" *) reg [47:0] ys;
    integer s;
    always @(*) begin
        ys = mag11;
        for (s = 1; s < 48; s = s * 2) begin
            ys = ys | (ys >> s);
        end
    end

    wire [47:0] oh12 = y12 & ~(y12 >> 1);
    (* shreg_extract = "no" *) reg [5:0]  pe12;
    integer k, bb;
    always @(*) begin
        for (k = 0; k < 6; k = k + 1) begin
            pe12[k] = 1'b0;
            for (bb = 0; bb < 48; bb = bb + 1) begin
                if ((bb >> k) & 1) begin
                    pe12[k] = pe12[k] | oh12[bb];
                end
            end
        end
    end

    (* shreg_extract = "no" *) reg [5:0]  pos13, pos14;
    (* shreg_extract = "no" *) reg       st13, nz13, st14, nz14, g14;
    (* shreg_extract = "no" *) reg [15:0] sig14;
    (* shreg_extract = "no" *) reg       sg12, sg13, sg14, sg15;

    // Window bits below the guard: positions <= pos - 17, i.e. y >> 17.
    wire [63:0] nrm13 = {16'd0, mag13} << (~pos13);

    always @(posedge clk) begin
        y12   <= ys;
        mag12 <= mag11;
        pos13 <= pe12;
        st13  <= (|(mag12 & (y12 >> 17))) | stk12;
        nz13  <= y12[0];
        mag13 <= mag12;
        sig14 <= nrm13[63:48];
        g14   <= nrm13[47];
        sg12  <= sg11;
        sg13  <= sg12;
        sg14  <= sg13;
        sg15  <= sg14;
        pos14 <= pos13;
        st14  <= st13;
        nz14  <= nz13;
    end

    // ---- 14 -> 15: round to nearest even ------------------------------------
    wire        up14 = g14 & (st14 | sig14[0]);
    wire [16:0] sr14 = {1'b0, sig14} + {16'd0, up14};

    (* shreg_extract = "no" *) reg              rc15, nz15;
    (* shreg_extract = "no" *) reg       [14:0] fr15;
    (* shreg_extract = "no" *) reg signed [11:0] e15;
    always @(posedge clk) begin
        rc15 <= sr14[16];
        fr15 <= sr14[16] ? 15'd0 : sr14[14:0];
        e15  <= $signed({6'd0, pos14}) + $signed(`F_EB(14));
        nz15 <= nz14;
    end

    // ---- 15 -> 16: exponent bounds, assemble --------------------------------
    wire signed [12:0] ef15 = $signed({e15[11], e15}) + (rc15 ? 13'sd1 : 13'sd0);

    always @(posedge clk) begin
        if (`F_FN(15)) begin
            out <= E8_NAN;
        end else if (`F_FI(15)) begin
            out <= {`F_FS(15), 8'hFF, 15'd0};
        end else if (!nz15 || `F_FZ(15)) begin
            // Exact cancellation is +0; only two zero terms keep a -0; a zero
            // segment is the entry's signed zero.
            out <= {`F_ZZ(15) & `F_ZS(15), 23'd0};
        end else if (ef15 >= 13'sd255) begin
            out <= {sg15, 8'hFF, 15'd0};
        end else if (ef15 <= 13'sd0) begin
            out <= {sg15, 23'd0};
        end else begin
            out <= {sg15, ef15[7:0], fr15};
        end
    end

    `undef F_ZZ
    `undef F_ZS
    `undef F_FN
    `undef F_FI
    `undef F_FS
    `undef F_SP
    `undef F_SC
    `undef F_SUB
    `undef F_TOP
    `undef F_FZ
    `undef F_EB

endmodule

`default_nettype wire

// FP16 -> MXFP7 block quantiser, in the MAG read path.
//
// SOFTWARE NEVER SEES MXFP7. Memory holds FP16; MXFP7 is an internal encoding
// between here and the MAC array, and it exists because it is 2.2x denser on
// the NoC -- a property thrown away if the mesh carries FP16.
//
// One invocation converts ONE L1 entry: 4 lanes x 32 K elements.
//
//   in    8 beats x 256 bit  =  4 lanes x 32 FP16      2048 bit
//   out   4 words x 256 bit  =  4 lanes x 32 MXFP7     1024 bit  + 4 x E5M3
//
// The scale is shared along K, so nothing can be emitted until the whole
// 32-element block of a lane has been seen -- which is why this buffers the
// entire entry rather than streaming.
//
// SCALE ENCODING -- E5M3, NOT E8M0.
//
//   scale = 2^(E - SBIAS) * (1 + M/8)      field = {E[4:0], M[2:0]}
//
// A power-of-two scale lands the block peak anywhere in [32,64) of the int7
// range, so up to a full bit of significand goes unused depending on where the
// peak falls in its binade. Three mantissa bits put it in [56,63] every time.
//
// E5 because the output is FP16: FP16's normal range spans 30 binades and E5
// covers 31, E4 covers 16. The field is still 8 bits, so the flit format, the
// NoC and L1 are unchanged -- only the interpretation.
//
//   value = q * scale,   anchor = 2*SBIAS, which cancels both stored biases
//
// LANE, NOT ROW. The source is always 4 lanes of 32 K: for an A operand a lane
// is a row of A, for a B operand a column of B. Only the output packing differs,
// which `b_layout` selects.
//
// Timing and area rationale: docs/mas/quantiser-timing.md.

`default_nettype none

module mx_quant #(
    // Scale exponent bias. scale = peak/63 with an FP16 peak spans
    // 2^-20 .. 2^10, so 20 centres that on the 5-bit field.
    parameter integer SBIAS = 20
)(
    input  wire         clk,
    input  wire         rst,

    input  wire         start,        // begin a new entry
    input  wire         b_layout,     // 0 = A packing, 1 = B packing

    input  wire [255:0] beat,         // 16 FP16 values, element 0 in bits [15:0]
    input  wire         beat_valid,   // 8 beats expected
    output wire         need_beat,

    output reg          done,
    output reg  [255:0] word0,
    output reg  [255:0] word1,
    output reg  [255:0] word2,
    output reg  [255:0] word3
);
    // ---- source buffer: 4 lanes x 32 FP16 ------------------------------
    // Flip-flops, stated rather than left to inference: a fill beat writes 16
    // words at once and the pack stage reads 32 through a 4:1 select, so this
    // is a register file and no RAM primitive can hold it.
    (* ram_style = "registers" *)
    reg [15:0] src [0:127];           // lane*32 + k
    reg [3:0]  bcnt;
    reg        filling;

    assign need_beat = filling;

    // Signed, and localparams rather than expressions on SBIAS: `-SBIAS[7:0]`
    // is an UNSIGNED negation, and comparing a signed reg against it makes the
    // whole comparison unsigned. It elaborates cleanly and clamps nothing.
    localparam signed [7:0] SEXP_MIN = -SBIAS;
    localparam signed [7:0] SEXP_MAX = 31 - SBIAS;

    // ---- the block peak, accumulated across the fill --------------------
    // FP16 is sign-magnitude with the exponent above the mantissa, so magnitude
    // order is the plain unsigned order of bits [14:0] -- no decode needed.
    //
    // A beat is 16 elements of ONE lane, so the reduction rides on the eight
    // cycles the fill already spends: two compare levels per beat down to four
    // partial maxima, then one cycle to fold them into the lane's accumulators.
    reg [14:0] r4 [0:3];              // four partial maxima of one beat
    reg [1:0]  r4_lane;
    reg        r4_first;              // first beat of its lane: load, not max
    reg        r4_valid;
    reg [14:0] acc [0:7];             // lane*2 + half

    // ---- per-lane block scale -------------------------------------------
    reg [10:0]       n_sig [0:3];     // peak significand renormalised to [1024,2048)
    reg signed [7:0] n_ep  [0:3];
    reg signed [7:0] sbase [0:3];     // 21 + sexp, the pack stage's shift base
    reg [12:0]       srec  [0:3];     // recip(smant)
    reg [7:0]        sfield[0:3];     // {E[4:0], M[2:0]}

    // Reciprocal of the scale mantissa: round(4096 * 8 / m8). The quantiser
    // divides by the scale and the divisor has exactly eight values, so a
    // table beats a divider by a wide margin. mxfp7.py holds the same numbers.
    function [12:0] recip;
        input [3:0] m8;
        begin
            case (m8)
                4'd8:  recip = 13'd4096;
                4'd9:  recip = 13'd3641;
                4'd10: recip = 13'd3277;
                4'd11: recip = 13'd2979;
                4'd12: recip = 13'd2731;
                4'd13: recip = 13'd2520;
                4'd14: recip = 13'd2341;
                default: recip = 13'd2185;   // m8 == 15
            endcase
        end
    endfunction

    // ---- FP16 decode, subnormals included -------------------------------
    // A subnormal has no implicit leading one and an effective exponent of 1.
    // Flushing them would zero most of any block whose peak is below ~2e-3,
    // which reads as the format being poor on small tensors.
    function [15:0] decode;      // {e[4:0], sig[10:0]}
        input [14:0] mag15;
        begin
            decode = (mag15[14:10] == 5'd0)
                   ? {5'd1, 1'b0, mag15[9:0]}
                   : {mag15[14:10], 1'b1, mag15[9:0]};
        end
    endfunction

    // ---- the pack pipeline ----------------------------------------------
    // Six stages. PK_PACK is 32 elements wide and runs four times, one output
    // word per pass, against a fetch that cannot deliver an entry faster than
    // its eight AXI beats.
    localparam [2:0] PK_IDLE  = 3'd0, PK_DRAIN = 3'd1, PK_NORM = 3'd2,
                     PK_SCALE = 3'd3, PK_PACK  = 3'd4, PK_TAIL = 3'd5;
    reg [2:0] pk;

    // Pack stage 1: the source select, the FP16 decode, the multiply by the
    // scale reciprocal, and the shift control. The product register is the
    // DSP48's own MREG, so the multiply costs no fabric and no logic level.
    (* use_dsp = "yes" *) reg [23:0] pmul [0:31];
    reg [2:0] u_r   [0:31];           // window offset, 0..7
    reg       z_r   [0:31];           // element quantises to zero
    reg       sat_r [0:31];           // element saturates the int7 range
    reg       sgn_r [0:31];
    reg [1:0] pkw, pkw_d;
    reg       pk2_valid;

    integer i, si, sj, oi, lane, j, hh;
    reg [4:0]  norm;

    // Per-stage temporaries, live within one stage only.
    reg [14:0] c0_v, c1_v, ha_v, hb_v;
    reg [4:0]  ef_v;
    reg [10:0] sig_v, tmp_v;
    reg [3:0]  ceil_v, smant_v;
    reg signed [7:0] ep_v, sexp_v;
    reg [10:0]       cs_v [0:1];
    reg signed [7:0] ce_v [0:1];
    reg              hi_v;

    reg [15:0]       h_v;
    reg [4:0]        e_v;
    reg [10:0]       s_v;
    reg signed [7:0] t_v;

    reg [7:0]   x8_v, sum_v;
    reg [5:0]   mag_v;
    reg [6:0]   q_v [0:31];
    reg [255:0] nw_v;

    // ---- control, the peak accumulator, and the scale --------------------
    always @(posedge clk) begin
        if (rst) begin
            filling <= 1'b0; pk <= PK_IDLE; done <= 1'b0; bcnt <= 4'd0;
            r4_valid <= 1'b0; pk2_valid <= 1'b0; pkw <= 2'd0; pkw_d <= 2'd0;
        end else begin
            done      <= 1'b0;
            r4_valid  <= 1'b0;
            pk2_valid <= (pk == PK_PACK) && !start;
            pkw_d     <= pkw;

            // Fold the previous beat's four maxima into its lane. Runs beside
            // the fill rather than inside it, so a beat cycle never carries
            // more than two compare levels.
            if (r4_valid) begin
                c0_v = (r4[0] > r4[1]) ? r4[0] : r4[1];
                c1_v = (r4[2] > r4[3]) ? r4[2] : r4[3];
                for (lane = 0; lane < 4; lane = lane + 1)
                    if (r4_lane == lane) begin
                        acc[lane*2+0] <= (r4_first || (c0_v > acc[lane*2+0]))
                                       ? c0_v : acc[lane*2+0];
                        acc[lane*2+1] <= (r4_first || (c1_v > acc[lane*2+1]))
                                       ? c1_v : acc[lane*2+1];
                    end
            end

            if (start) begin
                filling <= 1'b1; pk <= PK_IDLE; bcnt <= 4'd0;
            end else if (filling && beat_valid) begin
                for (i = 0; i < 16; i = i + 1)
                    src[{bcnt, 4'd0} + i] <= beat[i*16 +: 16];
                // 16 -> 4 in two compare levels. Beat b holds lane b/2, so
                // which accumulator these belong to is the beat counter.
                for (j = 0; j < 4; j = j + 1) begin
                    ha_v = (beat[(4*j+0)*16 +: 15] > beat[(4*j+1)*16 +: 15])
                         ?  beat[(4*j+0)*16 +: 15] : beat[(4*j+1)*16 +: 15];
                    hb_v = (beat[(4*j+2)*16 +: 15] > beat[(4*j+3)*16 +: 15])
                         ?  beat[(4*j+2)*16 +: 15] : beat[(4*j+3)*16 +: 15];
                    r4[j] <= (ha_v > hb_v) ? ha_v : hb_v;
                end
                r4_lane  <= bcnt[2:1];
                r4_first <= ~bcnt[0];
                r4_valid <= 1'b1;
                if (bcnt == 4'd7) begin
                    filling <= 1'b0;
                    pk <= PK_DRAIN;
                end else bcnt <= bcnt + 4'd1;
            end else if (pk == PK_DRAIN) begin
                // one cycle for the last beat's fold to land in `acc`
                pk <= PK_NORM;
            end else if (pk == PK_NORM) begin
                // Renormalise a subnormal peak into [1024,2048).
                //
                // BOTH HALVES OF THE ACCUMULATOR ARE RENORMALISED and the larger
                // one selected afterwards. Reducing first and renormalising the
                // winner puts a 15-bit compare in FRONT of an 11-step shift
                // chain; here the compare runs beside it and adds only a 2:1
                // mux. See docs/mas/quantiser-timing.md.
                for (lane = 0; lane < 4; lane = lane + 1) begin
                    for (hh = 0; hh < 2; hh = hh + 1) begin
                        {ef_v, sig_v} = decode(acc[lane*2+hh]);
                        ep_v  = $signed({3'b0, ef_v});
                        tmp_v = sig_v;
                        for (norm = 0; norm < 11; norm = norm + 1)
                            if (tmp_v != 11'd0 && tmp_v < 11'd1024) begin
                                tmp_v = tmp_v << 1;
                                ep_v  = ep_v - 8'sd1;
                            end
                        cs_v[hh] = tmp_v;
                        ce_v[hh] = ep_v;
                    end
                    hi_v = (acc[lane*2+0] > acc[lane*2+1]);
                    n_sig[lane] <= hi_v ? cs_v[0] : cs_v[1];
                    n_ep[lane]  <= hi_v ? ce_v[0] : ce_v[1];
                end
                pk <= PK_SCALE;
            end else if (pk == PK_SCALE) begin
                // The block scale: the smallest representable one with
                // peak/scale <= 63. Rounding UP is what keeps the peak from
                // clipping.
                //
                // ceil(n_sig/126) as eight constant compares, not a divide:
                // n_sig is in [1024,2047] whenever it is nonzero, so the
                // quotient is in [9,17] and its interior boundaries are known.
                for (lane = 0; lane < 4; lane = lane + 1) begin
                    tmp_v = n_sig[lane];
                    ep_v  = n_ep[lane];
                    ceil_v = (tmp_v > 11'd1764) ? 4'd15 :
                             (tmp_v > 11'd1638) ? 4'd14 :
                             (tmp_v > 11'd1512) ? 4'd13 :
                             (tmp_v > 11'd1386) ? 4'd12 :
                             (tmp_v > 11'd1260) ? 4'd11 :
                             (tmp_v > 11'd1134) ? 4'd10 : 4'd9;

                    if (tmp_v == 11'd0) begin
                        sexp_v  = 8'sd0;            // empty block, unit scale
                        smant_v = 4'd8;
                    end else if (tmp_v > 11'd1890) begin      // ceil >= 16
                        sexp_v  = ep_v - 8'sd20;
                        smant_v = (tmp_v > 11'd2016) ? 4'd9 : 4'd8;
                    end else begin
                        sexp_v  = ep_v - 8'sd21;
                        smant_v = ceil_v;
                    end

                    // A block whose peak is itself subnormal wants a scale the
                    // 5-bit field cannot hold. Clamping DEGRADES it (the peak
                    // lands below 63, so the block keeps fewer bits); letting
                    // the exponent wrap would corrupt it.
                    if      (sexp_v < SEXP_MIN) sexp_v = SEXP_MIN;
                    else if (sexp_v > SEXP_MAX) sexp_v = SEXP_MAX;

                    // {E[4:0], M[2:0]}: the exponent is stored BIASED, so the
                    // instruction's anchor of 2*SBIAS cancels both operands'
                    // biases in one subtraction downstream
                    sfield[lane] <= {(sexp_v[4:0] + SBIAS[4:0]), smant_v[2:0]};
                    sbase[lane]  <= sexp_v + 8'sd21;
                    srec[lane]   <= recip(smant_v);
                end
                pkw <= 2'd0;
                pk  <= PK_PACK;
            end else if (pk == PK_PACK) begin
                if (pkw == 2'd3) pk <= PK_TAIL;
                else pkw <= pkw + 2'd1;
            end else if (pk == PK_TAIL) begin
                pk   <= PK_IDLE;
                done <= 1'b1;
            end
        end
    end

    // ---- pack stage 1: select, decode, multiply, shift control -----------
    // Slot s carries lane s/8 and K-slice element s%8 of the word `pkw`
    // selects, so the source select is 4:1 and the scale reciprocal is fixed
    // per lane for the whole entry.
    //
    //   x = sig * 2^(e-25) and scale = (m8/8) * 2^sexp, so
    //     q = round(sig * recip(m8) * 2^(e - 25 - sexp - 12))
    //
    //   t = 21 + sexp - e   is that shift measured from bit 15 of the product.
    always @(posedge clk) begin
        for (si = 0; si < 32; si = si + 1) begin
            h_v = src[(si/8)*32 + (si%8) + pkw*8];
            {e_v, s_v} = decode(h_v[14:0]);
            t_v = sbase[si/8] - $signed({3'b000, e_v});
            pmul[si]  <= s_v * srec[si/8];
            u_r[si]   <= t_v[2:0];
            z_r[si]   <= (s_v == 11'd0) || (!t_v[7] && (|t_v[6:3]));
            sat_r[si] <= t_v[7];
            sgn_r[si] <= h_v[15];
        end
    end

    // ---- pack stage 2: window, round, clamp, sign, place -----------------
    // AN 8-BIT WINDOW, NOT A 24-BIT BARREL SHIFT. The product is under 2^23 and
    // the scale comes from the block peak, so t is in [0,7] for every element
    // that produces a nonzero result: above that the window clears the product
    // and gives zero, below it the element exceeds the peak and saturates.
    always @(posedge clk) begin
        if (rst) begin
            word0 <= 256'd0; word1 <= 256'd0; word2 <= 256'd0; word3 <= 256'd0;
        end else if (pk2_valid) begin
            for (sj = 0; sj < 32; sj = sj + 1) begin
                x8_v  = pmul[sj][22:15] >> u_r[sj];
                sum_v = {1'b0, x8_v[7:1]} + {7'd0, x8_v[0]};   // round to nearest
                mag_v = (sat_r[sj] || (|sum_v[7:6])) ? 6'd63   // clamp, never wrap
                                                     : sum_v[5:0];
                q_v[sj] = z_r[sj] ? 7'd0
                        : (sgn_r[sj] ? (~{1'b0, mag_v} + 7'd1) : {1'b0, mag_v});
            end

            // slot -> bit position, matching the CU's operand word layout
            // A: element (lane,k) at slot lane*8 + k of word (k/8)
            // B: element (k,lane) at slot (k%8)*4 + lane
            nw_v = 256'd0;
            for (oi = 0; oi < 32; oi = oi + 1)
                nw_v[255 - oi*7 -: 7] = b_layout ? q_v[(oi%4)*8 + (oi/4)]
                                                 : q_v[oi];
            // the scale fields ride in every word, at bit 31 - lane*8
            nw_v[31 -: 8] = sfield[0];
            nw_v[23 -: 8] = sfield[1];
            nw_v[15 -: 8] = sfield[2];
            nw_v[7  -: 8] = sfield[3];

            case (pkw_d)
                2'd0:    word0 <= nw_v;
                2'd1:    word1 <= nw_v;
                2'd2:    word2 <= nw_v;
                default: word3 <= nw_v;
            endcase
        end
    end

endmodule

`default_nettype wire

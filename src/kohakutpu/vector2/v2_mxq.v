// VPACK MX7: one register (8 chunks, 128 elements) -> one MXFP7 L1 entry,
// four words, byte-identical to mx_quant run on the eight FP16 words a VPACK
// F16 of the same register writes (beat b = chunk b).
//
//   entry lane r = chunks 2r, 2r+1;  k = (chunk & 1) * 16 + vector lane
//
// TWO PASSES OVER THE REGISTER, NO ENTRY BUFFER. The pack engine reads the
// eight chunks twice. Pass 0 folds each lane's block peak (FP16 magnitude
// order is the unsigned order of bits [14:0]); the scale of lane r is ready
// two chunks after its second chunk. Pass 1 quantises 16 elements a cycle
// into the output words as their scales come ready. Lane r's pass-1 data
// arrives eight chunk slots after its pass-0 data, two cycles after its
// scale: a stall anywhere only adds slack.
//
// The arithmetic is mx_quant's, stage for stage: decode with subnormals,
// peak renormalised to [1024,2048), E5M3 scale rounded up so the peak never
// clips, reciprocal table, 8-bit window, round to nearest, clamp, sign.
//
// Output: after pass 1's last chunk, four words on four consecutive cycles,
// word w at base + w * s0, the last flagged. The scale fields ride in every
// word at bit 31 - lane*8; A packing puts element (lane, k) at slot
// lane*8 + k%8 of word k/8, B packing at slot (k%8)*4 + lane.

`default_nettype none

module v2_mxq #(
    parameter integer LAW   = 9,
    parameter integer SBIAS = 20
)(
    input  wire           clk,
    input  wire           rst,

    // One converted chunk a cycle, as the pack engine's register stage.
    input  wire           in_v,
    input  wire           in_pass,      // 0 peak, 1 quantise
    input  wire [2:0]     in_chunk,
    input  wire [255:0]   in_f16,       // 16 FP16, vector lane i in [16i +: 16]
    input  wire           in_bl,        // B packing
    input  wire [LAW-1:0] in_base,
    input  wire [LAW-1:0] in_s0,
    input  wire           in_eq,        // pack queue the entry came from

    output reg            out_v,
    output reg  [255:0]   out_word,
    output reg  [LAW-1:0] out_addr,
    output reg            out_eq,
    output reg            out_last,
    // Something of an entry is inside: the engine starts no other mode.
    output wire           busy
);
    localparam signed [7:0] SEXP_MIN = -SBIAS;
    localparam signed [7:0] SEXP_MAX = 31 - SBIAS;

    function [15:0] decode;      // {e[4:0], sig[10:0]}
        input [14:0] mag15;
        begin
            decode = (mag15[14:10] == 5'd0)
                   ? {5'd1, 1'b0, mag15[9:0]}
                   : {mag15[14:10], 1'b1, mag15[9:0]};
        end
    endfunction

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
                default: recip = 13'd2185;
            endcase
        end
    endfunction

    integer i;

    // ================================================== pass 0: lane peaks
    // A: 16 -> 4 maxima, two compare levels. B: 4 -> 1 and the fold with the
    // lane's other chunk.
    reg  [14:0] a4 [0:3];
    reg         a_v;
    reg  [2:0]  a_ch;
    reg  [14:0] h_v, h_w;
    always @(posedge clk) begin
        a_v  <= in_v && !in_pass && !rst;
        a_ch <= in_chunk;
        for (i = 0; i < 4; i = i + 1) begin
            h_v = (in_f16[(4*i+0)*16 +: 15] > in_f16[(4*i+1)*16 +: 15])
                ?  in_f16[(4*i+0)*16 +: 15] : in_f16[(4*i+1)*16 +: 15];
            h_w = (in_f16[(4*i+2)*16 +: 15] > in_f16[(4*i+3)*16 +: 15])
                ?  in_f16[(4*i+2)*16 +: 15] : in_f16[(4*i+3)*16 +: 15];
            a4[i] <= (h_v > h_w) ? h_v : h_w;
        end
    end

    // B: 4 -> 2. C: 2 -> 1 and the fold, two compare levels each (three in
    // one cycle measured -0.061 ns at 3.0).
    reg  [14:0] m_a, m_b;
    reg         b_v;
    reg  [2:0]  b_ch;
    always @(posedge clk) begin
        m_a  <= (a4[0] > a4[1]) ? a4[0] : a4[1];
        m_b  <= (a4[2] > a4[3]) ? a4[2] : a4[3];
        b_v  <= a_v && !rst;
        b_ch <= a_ch;
    end

    reg  [14:0] pk_lo;                // the lane's even chunk
    reg  [14:0] pk;                   // the lane's peak, when pk_v
    reg         pk_v;
    reg  [1:0]  pk_r;
    reg  [14:0] m_c;
    always @(posedge clk) begin
        m_c = (m_a > m_b) ? m_a : m_b;
        pk_v <= b_v && b_ch[0] && !rst;
        pk_r <= b_ch[2:1];
        if (b_v && !b_ch[0]) begin
            pk_lo <= m_c;
        end
        pk <= (m_c > pk_lo) ? m_c : pk_lo;
    end

    // ================================================== the lane's scale
    // NORM: renormalise the peak into [1024,2048). SCALE: the smallest
    // representable scale with peak / scale <= 63.
    reg  [10:0]       n_sig;
    reg  signed [7:0] n_ep;
    reg               n_v;
    reg  [1:0]        n_r;
    reg  [4:0]        ef_v;
    reg  [10:0]       sig_v, tmp_v;
    reg  signed [7:0] ep_v;
    integer nrm;
    always @(posedge clk) begin
        {ef_v, sig_v} = decode(pk);
        ep_v  = $signed({3'b0, ef_v});
        tmp_v = sig_v;
        for (nrm = 0; nrm < 11; nrm = nrm + 1) begin
            if (tmp_v != 11'd0 && tmp_v < 11'd1024) begin
                tmp_v = tmp_v << 1;
                ep_v  = ep_v - 8'sd1;
            end
        end
        n_sig <= tmp_v;
        n_ep  <= ep_v;
        n_v   <= pk_v && !rst;
        n_r   <= pk_r;
    end

    reg  signed [7:0] sbase [0:3];    // 21 + sexp, the shift base
    reg  [12:0]       srec  [0:3];
    reg  [7:0]        sfield[0:3];    // {E[4:0], M[2:0]}
    reg  [3:0]        ceil_v, smant_v;
    reg  signed [7:0] sexp_v;
    always @(posedge clk) begin
        ceil_v = (n_sig > 11'd1764) ? 4'd15
               : (n_sig > 11'd1638) ? 4'd14
               : (n_sig > 11'd1512) ? 4'd13
               : (n_sig > 11'd1386) ? 4'd12
               : (n_sig > 11'd1260) ? 4'd11
               : (n_sig > 11'd1134) ? 4'd10
               : 4'd9;
        if (n_sig == 11'd0) begin
            sexp_v  = 8'sd0;
            smant_v = 4'd8;
        end else if (n_sig > 11'd1890) begin
            sexp_v  = n_ep - 8'sd20;
            smant_v = (n_sig > 11'd2016) ? 4'd9 : 4'd8;
        end else begin
            sexp_v  = n_ep - 8'sd21;
            smant_v = ceil_v;
        end
        if (sexp_v < SEXP_MIN) begin
            sexp_v = SEXP_MIN;
        end else if (sexp_v > SEXP_MAX) begin
            sexp_v = SEXP_MAX;
        end
        if (n_v) begin
            sfield[n_r] <= {(sexp_v[4:0] + SBIAS[4:0]), smant_v[2:0]};
            sbase[n_r]  <= sexp_v + 8'sd21;
            srec[n_r]   <= recip(smant_v);
        end
    end

    // ================================================== pass 1: quantise
    // Q1: decode, shift control, the multiply (its DSP's MREG).
    (* use_dsp = "yes" *) reg [23:0] pmul [0:15];
    reg  [2:0]  u_r   [0:15];
    reg         z_r   [0:15];
    reg         sat_r [0:15];
    reg         sgn_r [0:15];
    reg         q1_v;
    reg  [2:0]  q1_ch;
    reg  [4:0]        e_v;
    reg  [10:0]       s_v;
    reg  signed [7:0] t_v;
    wire [1:0] in_r = in_chunk[2:1];
    always @(posedge clk) begin
        q1_v  <= in_v && in_pass && !rst;
        q1_ch <= in_chunk;
        for (i = 0; i < 16; i = i + 1) begin
            {e_v, s_v} = decode(in_f16[i*16 +: 15]);
            t_v = sbase[in_r] - $signed({3'b000, e_v});
            pmul[i]  <= s_v * srec[in_r];
            u_r[i]   <= t_v[2:0];
            z_r[i]   <= (s_v == 11'd0) || (!t_v[7] && (|t_v[6:3]));
            sat_r[i] <= t_v[7];
            sgn_r[i] <= in_f16[i*16 + 15];
        end
    end

    // Q2: window, round, clamp, sign, into the output words. ob[w][lane][k%8].
    reg  [6:0] ob [0:3][0:3][0:7];
    reg        q2_last;
    reg  [7:0] x8_v, sum_v;
    reg  [5:0] mag_v;
    reg  [6:0] q_v;
    always @(posedge clk) begin
        q2_last <= q1_v && (q1_ch == 3'd7) && !rst;
        if (q1_v) begin
            for (i = 0; i < 16; i = i + 1) begin
                x8_v  = pmul[i][22:15] >> u_r[i];
                sum_v = {1'b0, x8_v[7:1]} + {7'd0, x8_v[0]};
                mag_v = (sat_r[i] || (|sum_v[7:6])) ? 6'd63 : sum_v[5:0];
                q_v   = z_r[i] ? 7'd0
                      : (sgn_r[i] ? (~{1'b0, mag_v} + 7'd1) : {1'b0, mag_v});
                ob[{q1_ch[0], i[3]}][q1_ch[2:1]][i[2:0]] <= q_v;
            end
        end
    end

    // ================================================== the entry's context
    // Taken with pass 1's first chunk, so the next entry's pass 0 cannot
    // disturb the words still to leave.
    reg           c_bl, c_eq;
    reg [LAW-1:0] c_base, c_s0;
    always @(posedge clk) begin
        if (in_v && in_pass && (in_chunk == 3'd0)) begin
            c_bl <= in_bl; c_eq <= in_eq; c_base <= in_base; c_s0 <= in_s0;
        end
    end

    // ================================================== emit
    reg  [1:0]     e_w;
    reg            e_act;
    // The next entry's lane 0 scale lands while word 3 leaves: this one's, held.
    reg  [31:0]    e_sf;
    reg  [255:0]   nw_v;
    reg  [6:0]     slot_q;
    integer oi;
    always @(*) begin
        nw_v = 256'd0;
        for (oi = 0; oi < 32; oi = oi + 1) begin
            // A: slot lane*8 + kk;  B: slot kk*4 + lane
            slot_q = c_bl ? ob[e_w][oi % 4][oi / 4] : ob[e_w][oi / 8][oi % 8];
            nw_v[255 - oi*7 -: 7] = slot_q;
        end
        nw_v[31 -: 32] = e_sf;
    end

    always @(posedge clk) begin
        if (q2_last) e_sf <= {sfield[0], sfield[1], sfield[2], sfield[3]};
    end

    always @(posedge clk) begin
        if (rst) begin
            e_act <= 1'b0; e_w <= 2'd0; out_v <= 1'b0;
        end else begin
            out_v <= e_act;
            if (q2_last) begin
                e_act <= 1'b1; e_w <= 2'd0;
            end else if (e_act) begin
                e_w <= e_w + 2'd1;
                if (e_w == 2'd3) e_act <= 1'b0;
            end
        end
        out_word <= nw_v;
        out_addr <= c_base + c_s0 * e_w;
        out_eq   <= c_eq;
        out_last <= (e_w == 2'd3);
    end

    assign busy = in_v || a_v || b_v || pk_v || n_v || q1_v || q2_last || e_act || out_v;

endmodule

`default_nettype wire

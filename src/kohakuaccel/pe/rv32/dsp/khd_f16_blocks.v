// khd_f16_blocks -- the E8M15 FMA lane taken apart, block by block, so the
// float tier's LUT can be attributed rather than estimated.
//
// MEASUREMENT PROBES, NOT A DATAPATH. Nothing instantiates these. `vec_alu` is
// the vector core's module and is NOT edited, so these are transcriptions of
// its FMA path kept line for line, and every block is registered in and out --
// an out-of-context run with combinational pins measures the pins.

`default_nettype none

// ---------------------------------------------------------------- aligner
// vec_alu cycle 5: {sig_c, 32'b0} >> s, one direction, 48 bits, plus the
// sticky for the bits that leave the window.
module khd_blk_align (
    input  wire        clk,
    input  wire [15:0] gc,
    input  wire [6:0]  s,
    output reg  [47:0] y,
    output reg         stk
);
    reg [15:0] gc_q;
    reg [6:0]  s_q;
    always @(posedge clk) begin gc_q <= gc; s_q <= s; end

    wire [47:0] algn_in  = {gc_q, 32'b0};
    wire [47:0] algn_out = algn_in >> s_q;
    wire [15:0] stk_mask = ~(16'hFFFF << (s_q - 7'd32));
    wire        algn_stk = (s_q >= 7'd33) && |(gc_q & stk_mask);

    always @(posedge clk) begin y <= algn_out; stk <= algn_stk; end
endmodule


// A VARIABLE SHIFT IS A MULTIPLY BY A ONE-HOT POWER OF TWO, and the DSP's B
// port is 18 bits, so the shift splits: the low four bits become the multiply
// and the top two become a placement mux over the 48-bit field.
//
//   gc << (32 - s),  s = 16q + r
//     = (gc << (16 - r)) << (16 - 16q)      w = gc * 2^(16-r), 32 bits
//   q=0 {w,16'b0}   q=1 {16'b0,w}   q=2 {32'b0,w[31:16]}   q=3 zero
//
// The bits that leave the window are exactly the ones the placement drops, so
// the sticky is an OR of w rather than a masked compare on the shift amount.
module khd_blk_align_dsp #(
    parameter integer MODEL = 0
)(
    input  wire        clk,
    input  wire        rst,
    input  wire [15:0] gc,
    input  wire [6:0]  s,
    output reg  [47:0] y,
    output reg         stk
);
    reg [15:0] gc_q;
    reg [6:0]  s_q;
    always @(posedge clk) begin gc_q <= gc; s_q <= s; end

    wire [1:0]  q = s_q[5:4];
    wire [3:0]  r = s_q[3:0];
    wire [17:0] onehot = 18'd1 << (5'd16 - {1'b0, r});

    wire signed [47:0] p;
    vec_dsp #(.PREADD(0), .MODEL(MODEL)) u_d (
        .clk(clk), .rst(rst), .en(1'b1),
        .a({14'b0, gc_q}), .b(onehot), .c(48'd0), .d(27'd0),
        .alumode(4'b0000), .p(p));

    // The DSP is A/B at N-3 for P at N, so the placement select travels with it.
    wire [1:0] q3;
    vec_delay #(.W(2), .D(3)) u_dq (.clk(clk), .d(q), .q(q3));

    wire [31:0] w = p[31:0];
    reg  [47:0] y_c;
    always @(*) begin
        case (q3)
            2'd0:    y_c = {w, 16'b0};
            2'd1:    y_c = {16'b0, w};
            2'd2:    y_c = {32'b0, w[31:16]};
            default: y_c = 48'd0;
        endcase
    end
    wire stk_c = (q3 == 2'd2) ? |w[15:0] : (q3 == 2'd3) ? |w : 1'b0;

    always @(posedge clk) begin y <= y_c; stk <= stk_c; end
endmodule


// ------------------------------------------------------------- normaliser
// vec_alu cycles 11-12: the leading-one search, the normalising left shift,
// and the significand/guard/sticky split that follows it.
module khd_blk_norm (
    input  wire        clk,
    input  wire [47:0] mag,
    input  wire        stk_in,
    output reg  [15:0] sig,
    output reg         g,
    output reg         s,
    output reg  [5:0]  pos,
    output reg         nz
);
    reg [47:0] mag_q;
    reg        stk_q;
    always @(posedge clk) begin mag_q <= mag; stk_q <= stk_in; end

    wire [5:0] lz_pos;
    wire       lz_nz;
    mx_lead1 #(.W(48)) u_l1 (.x(mag_q), .pos(lz_pos), .nz(lz_nz));

    reg [47:0] m1;
    reg [5:0]  p1;
    reg        n1, s1;
    always @(posedge clk) begin
        m1 <= mag_q; p1 <= lz_pos; n1 <= lz_nz; s1 <= stk_q;
    end

    wire [47:0] nrm = m1 << (6'd47 - p1);
    always @(posedge clk) begin
        sig <= nrm[47:32];
        g   <= nrm[31];
        s   <= (|nrm[30:0]) | s1;
        pos <= p1;
        nz  <= n1;
    end
endmodule


// The same extraction with the fine shift on a DSP. Only seventeen bits of the
// normalised value are ever used, so this extracts a window rather than
// shifting 48 bits: coarse by pos[5:3] into a 27-bit slice -- the DSP's A port
// -- and fine by pos[2:0] as a multiply.
//
//   Z = mag[8q+7 : 8q-19]  (27 bits)        P = Z * 2^(7-r)
//   the window is P[26:10], for every (q, r), because Z spans from 16 below
//   the lowest needed bit to the highest.
module khd_blk_norm_dsp #(
    parameter integer MODEL = 0
)(
    input  wire        clk,
    input  wire        rst,
    input  wire [47:0] mag,
    input  wire        stk_in,
    output reg  [15:0] sig,
    output reg         g,
    output reg         s,
    output reg  [5:0]  pos,
    output reg         nz
);
    reg [47:0] mag_q;
    reg        stk_q;
    always @(posedge clk) begin mag_q <= mag; stk_q <= stk_in; end

    wire [5:0] lz_pos;
    wire       lz_nz;
    mx_lead1 #(.W(48)) u_l1 (.x(mag_q), .pos(lz_pos), .nz(lz_nz));

    reg [47:0] m1;
    reg [5:0]  p1;
    reg        n1, s1;
    always @(posedge clk) begin
        m1 <= mag_q; p1 <= lz_pos; n1 <= lz_nz; s1 <= stk_q;
    end

    wire [2:0] qc = p1[5:3];
    wire [2:0] rc = p1[2:0];

    // 19 zeros below bit 0 so every coarse slice is a plain bit select.
    wire [66:0] ext = {mag_q[47:0], 19'b0};       // ext[j] = mag[j-19]
    wire [26:0] z   = ext[{qc, 3'b0} + 7'd27 -: 27];
    wire [17:0] onehot = 18'd1 << (4'd7 - {1'b0, rc});

    wire signed [47:0] p;
    vec_dsp #(.PREADD(0), .MODEL(MODEL)) u_d (
        .clk(clk), .rst(rst), .en(1'b1),
        .a({3'b0, z}), .b(onehot), .c(48'd0), .d(27'd0),
        .alumode(4'b0000), .p(p));

    wire [5:0] p4;
    wire       n4, s4;
    vec_delay #(.W(6), .D(3)) u_dp (.clk(clk), .d(p1), .q(p4));
    vec_delay #(.W(1), .D(3)) u_dn (.clk(clk), .d(n1), .q(n4));
    vec_delay #(.W(1), .D(3)) u_ds (.clk(clk), .d(s1), .q(s4));

    // Everything below the guard bit, in one OR: the DSP result carries the
    // bits it kept, and the coarse slice dropped only bits below those.
    wire [9:0] lost = p[9:0];

    always @(posedge clk) begin
        sig <= p[26:11];
        g   <= p[10];
        s   <= (|lost) | s4;
        pos <= p4;
        nz  <= n4;
    end
endmodule


// ------------------------------------------------------- the other blocks
// vec_alu cycle 13: round to nearest even, the exponent bounds, assemble.
module khd_blk_round (
    input  wire               clk,
    input  wire [15:0]        sig,
    input  wire               gbit,
    input  wire               sbit,
    input  wire               sign,
    input  wire               nz,
    input  wire [5:0]         posn,
    input  wire signed [11:0] eb,
    input  wire               nan,
    input  wire               inf,
    input  wire               zero,
    input  wire               ssign,
    input  wire               canc,
    output reg  [23:0]        out
);
    localparam [23:0] E8_NAN = 24'h7FC000;

    reg [15:0] s12_sig;
    reg        s12_g, s12_s, s12_sign, s12_nz;
    reg [5:0]  s12_pos;
    reg signed [11:0] s12_eb;
    reg d12_nan, d12_inf, d12_zero, d12_ssign, d12_canc;
    always @(posedge clk) begin
        s12_sig <= sig; s12_g <= gbit; s12_s <= sbit; s12_sign <= sign;
        s12_nz <= nz; s12_pos <= posn; s12_eb <= eb;
        d12_nan <= nan; d12_inf <= inf; d12_zero <= zero;
        d12_ssign <= ssign; d12_canc <= canc;
    end

    wire        rnd_up = s12_g & (s12_s | s12_sig[0]);
    wire [16:0] sig_r  = {1'b0, s12_sig} + {16'b0, rnd_up};
    wire        rcarry = sig_r[16];
    wire [14:0] frac   = rcarry ? 15'd0 : sig_r[14:0];
    wire signed [11:0] e_fin = $signed({6'b0, s12_pos}) + s12_eb
                             + (rcarry ? 12'sd1 : 12'sd0);

    always @(posedge clk) begin
        if (d12_nan)                 out <= E8_NAN;
        else if (d12_inf)            out <= {d12_ssign, 8'hFF, 15'd0};
        else if (d12_zero)           out <= {d12_ssign, 23'd0};
        else if (~s12_nz)            out <= {s12_sign & ~d12_canc, 23'd0};
        else if (e_fin >= 12'sd255)  out <= {s12_sign, 8'hFF, 15'd0};
        else if (e_fin <= 12'sd0)    out <= {s12_sign, 23'd0};
        else                         out <= {s12_sign, e_fin[7:0], frac};
    end
endmodule


// vec_alu cycle 10: recover the magnitude and the sign out of DSP-M.
module khd_blk_mag (
    input  wire        clk,
    input  wire signed [47:0] dspm_p,
    input  wire        neg,
    input  wire        snz,
    input  wire        sab,
    input  wire        scc,
    output reg  [47:0] mag,
    output reg         sign
);
    reg signed [47:0] p_q;
    reg        neg_q, snz_q, sab_q, scc_q;
    always @(posedge clk) begin
        p_q <= dspm_p; neg_q <= neg; snz_q <= snz;
        sab_q <= sab; scc_q <= scc;
    end

    wire        res_neg = neg_q & snz_q & p_q[47];
    wire [47:0] fma_mag = res_neg ? {15'b0, (~p_q[32:0] + 33'd1)} : p_q;

    always @(posedge clk) begin
        mag  <= fma_mag;
        sign <= neg_q ? (res_neg ? sab_q : scc_q) : sab_q;
    end
endmodule


// vec_alu cycle 4: the shift amount and the exponent base, out of DSP-E.
module khd_blk_expo (
    input  wire        clk,
    input  wire [23:0] dspe_p,
    input  wire [7:0]  ec,
    input  wire        cz,
    output reg  [6:0]  s_amt,
    output reg signed [11:0] ebase
);
    reg [23:0] p_q;
    reg [7:0]  ec_q;
    reg        cz_q;
    always @(posedge clk) begin p_q <= dspe_p; ec_q <= ec; cz_q <= cz; end

    wire signed [12:0] s_raw = $signed({1'b0, p_q[11:0]}) - 13'sd495;
    wire        byp   = s_raw[12] & ~cz_q;
    wire [6:0]  s_c   = cz_q             ? 7'd48
                      : byp              ? 7'd0
                      : (s_raw > 13'sd48) ? 7'd48 : s_raw[6:0];
    wire signed [11:0] eb_fma = $signed({1'b0, p_q[23:12]}) - 12'sd286;
    wire signed [11:0] eb_byp = $signed({4'b0, ec_q}) - 12'sd47;

    always @(posedge clk) begin
        s_amt <= s_c;
        ebase <= byp ? eb_byp : eb_fma;
    end
endmodule


// vec_alu cycle 1: the specials for the FMA family.
module khd_blk_spec (
    input  wire        clk,
    input  wire [23:0] va,
    input  wire [23:0] vb,
    input  wire [23:0] vc,
    input  wire        az,
    input  wire        bz,
    output reg         nan,
    output reg         inf,
    output reg         sgn,
    output reg         canc
);
    reg [23:0] a_q, b_q, c_q;
    reg        az_q, bz_q;
    always @(posedge clk) begin
        a_q <= va; b_q <= vb; c_q <= vc; az_q <= az; bz_q <= bz;
    end

    wire [7:0] va_e = a_q[22:15], vb_e = b_q[22:15], vc_e = c_q[22:15];
    wire va_n = (va_e == 8'hFF) &  (|a_q[14:0]);
    wire vb_n = (vb_e == 8'hFF) &  (|b_q[14:0]);
    wire vc_n = (vc_e == 8'hFF) &  (|c_q[14:0]);
    wire va_i = (va_e == 8'hFF) & ~(|a_q[14:0]);
    wire vb_i = (vb_e == 8'hFF) & ~(|b_q[14:0]);
    wire vc_i = (vc_e == 8'hFF) & ~(|c_q[14:0]);
    wire vc_z = (vc_e == 8'd0);

    wire sign_ab = a_q[23] ^ b_q[23];
    wire fma_neg = sign_ab ^ c_q[23];
    wire pz      = az_q | bz_q;

    wire p_nan = va_n | vb_n | (va_i & bz_q) | (az_q & vb_i);
    wire p_inf = (va_i | vb_i) & ~p_nan;
    wire f_nan = p_nan | vc_n | (p_inf & vc_i & (sign_ab != c_q[23]));
    wire f_inf = (p_inf | vc_i) & ~f_nan;

    always @(posedge clk) begin
        nan  <= f_nan;
        inf  <= f_inf;
        sgn  <= p_inf ? sign_ab : c_q[23];
        canc <= fma_neg & ~pz & ~vc_z;
    end
endmodule


// ------------------------------------------------------- the conversions
module khd_blk_cvt_in (
    input  wire        clk,
    input  wire [15:0] f16,
    output reg  [23:0] e8
);
    reg [15:0] q;
    wire [23:0] c;
    always @(posedge clk) q <= f16;
    vec_cvt_f16_to_e8 u_c (.f16(q), .e8(c));
    always @(posedge clk) e8 <= c;
endmodule


module khd_blk_cvt_out (
    input  wire        clk,
    input  wire [23:0] e8,
    output reg  [15:0] f16
);
    reg [23:0] q;
    wire [15:0] c;
    always @(posedge clk) q <= e8;
    vec_cvt_e8_to_f16 u_c (.e8(q), .f16(c));
    always @(posedge clk) f16 <= c;
endmodule

`default_nettype wire

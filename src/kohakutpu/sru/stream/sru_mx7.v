// FP16 sub-tile words -> MXFP7 L1 entries, one word a cycle, bit for bit
// mx_quant.v's quantiser (kohakutpu.language.numerics.mxfp7 is the spec).
//
// An entry is 8 consecutive words of a sub-row: word w holds rows 0..3,
// columns 4w..4w+3 (lane 4r + c), so the entry is 4 rows x 32 K. It leaves as
// 4 words; out word w holds K 8w..8w+7 of every row in mx_quant's slot order
// (A: slot r*8 + kk, B: slot kk*4 + r, slot s at bits [255 - 7s -: 7]) and the
// four E5M3 fields at [31:0], row r at [31 - 8r -: 8].
//
// THREE BANKS, rotating, as 24 words of one distributed RAM (word {bank,
// position}). A bank's drain starts once the entry's scales are derived, 4
// cycles after its fill, and runs 8: a bank is busy ~12 cycles past its fill
// while the next fills in 8, so two banks collide at full rate and three never
// do. The caller keeps a sub-row a multiple of 8 words.
//
// MUL_DSP = 0 multiplies in LUT (0 DSP); 1 puts the 16 multiplies in DSPs.

`default_nettype none

(* shreg_extract = "no" *)
module sru_mx7 #(
    parameter integer MUL_DSP = 0
)(
    input  wire         clk,
    input  wire         rst,

    input  wire         b_layout,
    input  wire         in_valid,
    input  wire         in_first,     // first word of a sub-row
    input  wire [255:0] in_data,

    output reg          out_valid,
    output reg  [255:0] out_data
);
    integer i, r, s;

    // ---- 0: register the input ---------------------------------------------
    reg         v0, f0;
    reg [255:0] d0;
    always @(posedge clk) begin
        if (rst) begin
            v0 <= 1'b0;
        end else begin
            v0 <= in_valid;
        end
        f0 <= in_first;
        d0 <= in_data;
    end

    // ---- fill: write into the filling bank, and the word's row peaks ------
    reg [1:0]   fsel;
    reg [2:0]   fcnt;
    wire        fill = v0;
    wire [2:0]  fpos = f0 ? 3'd0 : fcnt;

    // max |h| (bits [14:0]) of each row's 4 elements
    reg [14:0] wpk [0:3];
    reg [14:0] pa, pb;
    always @(*) begin
        for (r = 0; r < 4; r = r + 1) begin
            pa = (d0[(4*r+0)*16 +: 15] > d0[(4*r+1)*16 +: 15]) ? d0[(4*r+0)*16 +: 15] : d0[(4*r+1)*16 +: 15];
            pb = (d0[(4*r+2)*16 +: 15] > d0[(4*r+3)*16 +: 15]) ? d0[(4*r+2)*16 +: 15] : d0[(4*r+3)*16 +: 15];
            wpk[r] = (pa > pb) ? pa : pb;
        end
    end

    reg [14:0] wp1 [0:3];
    reg        v1, st1, end1;
    reg [1:0]  b1;
    reg [1:0]  dsel;
    reg [2:0]  ready;
    wire       drain = ready[dsel];
    reg  [2:0] dcnt;
    wire [255:0] top;
    kohaku_sdpram #(.WIDTH(256), .DEPTH(32), .MEM_PRIM("distributed"), .READ_LAT(0)) u_banks (
        .clk(clk), .wr_en(fill), .wr_addr({fsel, fpos}), .wr_data(d0),
        .rd_en(1'b1), .rd_addr({dsel, dcnt}), .rd_data(top)
    );
    always @(posedge clk) begin
        for (r = 0; r < 4; r = r + 1) wp1[r] <= wpk[r];
        st1  <= (fpos == 3'd0);
        end1 <= (fpos == 3'd7);
        b1   <= fsel;
    end

    // running peak per row, per bank; final the cycle after the 8th word
    reg [14:0] pk [0:2][0:3];
    reg        pkdone;
    reg [1:0]  pkbank;
    always @(posedge clk) begin
        if (rst) begin
            v1 <= 1'b0;
            pkdone <= 1'b0;
        end else begin
            v1 <= fill;
            pkdone <= v1 & end1;
        end
        pkbank <= b1;
        if (v1) begin
            for (r = 0; r < 4; r = r + 1) begin
                if (st1 || (wp1[r] > pk[b1][r])) begin
                    pk[b1][r] <= wp1[r];
                end
            end
        end
    end

    // ---- scale per row (mx_quant PK_NORM + PK_SCALE) ----------------------
    // Renormalise the peak into [1024, 2048), then the smallest E5M3 scale
    // with peak / scale <= 63.
    reg [10:0]       nsig [0:3];
    reg signed [7:0] nep  [0:3];
    reg              sc_go;
    reg [1:0]        sc_bank;
    reg [10:0]       tsig;
    reg signed [7:0] tep;
    integer          nn;
    always @(posedge clk) begin
        if (rst) begin
            sc_go <= 1'b0;
        end else begin
            sc_go <= pkdone;
        end
        sc_bank <= pkbank;
        for (r = 0; r < 4; r = r + 1) begin
            tsig = (pk[pkbank][r][14:10] == 5'd0) ? {1'b0, pk[pkbank][r][9:0]}
                                                 : {1'b1, pk[pkbank][r][9:0]};
            tep  = (pk[pkbank][r][14:10] == 5'd0) ? 8'sd1 : $signed({3'b000, pk[pkbank][r][14:10]});
            for (nn = 0; nn < 11; nn = nn + 1) begin
                if (tsig != 11'd0 && tsig < 11'd1024) begin
                    tsig = tsig << 1;
                    tep  = tep - 8'sd1;
                end
            end
            nsig[r] <= tsig;
            nep[r]  <= tep;
        end
    end

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

    reg signed [7:0] sbase [0:2][0:3];   // 21 + sexp
    reg [12:0]       srec  [0:2][0:3];
    reg [7:0]        sfld  [0:2][0:3];
    reg [3:0]        ceil_v, smant;
    reg signed [7:0] sexp;
    always @(posedge clk) begin
        if (sc_go) begin
            for (r = 0; r < 4; r = r + 1) begin
                ceil_v = (nsig[r] > 11'd1764) ? 4'd15 : (nsig[r] > 11'd1638) ? 4'd14
                       : (nsig[r] > 11'd1512) ? 4'd13 : (nsig[r] > 11'd1386) ? 4'd12
                       : (nsig[r] > 11'd1260) ? 4'd11 : (nsig[r] > 11'd1134) ? 4'd10 : 4'd9;
                if (nsig[r] == 11'd0) begin
                    sexp = 8'sd0;           smant = 4'd8;
                end else if (nsig[r] > 11'd1890) begin
                    sexp = nep[r] - 8'sd20; smant = (nsig[r] > 11'd2016) ? 4'd9 : 4'd8;
                end else begin
                    sexp = nep[r] - 8'sd21; smant = ceil_v;
                end
                if (sexp < -8'sd20) begin
                    sexp = -8'sd20;
                end else if (sexp > 8'sd11) begin
                    sexp = 8'sd11;
                end
                sbase[sc_bank][r] <= sexp + 8'sd21;
                srec[sc_bank][r]  <= recip(smant);
                sfld[sc_bank][r]  <= {sexp[4:0] + 5'd20, smant[2:0]};
            end
        end
    end

    // ---- drain: a word a cycle out of the full bank, in fill order ---------

    always @(posedge clk) begin
        if (rst) begin
            fsel  <= 2'd0;
            fcnt  <= 3'd0;
            dsel  <= 2'd0;
            dcnt  <= 3'd0;
            ready <= 3'b000;
        end else begin
            if (fill) begin
                fcnt <= fpos + 3'd1;
                if (fpos == 3'd7) begin
                    fsel <= (fsel == 2'd2) ? 2'd0 : fsel + 2'd1;
                end
            end
            if (sc_go) begin
                ready[sc_bank] <= 1'b1;
            end
            if (drain) begin
                dcnt <= dcnt + 3'd1;
                if (dcnt == 3'd7) begin
                    ready[dsel] <= 1'b0;
                    dsel <= (dsel == 2'd2) ? 2'd0 : dsel + 2'd1;
                end
            end
        end
    end

    // ---- quantise: 16 elements a cycle --------------------------------------
    // q1: decode and the row's scale; q2: multiply; q3: window, round, clamp.
    reg [10:0]       qs  [0:15];
    reg signed [7:0] qt  [0:15];
    reg              qg  [0:15];
    reg [12:0]       qr  [0:15];
    reg              qv1, qh1, qb1;
    reg [1:0]        qw1;
    reg [7:0]        qf1 [0:3];
    reg [15:0]       hh;
    always @(posedge clk) begin
        if (rst) begin
            qv1 <= 1'b0;
        end else begin
            qv1 <= drain;
        end
        qh1 <= dcnt[0];               // second half of an output word
        qw1 <= dcnt[2:1];
        qb1 <= b_layout;
        for (r = 0; r < 4; r = r + 1) qf1[r] <= sfld[dsel][r];
        for (s = 0; s < 16; s = s + 1) begin
            hh     = top[s*16 +: 16];
            qs[s] <= (hh[14:10] == 5'd0) ? {1'b0, hh[9:0]} : {1'b1, hh[9:0]};
            qt[s] <= sbase[dsel][s/4]
                   - ((hh[14:10] == 5'd0) ? 8'sd1 : $signed({3'b000, hh[14:10]}));
            qg[s] <= hh[15];
            qr[s] <= srec[dsel][s/4];
        end
    end

    reg [23:0]       pm  [0:15];
    reg [2:0]        pu  [0:15];
    reg              pz  [0:15];
    reg              psat[0:15];
    reg              pg  [0:15];
    reg              qv2, qh2, qb2;
    reg [1:0]        qw2;
    reg [7:0]        qf2 [0:3];
    generate
    if (MUL_DSP != 0) begin : g_dsp
        (* use_dsp = "yes" *) reg [23:0] pmd [0:15];
        always @(posedge clk) begin
            for (s = 0; s < 16; s = s + 1) pmd[s] <= qs[s] * qr[s];
        end
        always @(*) begin
            for (s = 0; s < 16; s = s + 1) pm[s] = pmd[s];
        end
    end else begin : g_lut
        (* use_dsp = "no" *) reg [23:0] pml [0:15];
        always @(posedge clk) begin
            for (s = 0; s < 16; s = s + 1) pml[s] <= qs[s] * qr[s];
        end
        always @(*) begin
            for (s = 0; s < 16; s = s + 1) pm[s] = pml[s];
        end
    end
    endgenerate
    always @(posedge clk) begin
        if (rst) begin
            qv2 <= 1'b0;
        end else begin
            qv2 <= qv1;
        end
        qh2 <= qh1;
        qw2 <= qw1;
        qb2 <= qb1;
        for (r = 0; r < 4; r = r + 1) qf2[r] <= qf1[r];
        for (s = 0; s < 16; s = s + 1) begin
            pu[s]   <= qt[s][2:0];
            pz[s]   <= (qs[s] == 11'd0) || (!qt[s][7] && (|qt[s][6:3]));
            psat[s] <= qt[s][7];
            pg[s]   <= qg[s];
        end
    end

    reg [7:0] x8, sm;
    reg [5:0] mg;
    reg [6:0] q [0:15];
    reg       qv3, qh3, qb3;
    reg [1:0] qw3;
    reg [7:0] qf3 [0:3];
    always @(posedge clk) begin
        if (rst) begin
            qv3 <= 1'b0;
        end else begin
            qv3 <= qv2;
        end
        qh3 <= qh2;
        qw3 <= qw2;
        qb3 <= qb2;
        for (r = 0; r < 4; r = r + 1) qf3[r] <= qf2[r];
        for (s = 0; s < 16; s = s + 1) begin
            x8 = pm[s][22:15] >> pu[s];
            sm = {1'b0, x8[7:1]} + {7'd0, x8[0]};
            mg = (psat[s] || (|sm[7:6])) ? 6'd63 : sm[5:0];
            q[s] <= pz[s] ? 7'd0 : (pg[s] ? (~{1'b0, mg} + 7'd1) : {1'b0, mg});
        end
    end

    // ---- assemble: two drained words make one out word ----------------------
    // Element s of a drained word is row s/4, kk = 4*half + s%4; every slot
    // below is a constant, so the layout costs enables, not a write mux.
    reg [255:0] acc;
    always @(posedge clk) begin
        if (rst) begin
            out_valid <= 1'b0;
        end else begin
            out_valid <= qv3 & qh3;
        end
        if (qv3) begin
            for (s = 0; s < 16; s = s + 1) begin
                case ({qb3, qh3})
                    2'b00: acc[255 - ((s/4)*8 + s%4)*7 -: 7]          = q[s];
                    2'b01: acc[255 - ((s/4)*8 + 4 + s%4)*7 -: 7]      = q[s];
                    2'b10: acc[255 - ((s%4)*4 + s/4)*7 -: 7]          = q[s];
                    default: acc[255 - ((s%4)*4 + 16 + s/4)*7 -: 7]   = q[s];
                endcase
            end
            acc[31:0] = {qf3[0], qf3[1], qf3[2], qf3[3]};
            if (qh3) begin
                out_data <= acc;
            end
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (!rst && fill && drain && (fsel == dsel)) begin
            $display("%0t ERROR sru_mx7 %m: a bank fills while it drains", $time);
        end
    end
`endif

endmodule

`default_nettype wire

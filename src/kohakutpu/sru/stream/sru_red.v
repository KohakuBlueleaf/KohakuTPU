// Per-row reduction over a sub-row of 4 x 4 sub-tile words (lane 4r + c is
// row r), one word a cycle. The spec and the reference are
// scripts/py/sru/red.py.
//
// KIND "max": the largest E8M15 of each row under the total order of
//   key = s ? ~w[22:0] : {1, w[22:0]} (-NaN < -inf < ... < -0 < +0 < ... <
//   +inf < +NaN). out[r] is the winning word.
//
// KIND "sum": a block sum per row. EM is the running max exponent field of
//   the row (word by word, never falling); each element aligns to it as
//   a = trunc(sig * 2^8 >> (EM - e)) (0 for e = 0 or past 23) with its sign;
//   T = the row's four a; A = (A >>> (EM_n - EM_n-1)) + T, 48-bit signed.
//   out[r] = {special[1:0], EM[7:0], A[47:0]}: the row's value is
//   A * 2^(EM - 150) unless special (1 = a NaN or both infinities, 2 = +inf,
//   3 = -inf). The finalise rounds it.
//
// first/last mark a sub-row's first and last word (both on a one-word row).

`default_nettype none

(* shreg_extract = "no" *)
module sru_red #(
    parameter         KIND = "max",
    // max only: element width, 24 (E8M15) or 16 (FP16, the low 256 bits)
    parameter integer EW   = 24
)(
    input  wire           clk,
    input  wire           rst,

    input  wire           in_valid,
    input  wire           in_first,
    input  wire           in_last,
    input  wire [383:0]   in_data,

    output reg            out_valid,
    output reg  [4*58-1:0] out          // max: [r*58 +: 24]; sum: 58 bits a row
);
    genvar r;

    // ---- control: valid/first/last through the stages ----------------------
    // max lands its result a cycle after the last word's update, sum two.
    localparam integer OLAT = (KIND == "max") ? 0 : 1;
    reg [4:0] v, f, l;
    always @(posedge clk) begin
        if (rst) begin
            v <= 5'd0;
            out_valid <= 1'b0;
        end else begin
            v <= {v[3:0], in_valid};
            out_valid <= v[3+OLAT] & l[3+OLAT];
        end
        f <= {f[3:0], in_first};
        l <= {l[3:0], in_last};
    end

    generate
    if (KIND == "max") begin : g_max
        for (r = 0; r < 4; r = r + 1) begin : g_row
            wire [EW-1:0] w0 = in_data[(4*r+0)*EW +: EW];
            wire [EW-1:0] w1 = in_data[(4*r+1)*EW +: EW];
            wire [EW-1:0] w2 = in_data[(4*r+2)*EW +: EW];
            wire [EW-1:0] w3 = in_data[(4*r+3)*EW +: EW];

            // keys
            function [EW-1:0] key;
                input [EW-1:0] w;
                key = w[EW-1] ? {1'b0, ~w[EW-2:0]} : {1'b1, w[EW-2:0]};
            endfunction

            reg [EW-1:0] a1, b1, m2, acc;
            reg [EW-1:0] x0, x1, x2, x3;
            always @(posedge clk) begin
                x0 <= w0; x1 <= w1; x2 <= w2; x3 <= w3;
                // stage 1: two pairs
                a1 <= (key(x1) > key(x0)) ? x1 : x0;
                b1 <= (key(x3) > key(x2)) ? x3 : x2;
                // stage 2: the word's max
                m2 <= (key(b1) > key(a1)) ? b1 : a1;
                // stage 3: the running max
                if (v[2] && f[2]) begin
                    acc <= m2;
                end else if (v[2] && (key(m2) > key(acc))) begin
                    acc <= m2;
                end
                if (v[3] && l[3]) begin
                    out[r*58 +: 58] <= {{(58-EW){1'b0}}, acc};
                end
            end
        end
    end else begin : g_sum
        for (r = 0; r < 4; r = r + 1) begin : g_row
            wire [23:0] w [0:3];
            assign w[0] = in_data[(4*r+0)*24 +: 24];
            assign w[1] = in_data[(4*r+1)*24 +: 24];
            assign w[2] = in_data[(4*r+2)*24 +: 24];
            assign w[3] = in_data[(4*r+3)*24 +: 24];

            // ---- stage 1: inputs, the word's max exponent, specials -------
            reg [23:0] x [0:3];
            reg [7:0]  em1;
            reg [1:0]  sp1;          // 0 none, 1 NaN, 2 +inf, 3 -inf
            integer i;
            reg [7:0] ea, eb, et;
            reg       anan, apinf, aninf;
            always @(*) begin
                ea = (w[0][22:15] > w[1][22:15]) ? w[0][22:15] : w[1][22:15];
                eb = (w[2][22:15] > w[3][22:15]) ? w[2][22:15] : w[3][22:15];
                et = (ea > eb) ? ea : eb;
                anan = 1'b0; apinf = 1'b0; aninf = 1'b0;
                for (i = 0; i < 4; i = i + 1) begin
                    if (w[i][22:15] == 8'hFF) begin
                        if (w[i][14:0] != 15'd0) anan = 1'b1;
                        else if (w[i][23]) aninf = 1'b1;
                        else apinf = 1'b1;
                    end
                end
            end
            always @(posedge clk) begin
                for (i = 0; i < 4; i = i + 1) x[i] <= w[i];
                em1 <= et;
                sp1 <= (anan | (apinf & aninf)) ? 2'd1 : apinf ? 2'd2 : aninf ? 2'd3 : 2'd0;
            end

            // ---- stage 2: the running max exponent (an 8-bit loop) --------
            reg [7:0] EM, gr2;
            reg [23:0] x2 [0:3];
            reg [1:0]  sp2;
            wire [7:0] emn = (f[0] || (em1 > EM)) ? em1 : EM;
            always @(posedge clk) begin
                if (v[0]) begin
                    EM  <= emn;
                end
                gr2 <= f[0] ? 8'd0 : (emn - EM);
                for (i = 0; i < 4; i = i + 1) x2[i] <= x[i];
                sp2 <= sp1;
            end

            // ---- stage 3: align to EM; a negative element as ~mag ---------
            // The two's complement's +1 rides the adders' carry-ins below.
            reg  [7:0]  EM3, gr3;
            reg  [24:0] a3 [0:3];
            reg  [3:0]  s3;
            reg  [1:0]  sp3;
            reg  [7:0]  d;
            reg  [23:0] mg;
            always @(posedge clk) begin
                for (i = 0; i < 4; i = i + 1) begin
                    d  = EM - x2[i][22:15];
                    mg = ((x2[i][22:15] == 8'd0) || (&x2[i][22:15]) || (d > 8'd23))
                       ? 24'd0 : ({1'b1, x2[i][14:0], 8'd0} >> d[4:0]);
                    a3[i] <= {1'b0, mg} ^ {25{x2[i][23]}};
                    s3[i] <= x2[i][23];
                end
                EM3 <= EM;
                gr3 <= gr2;
                sp3 <= sp2;
            end

            // ---- stage 4: the word's row sum, three of the four +1s --------
            wire signed [26:0] p01 = $signed({{2{a3[0][24]}}, a3[0]}) + $signed({{2{a3[1][24]}}, a3[1]})
                                   + $signed({26'd0, s3[0]});
            wire signed [26:0] p23 = $signed({{2{a3[2][24]}}, a3[2]}) + $signed({{2{a3[3][24]}}, a3[3]})
                                   + $signed({26'd0, s3[2]});
            reg  signed [26:0] t4;
            reg         c4;
            reg  [7:0]  EM4, gr4;
            reg  [1:0]  sp4;
            always @(posedge clk) begin
                t4  <= p01 + p23 + $signed({26'd0, s3[1]});
                c4  <= s3[3];
                EM4 <= EM3;
                gr4 <= gr3;
                sp4 <= sp3;
            end

            // ---- accumulate (the loop: shift-on-growth + add + the last +1)
            reg signed [47:0] A;
            reg  [1:0]  SP;
            reg  [7:0]  EMA;
            reg         done;
            // $signed on the fill arm: an unsigned arm makes the >>> logical.
            wire signed [47:0] ash = (gr4 > 8'd47) ? $signed({48{A[47]}}) : (A >>> gr4[5:0]);
            wire [1:0] spn = (SP == 2'd1 || sp4 == 2'd1) ? 2'd1
                           : (SP == 2'd0) ? sp4
                           : (sp4 == 2'd0 || sp4 == SP) ? SP : 2'd1;
            always @(posedge clk) begin
                if (v[3]) begin
                    A   <= (f[3] ? 48'sd0 : ash) + {{21{t4[26]}}, t4} + {47'd0, c4};
                    SP  <= f[3] ? sp4 : spn;
                    EMA <= EM4;
                end
                done <= v[3] & l[3];
                if (done) begin
                    out[r*58 +: 58] <= {SP, EMA, A};
                end
            end
        end
    end
    endgenerate

endmodule

`default_nettype wire

// R2's row sums -> E8M15, one row a cycle: {special, EM, A} is
// A * 2^(EM - 150), rounded once to nearest even (special 1 NaN, 2 +inf,
// 3 -inf). A word of 4 rows in, 4 rows out on the following cycles.

`default_nettype none

(* shreg_extract = "no" *)
module sru_sum2e8 (
    input  wire          clk,
    input  wire          rst,
    input  wire          in_valid,
    input  wire [231:0]  in_rows,      // 4 x {sp[1:0], EM[7:0], A[47:0]}
    output reg           out_valid,
    output reg  [95:0]   out_rows      // 4 x E8M15, all at once
);
    reg  [231:0] hold;
    reg  [2:0]   n;                    // rows left to convert, 4..0
    reg  [1:0]   ri;
    wire [57:0]  cur = hold[ri*58 +: 58];

    // stage 1: magnitude
    reg [47:0] mag1;
    reg        s1, v1, last1;
    reg [1:0]  sp1, r1;
    reg [7:0]  em1;
    always @(posedge clk) begin
        if (rst) begin
            n <= 3'd0;
            v1 <= 1'b0;
        end else begin
            v1 <= (n != 3'd0);
            if (in_valid) begin
                n <= 3'd4;
            end else if (n != 3'd0) begin
                n <= n - 3'd1;
            end
        end
        if (in_valid) begin
            hold <= in_rows;
            ri   <= 2'd0;
        end else if (n != 3'd0) begin
            ri   <= ri + 2'd1;
        end
        mag1  <= cur[47] ? (~cur[47:0] + 48'd1) : cur[47:0];
        s1    <= cur[47];
        sp1   <= cur[57:56];
        em1   <= cur[55:48];
        r1    <= ri;
        last1 <= (n == 3'd1);
    end

    // stage 2: lead one
    reg [47:0] y;
    reg [5:0]  pos2;
    reg [47:0] mag2;
    reg        s2, v2, last2, nz2;
    reg [1:0]  sp2, r2;
    reg [7:0]  em2;
    integer    k, b;
    always @(*) begin
        y = mag1;
        for (k = 1; k < 48; k = k * 2) y = y | (y >> k);
    end
    reg [5:0] pe;
    always @(*) begin
        pe = 6'd0;
        for (b = 0; b < 48; b = b + 1) begin
            if (y[b] & ~((b == 47) ? 1'b0 : y[b+1])) pe = b;
        end
    end
    always @(posedge clk) begin
        if (rst) begin
            v2 <= 1'b0;
        end else begin
            v2 <= v1;
        end
        pos2 <= pe; mag2 <= mag1; nz2 <= y[0];
        s2 <= s1; sp2 <= sp1; em2 <= em1; r2 <= r1; last2 <= last1;
    end

    // stage 3: normalise; the field is EM + pos - 23 before the round's carry
    wire [63:0] nrm = {16'd0, mag2} << (~pos2);
    reg  [15:0] sig3;
    reg         g3, st3, s3, v3, last3, nz3;
    reg  [1:0]  sp3, r3;
    reg  signed [10:0] e3;
    always @(posedge clk) begin
        if (rst) begin
            v3 <= 1'b0;
        end else begin
            v3 <= v2;
        end
        sig3 <= nrm[63:48];
        g3   <= nrm[47];
        st3  <= |nrm[46:0];
        e3   <= $signed({3'd0, em2}) + $signed({5'd0, pos2}) - 11'sd23;
        s3 <= s2; sp3 <= sp2; nz3 <= nz2; r3 <= r2; last3 <= last2;
    end

    // stage 4: round, bounds, assemble
    wire [16:0] sr = {1'b0, sig3} + {16'd0, g3 & (st3 | sig3[0])};
    wire signed [10:0] ef = e3 + (sr[16] ? 11'sd1 : 11'sd0);
    reg [23:0] w;
    always @(*) begin
        if (sp3 == 2'd1) begin
            w = 24'h7FC000;
        end else if (sp3 == 2'd2) begin
            w = {1'b0, 8'hFF, 15'd0};
        end else if (sp3 == 2'd3) begin
            w = {1'b1, 8'hFF, 15'd0};
        end else if (!nz3) begin
            w = 24'd0;
        end else if (ef >= 11'sd255) begin
            w = {s3, 8'hFF, 15'd0};
        end else if (ef <= 11'sd0) begin
            w = {s3, 23'd0};
        end else begin
            w = {s3, ef[7:0], sr[16] ? 15'd0 : sr[14:0]};
        end
    end
    always @(posedge clk) begin
        if (rst) begin
            out_valid <= 1'b0;
        end else begin
            out_valid <= v3 & last3;
        end
        if (v3) begin
            out_rows[r3*24 +: 24] <= w;
        end
    end
endmodule

`default_nettype wire

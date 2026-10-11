// vec_agu's strided address generator (8 descriptors, base + 4 (stride,
// bound)), with TWO walkers sharing the descriptor table: walker 0 serves
// fills, walker 1 drains, so a load and a send run at once. A walker latches
// its descriptor and offset at `start`; the two starts share one table read,
// and walker 1's wins a tie (`start_ok`).
//
// `addr` is a register. A walker keeps the address where its current
// dimension-1 run began (b1) and counts dimensions down (`left0` words, r1..r3
// runs). With bounds n_k and strides s_k, ending a dimension-k run starts the
// next at b1 + D_k, b1 then holding the ending run's last dimension-1 run:
//   D1 = s1,  D2 = s2 - (n1-1) s1,  D3 = s3 - (n2-1) s2 - (n1-1) s1
// D2 and D3 settle three cycles after `start`; the core's first step is later.
//
// A step takes n words (`n`, out): one, or where dimension 0 is contiguous
// (stride 32 bytes) min(cap, left0), cap >= 1. Ending dimension 0 carries
// into dimension 1 as a single step would.

`default_nettype none

module v2_agu #(
    parameter integer AW = 40
)(
    input  wire               clk,
    input  wire               rst,

    input  wire               wr_en,
    input  wire [2:0]         wr_ad,
    input  wire [2:0]         wr_fld,     // 0 = base, 1..4 = dim 0..3
    input  wire [AW-1:0]      wr_val,

    input  wire [1:0]         start,
    output wire [1:0]         start_ok,
    input  wire [5:0]         sel,        // {walker 1, walker 0}
    input  wire [2*AW-1:0]    off,        // added to the base, modulo 2^AW
    input  wire [1:0]         step,
    input  wire [31:0]        cap,        // 16 each
    output wire [31:0]        n,
    output wire [1:0]         contig,     // dimension 0 is contiguous

    output wire [2*AW-1:0]    addr,
    output wire [63:0]        total,
    output wire [35:0]        stride0,
    output wire [31:0]        left0
);
    reg [AW-1:0] base  [0:7];
    reg [17:0] strd  [0:7][0:3];
    reg [15:0] bnd   [0:7][0:3];

    wire [1:0] fld_sel = wr_fld[1:0] - 2'd1;

    integer d, k;
    always @(posedge clk) begin
        if (rst) begin
            for (d = 0; d < 8; d = d + 1) begin
                base[d] <= {AW{1'b0}};
                for (k = 0; k < 4; k = k + 1) begin
                    strd[d][k] <= 18'd0;
                    bnd[d][k]  <= 16'd1;
                end
            end
        end else if (wr_en) begin
            if (wr_fld == 3'd0) begin
                base[wr_ad] <= wr_val;
            end else if (wr_fld <= 3'd4) begin
                strd[wr_ad][fld_sel] <= wr_val[33:16];
                bnd[wr_ad][fld_sel]  <= wr_val[15:0];
            end
        end
    end

    // One table read a cycle.
    assign start_ok = {start[1], start[0] && !start[1]};
    wire [2:0] rsel = start[1] ? sel[5:3] : sel[2:0];
    wire [AW-1:0] t_base = base[rsel];
    wire [15:0] t_b0 = (bnd[rsel][0] == 16'd0) ? 16'd1 : bnd[rsel][0];
    wire [15:0] t_b1 = (bnd[rsel][1] == 16'd0) ? 16'd1 : bnd[rsel][1];
    wire [15:0] t_b2 = (bnd[rsel][2] == 16'd0) ? 16'd1 : bnd[rsel][2];
    wire [15:0] t_b3 = (bnd[rsel][3] == 16'd0) ? 16'd1 : bnd[rsel][3];

    genvar w;
    generate
    for (w = 0; w < 2; w = w + 1) begin : g_w
        wire go = start_ok[w];
        wire [AW-1:0] w_off = off[w*AW +: AW];
        wire [15:0] cp = cap[w*16 +: 16];

        reg [15:0] cb0, cb1m, cb2m, cb3;
        reg signed [17:0] cs0, cs1, cs2, cs3;
        reg [15:0] lft, r1, r2, r3;
        reg [AW-1:0] a, b1;
        reg busy, c32;

        reg  signed [34:0] p1, p2;
        reg  [AW-1:0]      d2, d3, q12;
        always @(posedge clk) begin
            p1  <= $signed({1'b0, cb1m}) * cs1;
            p2  <= $signed({1'b0, cb2m}) * cs2;
            d2  <= {{(AW-18){cs2[17]}}, cs2} - {{(AW-35){p1[34]}}, p1};
            q12 <= {{(AW-35){p1[34]}}, p1} + {{(AW-35){p2[34]}}, p2};
            d3  <= {{(AW-18){cs3[17]}}, cs3} - q12;
        end
        wire [AW-1:0] bstep = (r1 != 16'd0) ? {{(AW-18){cs1[17]}}, cs1}
                            : (r2 != 16'd0) ? d2 : d3;
        wire [AW-1:0] bsum = b1 + bstep;
        wire [AW-1:0] a0 = t_base + w_off;
        // in0: the step stays in dimension 0.
        wire in0 = c32 ? (cp < lft) : (lft != 16'd1);
        wire [15:0] nw = !c32 ? 16'd1 : in0 ? cp : lft;
        wire [AW-1:0] a_in = c32 ? a + {{(AW-21){1'b0}}, cp, 5'd0}
                                 : a + {{(AW-18){cs0[17]}}, cs0};

        assign n[w*16 +: 16] = nw;
        assign contig[w] = c32;
        assign addr[w*AW +: AW] = a;
        assign stride0[w*18 +: 18] = cs0;
        assign left0[w*16 +: 16] = lft;

        reg [31:0] t01, t23, total_r;
        reg [15:0] s01, s23;
        always @(posedge clk) begin
            t01     <= {16'd0, cb0} * ({16'd0, cb1m} + 32'd1);
            t23     <= ({16'd0, cb2m} + 32'd1) * {16'd0, cb3};
            s01     <= (t01[31:16] != 16'd0) ? 16'hFFFF : t01[15:0];
            s23     <= (t23[31:16] != 16'd0) ? 16'hFFFF : t23[15:0];
            total_r <= s01 * s23;
        end
        assign total[w*32 +: 32] = total_r;

        always @(posedge clk) begin
            if (rst) begin
                cb0 <= 16'd1; cb1m <= 16'd0; cb2m <= 16'd0; cb3 <= 16'd1;
                cs0 <= 18'd0; cs1 <= 18'd0; cs2 <= 18'd0; cs3 <= 18'd0;
                lft <= 16'd1; r1 <= 16'd0; r2 <= 16'd0; r3 <= 16'd0;
                a <= {AW{1'b0}}; b1 <= {AW{1'b0}};
                busy <= 1'b0; c32 <= 1'b0;
            end else if (go) begin
                cb0 <= t_b0; cb1m <= t_b1 - 16'd1; cb2m <= t_b2 - 16'd1; cb3 <= t_b3;
                cs0 <= strd[rsel][0]; cs1 <= strd[rsel][1];
                cs2 <= strd[rsel][2]; cs3 <= strd[rsel][3];
                c32 <= (strd[rsel][0] == 18'd32);
                lft <= t_b0;
                r1 <= t_b1 - 16'd1; r2 <= t_b2 - 16'd1; r3 <= t_b3 - 16'd1;
                a <= a0; b1 <= a0;
                busy <= 1'b1;
            end else if (step[w] && busy) begin
                if (in0) begin
                    lft <= lft - nw;
                    a   <= a_in;
                end else if ((r1 | r2 | r3) != 16'd0) begin
                    lft <= cb0;
                    a <= bsum; b1 <= bsum;
                    if (r1 != 16'd0) begin
                        r1 <= r1 - 16'd1;
                    end else if (r2 != 16'd0) begin
                        r1 <= cb1m; r2 <= r2 - 16'd1;
                    end else begin
                        r1 <= cb1m; r2 <= cb2m; r3 <= r3 - 16'd1;
                    end
                end else begin
                    busy <= 1'b0;
                end
            end
        end
    end
    endgenerate

endmodule

`default_nettype wire

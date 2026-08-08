// Tensor compute unit: 4 x 8 x 4, int7 elements, 128 MACs per cycle.
//
// For a fixed k and j the activation B[k][j] is shared across all four rows of
// A, which is exactly the shape mx_mac wants -- two rows of A per DSP, one
// activation on the shared port. So the tile decomposes into 8 chains of 8:
//
//        chain(p,j),  p = row pair 0..1,  j = column 0..3
//        stage k      k = 0..7            one DSP, two output rows
//
//   +--------+  +--------+        +--------+
//   | k=0  Z=0  | k=0    |  ...   |        |   stage 0 starts from zero
//   +---||---+  +---||---+        +--------+
//   | k=1 PCIN  |        |        |            dedicated cascade, no fabric
//   +---||---+  +---||---+        +--------+
//   |  ...   |  |        |        |        |
//   +---||---+  +---||---+        +--------+
//   | k=7 PCIN + C(prev TCU)      |        |   cross-TCU partial on W
//   +--------+  +--------+        +--------+
//
// 8 chains x 8 DSP = 64 DSP, 128 MACs/cycle, one 4x8x4 tile per cycle.
//
// A cascade adds one pipeline stage per DSP, so stage k's operands must arrive
// k cycles after stage 0's. The skew is held in shift registers, which map to
// SRL32: one LUT per bit at any depth up to 32.
//
// Latency: operands at t -> part_out at t + 11.
//   stage k operands at t+k, its P at t+k+4, so stage 7 completes at t+11.

`default_nettype none

module mx_tcu #(
    parameter integer S     = 19,
    parameter integer FIRST = 0,    // 1 = head of a cluster, no upstream partial
    parameter integer MODEL = 0
)(
    input  wire         clk,
    input  wire         rst,
    input  wire         en,

    // A[i][k] at a_in[(i*8+k)*7 +: 7],  i = row 0..3, k = 0..7
    input  wire [223:0] a_in,
    // B[k][j] at b_in[(k*4+j)*7 +: 7],  k = 0..7, j = column 0..3
    input  wire [223:0] b_in,

    // packed partials, chain c = p*4 + j at [c*48 +: 48]
    input  wire [383:0] part_in,
    output wire [383:0] part_out
);
    localparam integer NK = 8;

    // ------------------------------------------------------------------ skew
    // Stage k needs every operand it will use, delayed by k. Bundle them so one
    // shift register serves the whole stage: 4 rows of A and 4 columns of B.
    wire [55:0] stage_op [0:NK-1];

    genvar k, p, j;
    generate
    for (k = 0; k < NK; k = k + 1) begin : g_skew
        wire [55:0] raw = { a_in[(0*8+k)*7 +: 7], a_in[(1*8+k)*7 +: 7],
                            a_in[(2*8+k)*7 +: 7], a_in[(3*8+k)*7 +: 7],
                            b_in[(k*4+0)*7 +: 7], b_in[(k*4+1)*7 +: 7],
                            b_in[(k*4+2)*7 +: 7], b_in[(k*4+3)*7 +: 7] };
        if (k == 0) begin : g_nodelay
            assign stage_op[k] = raw;
        end else begin : g_delay
            reg [55:0] sr [0:k-1];
            integer d;
            always @(posedge clk) begin
                if (rst) begin
                    for (d = 0; d < k; d = d + 1) sr[d] <= 56'd0;
                end else if (en) begin
                    sr[0] <= raw;
                    for (d = 1; d < k; d = d + 1) sr[d] <= sr[d-1];
                end
            end
            assign stage_op[k] = sr[k-1];
        end
    end
    endgenerate

    // unpack one stage's bundle
    `define A_OF(kk, ii) stage_op[kk][55 - (ii)*7 -: 7]
    `define B_OF(kk, jj) stage_op[kk][27 - (jj)*7 -: 7]

    // ---------------------------------------------------------------- chains
    generate
    for (p = 0; p < 2; p = p + 1) begin : g_pair
        for (j = 0; j < 4; j = j + 1) begin : g_col
            localparam integer C_IDX = p*4 + j;

            wire signed [47:0] casc [0:NK];
            assign casc[0] = 48'sd0;

            for (k = 0; k < NK; k = k + 1) begin : g_stage
                // stage 0 starts from zero; the rest take the cascade.
                // the last stage also absorbs the previous TCU's partial on W.
                localparam integer ZS = (k == 0) ? 1 : 0;
                localparam integer WS = (k == NK-1 && FIRST == 0) ? 1 : 0;

                wire signed [47:0] p_out, pc_out;

                mx_mac #(.S(S), .Z_SEL(ZS), .W_SEL(WS), .MODEL(MODEL)) u_mac (
                    .clk(clk), .rst(rst), .en(en),
                    .w_hi(`A_OF(k, 2*p+1)),     // upper field -> odd row
                    .w_lo(`A_OF(k, 2*p)),       // lower field -> even row
                    .act (`B_OF(k, j)),
                    .pcin(casc[k]),
                    .cin (part_in[C_IDX*48 +: 48]),
                    .p(p_out), .pcout(pc_out)
                );

                assign casc[k+1] = pc_out;
                if (k == NK-1) assign part_out[C_IDX*48 +: 48] = p_out;
            end
        end
    end
    endgenerate

    `undef A_OF
    `undef B_OF

endmodule

`default_nettype wire

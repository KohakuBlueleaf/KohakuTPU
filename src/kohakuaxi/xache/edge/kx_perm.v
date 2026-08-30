// Elaboration-time address bit permutation for the Xache: NSWAP bit-pair swaps,
// applied in order, as pure wires. Moving a low field into the home field IS
// channel interleaving at that field's granularity, and costs nothing: the
// permutation is a bijection on a dense address space, and every consumer
// downstream (home select, set index, tag, DRAM address) only reads fields.
// Use a ROTATION -- pairs (i, i+log2 N) for i = G .. HOME_LSB-1 -- not a plain
// swap of the two fields: a swap parks the constant top bits inside the set
// index and idles 1/N of every array. Guard: every index must lie in
// [MIN_BIT, WIDTH), MIN_BIT = max(LINE_LSB, 12) -- below the line a cache line
// would straddle homes, below 4 KB an AXI burst could change home mid-burst.
// An offending pair instantiates an undefined module: the build fails.

`default_nettype none

module kx_perm #(
    parameter integer WIDTH   = 40,
    parameter integer NSWAP   = 0,
    parameter [((NSWAP < 1) ? 1 : NSWAP)*8-1:0] SWAP_A = 0,   // bit index, one byte per pair
    parameter [((NSWAP < 1) ? 1 : NSWAP)*8-1:0] SWAP_B = 0,
    parameter integer MIN_BIT = 12
)(
    input  wire [WIDTH-1:0] i,
    output wire [WIDTH-1:0] o
);
    localparam integer NSW = (NSWAP < 1) ? 1 : NSWAP;
    wire [WIDTH-1:0] st [0:NSW];
    assign st[0] = i;

    genvar s, b;
    generate for (s = 0; s < NSW; s = s + 1) begin : g_s
        localparam integer A = SWAP_A[s*8 +: 8];
        localparam integer B = SWAP_B[s*8 +: 8];
        if (s < NSWAP) begin : g_on
            if (A < MIN_BIT || B < MIN_BIT || A >= WIDTH || B >= WIDTH) begin : g_bad
                kx_perm_swap_must_be_ge_line_and_4KB u_illegal ();
            end
            for (b = 0; b < WIDTH; b = b + 1) begin : g_b
                assign st[s+1][b] = (b == A) ? st[s][B] : (b == B) ? st[s][A] : st[s][b];
            end
        end else begin : g_off
            assign st[s+1] = st[s];
        end
    end endgenerate

    assign o = st[NSW];
endmodule

`default_nettype wire

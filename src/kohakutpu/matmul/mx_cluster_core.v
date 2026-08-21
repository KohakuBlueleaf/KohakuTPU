// The four chained tensor CUs, without an accumulator.
//
//   K 0..7        K 8..15       K 16..23      K 24..31
//   +-------+     +-------+     +-------+     +-------+
//   | TCU 0 |--C->| TCU 1 |--C->| TCU 2 |--C->| TCU 3 |--> part_out
//   +-------+     +-------+     +-------+     +-------+
//
// Split out from mx_cluster so the same verified datapath feeds either
// accumulator -- the exact fixed-point one mx_cluster_tb checks bit-for-bit, or
// the floating-point one a real machine uses.
//
// TIMING -- the W path costs TWO cycles, not one. C is registered once
// (CREG=1), so `c_r <= cin` at edge E captures a value valid after E-1, and the
// ALU consumes c_r at E+1:
//
//     TCU c stage-7 P   lands after   E(11 + d_c)
//     it consumes cin   valid after   E(9  + d_c)
//     TCU c-1 part_out  valid after   E(11 + d_{c-1})
//     =>  d_c = d_{c-1} + 2
//
// Assuming one cycle silently drops half the products: the downstream TCU
// samples its neighbour's partial a cycle early, when it still holds the
// previous tile's value (or zero on the first tile).
//
// Latency, operands to part_valid: 11 + 2*(NTCU-1) = 17 cycles.

`default_nettype none

module mx_cluster_core #(
    parameter integer S     = 19,
    parameter integer MODEL = 0
)(
    input  wire         clk,
    input  wire         rst,
    input  wire         en,

    // A[i][kk] at a_in[(i*32+kk)*7 +: 7],  i = 0..3, kk = 0..31
    input  wire [895:0] a_in,
    // B[kk][j] at b_in[(kk*4+j)*7 +: 7],   kk = 0..31, j = 0..3
    input  wire [895:0] b_in,

    input  wire         in_valid,
    input  wire         in_first,      // travels with the tile, meaning "start a tile"

    output wire [383:0] part_out,      // 8 chains x 48, packed two results each
    output wire         part_valid,
    output wire         part_first
);
    localparam integer NTCU     = 4;
    localparam integer CTRL_DLY = 11 + 2*(NTCU - 1);

    // ------------------------------------------------- per-TCU operand slices
    wire [223:0] a_slice [0:NTCU-1];
    wire [223:0] b_slice [0:NTCU-1];

    genvar c, i, j, k;
    generate
    for (c = 0; c < NTCU; c = c + 1) begin : g_slice
        for (i = 0; i < 4; i = i + 1) begin : g_arow
            for (k = 0; k < 8; k = k + 1) begin : g_ak
                assign a_slice[c][(i*8+k)*7 +: 7] = a_in[(i*32 + c*8 + k)*7 +: 7];
            end
        end
        for (k = 0; k < 8; k = k + 1) begin : g_bk
            for (j = 0; j < 4; j = j + 1) begin : g_bcol
                assign b_slice[c][(k*4+j)*7 +: 7] = b_in[((c*8 + k)*4 + j)*7 +: 7];
            end
        end
    end
    endgenerate

    // --------------------------------------------------------- operand delay
    // TCU c is two cycles behind TCU c-1 so its last stage meets the upstream
    // partial. Depth 2..6, where a register chain beats a shift-register LUT.
    wire [223:0] a_dly [0:NTCU-1];
    wire [223:0] b_dly [0:NTCU-1];

    generate
    for (c = 0; c < NTCU; c = c + 1) begin : g_delay
        if (c == 0) begin : g_d0
            assign a_dly[c] = a_slice[c];
            assign b_dly[c] = b_slice[c];
        end else begin : g_dn
            localparam integer DLY = 2*c;
            // FLOPS, NOT SRLs -- see mx_tcu.v: one LUT per bit at any depth, and
            // these are 2, 4 and 6 deep against a 16-stage SRL.
            (* shreg_extract = "no" *) reg [223:0] ad [0:DLY-1];
            (* shreg_extract = "no" *) reg [223:0] bd [0:DLY-1];
            integer d;
            // Still no reset: operands qualified by `vld_sr` below.
            always @(posedge clk) begin
                if (en) begin
                    ad[0] <= a_slice[c];
                    bd[0] <= b_slice[c];
                    for (d = 1; d < DLY; d = d + 1) begin
                        ad[d] <= ad[d-1]; bd[d] <= bd[d-1];
                    end
                end
            end
            assign a_dly[c] = ad[DLY-1];
            assign b_dly[c] = bd[DLY-1];
        end
    end
    endgenerate

    // ------------------------------------------------------------ TCU chain
    wire [383:0] part [0:NTCU];
    assign part[0] = 384'd0;        // TCU 0 has no upstream partial

    generate
    for (c = 0; c < NTCU; c = c + 1) begin : g_tcu
        mx_tcu #(.S(S), .FIRST(c == 0 ? 1 : 0), .MODEL(MODEL)) u_tcu (
            .clk(clk), .rst(rst), .en(en),
            .a_in(a_dly[c]), .b_in(b_dly[c]),
            .part_in(part[c]),
            .part_out(part[c+1])
        );
    end
    endgenerate

    assign part_out = part[NTCU];

    // control travels with the data
    reg [CTRL_DLY-1:0] vld_sr, fst_sr;
    always @(posedge clk) begin
        if (rst) begin
            vld_sr <= 0; fst_sr <= 0;
        end else if (en) begin
            vld_sr <= {vld_sr[CTRL_DLY-2:0], in_valid};
            fst_sr <= {fst_sr[CTRL_DLY-2:0], in_first};
        end
    end

    assign part_valid = vld_sr[CTRL_DLY-1];
    assign part_first = fst_sr[CTRL_DLY-1];

endmodule

`default_nettype wire

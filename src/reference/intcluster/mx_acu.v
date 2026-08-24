// Accumulator CU: unpack the cluster's packed partials, apply the block scale,
// accumulate an output tile.
//
// The cluster hands over 8 chains of 48 bits. Each holds two results in
// disjoint fields, because one DSP carried two rows of A:
//
//     P  =  U * 2^19  +  L        U = sum of w_hi*a   (odd row)
//                                 L = sum of w_lo*a   (even row)
//
// Extraction is wiring plus one increment. The whole 48-bit word is a single
// two's-complement accumulation, so a negative L borrows from U's field:
//
//     L = signed( P[18:0] )                 no logic
//     U = P[47:19] + P[18]                  add back the borrow
//
// Accumulation here is EXACT FIXED POINT: a bring-up instrument, checkable
// against a model bit for bit. The production accumulator is S1E7M<ACC_MW> --
// see docs/compute/matmul-circuit.md s6.2.
//
// The scale here is a PLAIN 8-BIT EXPONENT per row of A and per column of B, so
// output (i,j) is scaled by 2^(sa[i] + sb[j]). CONTRACT: the caller must choose
// an anchor that makes no shift negative.
//
// That is NOT the machine's scale format -- the shipping path carries E5M3 and
// mx_acu_fp multiplies the partial sum by the two mantissas. A pure
// power-of-two scale keeps this block's comparison bit-for-bit.

`default_nettype none

module mx_acu #(
    parameter integer ACCW   = 48,  // accumulator width per output element
    // Largest scale-vs-anchor difference honoured. sa+sb-anchor is nominally 8
    // bits, which builds a 0..255 barrel shifter per lane and dominated the
    // cluster's LUT count -- for range that cannot be used, since a shift past
    // ACCW leaves the accumulator anyway. 5 bits removes three shifter stages
    // from all 16 lanes.
    parameter integer SH_MAX = 31
)(
    input  wire         clk,
    input  wire         rst,
    input  wire         en,

    input  wire [383:0] part_in,    // 8 chains x 48, from the last TCU
    input  wire [31:0]  sa,         // 4 x 8-bit exponent, one per row of A
    input  wire [31:0]  sb,         // 4 x 8-bit exponent, one per column of B
    input  wire [7:0]   anchor,     // common exponent for this output tile
    input  wire         in_valid,
    input  wire         acc_clear,  // start a fresh output tile

    output wire [16*ACCW-1:0] acc_out,   // element (i,j) at [(i*4+j)*ACCW +: ACCW]
    output reg               acc_valid
);
    // ------------------------------------------------------- stage 1: extract
    reg signed [29:0] val_r [0:15];
    reg               v1;
    reg  [7:0]        sa_r [0:3];
    reg  [7:0]        sb_r [0:3];
    reg  [7:0]        anchor_r;
    reg               clear_r;

    integer i, j;
    always @(posedge clk) begin
        // VALIDS ONLY. val_r/sa_r/sb_r/anchor_r are 552 flops that `v1` qualifies
        // at every use below, and `clear_r` is read only inside `if (v1)`.
        if (rst) begin
            v1 <= 1'b0;
        end else if (en) begin
            v1       <= in_valid;
            clear_r  <= acc_clear;
            anchor_r <= anchor;
            for (i = 0; i < 4; i = i + 1) begin
                sa_r[i] <= sa[i*8 +: 8];
                sb_r[i] <= sb[i*8 +: 8];
            end
            for (i = 0; i < 4; i = i + 1) begin
                for (j = 0; j < 4; j = j + 1) begin
                    // chain index for row i, column j: the row pair is i>>1
                    // even rows are the lower field, odd rows the upper
                    if (i[0] == 1'b0) begin
                        val_r[i*4+j] <= $signed(part_in[((i>>1)*4+j)*48 +: 19]);
                    end
                    else begin
                        val_r[i*4+j] <= $signed(part_in[((i>>1)*4+j)*48 + 19 +: 29])
                                      + $signed({1'b0, part_in[((i>>1)*4+j)*48 + 18]});
                    end
                end
            end
        end
    end

    // -------------------------------------------- stage 2: scale, accumulate
    reg signed [ACCW-1:0] acc [0:15];

    genvar g;
    generate
    for (g = 0; g < 16; g = g + 1) begin : g_out
        assign acc_out[g*ACCW +: ACCW] = acc[g];
    end
    endgenerate

    // Shift for element (i,j) relative to the tile anchor, clamped to SH_MAX.
    // The clamp bounds the hardware; it does not make an out-of-range anchor
    // correct.
    function [4:0] shamt;
        input integer ii;
        input integer jj;
        reg [8:0] raw;
        begin
            raw   = {1'b0, sa_r[ii]} + {1'b0, sb_r[jj]} - {1'b0, anchor_r};
            shamt = (raw > SH_MAX[8:0]) ? SH_MAX[4:0] : raw[4:0];
        end
    endfunction

    always @(posedge clk) begin
        // RESET-RISK: `acc` is unreset only because `clear_r` LOADS rather than
        // adds. A sweep starting without acc_clear resumes the previous tile.
        if (rst) begin
            acc_valid <= 1'b0;
        end else if (en) begin
            acc_valid <= v1;
            if (v1) begin
                for (i = 0; i < 4; i = i + 1) begin
                    for (j = 0; j < 4; j = j + 1) begin
                        if (clear_r) begin
                            acc[i*4+j] <= $signed(val_r[i*4+j]) <<< shamt(i, j);
                        end
                        else begin
                            acc[i*4+j] <= acc[i*4+j]
                                        + ($signed(val_r[i*4+j]) <<< shamt(i, j));
                        end
                    end
                end
            end
        end
    end

endmodule

`default_nettype wire

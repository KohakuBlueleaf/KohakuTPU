// The 4x4 granule transpose, streaming, one word in and one word out a shift.
//
// A word is four granules of G bits. In GT4 mode four consecutive input words
// w0..w3 form a group and leave as four words where
//
//     out word i, granule w  =  in word w, granule i
//
// which is its own inverse, so unpack (tile -> flat) and pack (flat -> tile)
// use the same block. In plain mode a word passes unchanged. Both modes have
// the same latency, three shifts, so the two can follow each other freely.
//
// HOW. Granule i of every input word enters shift register SR_i. Out word i
// of a group leaves at the shift that brings in w3 plus i, and needs granule i
// of word w, which entered 3 + i - w shifts earlier. So output granule w at
// output phase i is SR_i tap (3 + i - w): a 4:1 select per output bit, the
// phase being the select. Plain mode is phase i = w for every granule, which
// is SR_w tap 3 -- the same four candidates, so the mode costs no extra mux.
//
// SHIFTING. A shift happens when a word arrives, or when no word arrives but
// outputs are still inside (`pend`) and the input sits at a group boundary.
// A group's four words must arrive on four consecutive shifts; the caller
// keeps a GT4 group contiguous by never stalling inside one -- if it has to
// stall there, the block simply does not shift until the next word comes.
// Each word carries a TW-bit tag (address, register, ...) which leaves with
// the output word that took its slot: out word i carries in word i's tag.

`default_nettype none

module v2_gt4 #(
    parameter integer G  = 64,       // granule bits; a word is 4 granules
    parameter integer TW = 1
)(
    input  wire           clk,
    input  wire           rst,

    input  wire           in_valid,
    input  wire           in_gt4,    // this word belongs to a GT4 group
    input  wire [4*G-1:0] in_data,
    input  wire [TW-1:0]  in_tag,

    output reg            out_valid,
    output reg  [4*G-1:0] out_data,
    output reg  [TW-1:0]  out_tag,
    // Words inside that have not left yet; the caller's flush and idle test.
    output wire           busy
);
    // R_g[k], k = 1..6: granule g of the word that arrived k shifts ago; [0]
    // is never written, so `tap` can index it in its untaken arm.
    reg [G-1:0]  r0 [0:6];
    reg [G-1:0]  r1 [0:6];
    reg [G-1:0]  r2 [0:6];
    reg [G-1:0]  r3 [0:6];
    // Tag line: valid, gt4, phase, tag. Only taps 1..3 are ever read.
    reg [3:1]    tv;
    reg [3:1]    tg;
    reg [1:0]    tp [1:3];
    reg [TW-1:0] tt [1:3];

    // Input phase within a GT4 group; 0 at a group boundary.
    reg [1:0] ph;

    wire pend  = tv[1] | tv[2] | tv[3];
    wire shift = in_valid | (pend & (ph == 2'd0));
    assign busy = pend;

    // Tap k of SR_g, tap 0 being the word arriving now.
    function [G-1:0] tap;
        input integer g;
        input integer k;
        begin
            case (g)
                0: tap = (k == 0) ? in_data[0*G +: G] : r0[k];
                1: tap = (k == 0) ? in_data[1*G +: G] : r1[k];
                2: tap = (k == 0) ? in_data[2*G +: G] : r2[k];
                default: tap = (k == 0) ? in_data[3*G +: G] : r3[k];
            endcase
        end
    endfunction

    // Output granule w: phase i picks SR_i tap (3 + i - w).
    wire [1:0] oph = tp[3];
    wire       ogt = tg[3];
    reg  [4*G-1:0] onext;
    integer w;
    always @(*) begin
        for (w = 0; w < 4; w = w + 1) begin
            case (ogt ? oph : w[1:0])
                2'd0: onext[w*G +: G] = tap(0, 3 - w);
                2'd1: onext[w*G +: G] = tap(1, 4 - w);
                2'd2: onext[w*G +: G] = tap(2, 5 - w);
                default: onext[w*G +: G] = tap(3, 6 - w);
            endcase
        end
    end

    integer k;
    always @(posedge clk) begin
        if (rst) begin
            tv <= 3'd0;
            ph <= 2'd0;
            out_valid <= 1'b0;
        end else begin
            out_valid <= shift & tv[3];
            if (shift) begin
                out_data <= onext;
                out_tag  <= tt[3];
                r0[1] <= in_data[0*G +: G];
                r1[1] <= in_data[1*G +: G];
                r2[1] <= in_data[2*G +: G];
                r3[1] <= in_data[3*G +: G];
                for (k = 2; k <= 6; k = k + 1) begin
                    r0[k] <= r0[k-1];
                    r1[k] <= r1[k-1];
                    r2[k] <= r2[k-1];
                    r3[k] <= r3[k-1];
                end
                tv <= {tv[2:1], in_valid};
                tg <= {tg[2:1], in_gt4};
                tp[1] <= ph;  tp[2] <= tp[1];  tp[3] <= tp[2];
                tt[1] <= in_tag;  tt[2] <= tt[1];  tt[3] <= tt[2];
                if (in_valid && in_gt4) begin
                    ph <= ph + 2'd1;
                end
            end
        end
    end

`ifndef SYNTHESIS
    // A plain word inside a GT4 group would be taken as one of its four.
    always @(posedge clk) begin
        if (!rst && in_valid && !in_gt4 && (ph != 2'd0)) begin
            $display("V2_GT4_SPLIT %m t=%0t: plain word inside a GT4 group", $time);
        end
    end
`endif

endmodule

`default_nettype wire

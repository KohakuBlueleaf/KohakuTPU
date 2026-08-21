// khd_perm -- the cross-lane network: slide, saturating pack, and widening
// unpack. Everything in the DSP datapath that moves data BETWEEN lanes.
//
// Elementwise work never crosses lanes, which is what keeps the lane array
// cheap; the three operations here are the exceptions, and each exists because
// a real kernel cannot be written without it:
//
//   vsldw   a stencil's misaligned neighbour. Line-aligned loads are a
//           contract, so the only way to reach element i-1 is to slide two
//           loaded vectors past each other.
//   vpack   the requantise epilogue's last step: int32 accumulators down to
//           int8, saturating, two vectors in and one out.
//   vunpk   the other direction, for widening before an accumulation.
//
// THE SLIDE IS THE EXPENSIVE ONE and it is why HAS_PERM exists. Each output
// lane picks one of 2*SIMD input lanes, so it is a 32-bit mux per lane whose
// width grows with SIMD -- the only structure here that does. Pack and unpack
// are per-element and nearly free; unpack is pure wiring plus a sign bit.
//
// A SLIDE IS A ROTATE OF THE CONCATENATION, not a clamp. Every index 0..7 is
// then defined at every SIMD width, so the RTL and the golden model cannot
// disagree about what happens past the end -- which is the shape of hole a
// "shift in zeroes" or "clamp to the last lane" answer leaves.

`default_nettype none

module khd_perm #(
    parameter integer SIMD     = 8,
    parameter integer HAS_PERM = 1
)(
    input  wire [32*SIMD-1:0] v1,
    input  wire [32*SIMD-1:0] v2,
    input  wire [3:0]         op4,
    input  wire [2:0]         idx,
    output wire [32*SIMD-1:0] y
);
    localparam integer VW = 32 * SIMD;

`include "khd_isa.vh"

    genvar i;
    generate
    if (HAS_PERM == 0) begin : g_none
        assign y = {VW{1'b0}};
    end else begin : g_perm

        // ---- slide: lane i takes lane (idx + i) mod 2*SIMD ----
        wire [63:0] cat_l [0:2*SIMD-1];
        wire [VW-1:0] sldw;
        for (i = 0; i < 2 * SIMD; i = i + 1) begin : g_cat
            assign cat_l[i] = (i < SIMD) ? {32'd0, v1[32*i +: 32]}
                                         : {32'd0, v2[32*(i-SIMD) +: 32]};
        end
        for (i = 0; i < SIMD; i = i + 1) begin : g_sldw
            assign sldw[32*i +: 32] =
                cat_l[(idx + i) % (2 * SIMD)][31:0];
        end

        // ---- saturating pack: {v2, v1} narrowed, one element per half ----
        // VW/16 int16 elements per SOURCE, and both sources are consumed --
        // half that count leaves the top half of the result undriven, which
        // reads as high-Z and then spreads X through everything downstream.
        localparam integer H = VW / 16;
        wire [VW-1:0] pack16, pack32;
        for (i = 0; i < H; i = i + 1) begin : g_p16
            // int16 -> int8. It fits when the discarded bits are all copies of
            // the kept sign bit, which is one AND and one NOR rather than two
            // magnitude compares.
            wire [15:0] a = v1[16*i +: 16];
            wire [15:0] b = v2[16*i +: 16];
            wire fa = (&a[15:7]) | (~|a[15:7]);
            wire fb = (&b[15:7]) | (~|b[15:7]);
            assign pack16[8*i +: 8]         = fa ? a[7:0] : (a[15] ? 8'h80 : 8'h7F);
            assign pack16[8*(i + H) +: 8]   = fb ? b[7:0] : (b[15] ? 8'h80 : 8'h7F);
        end
        for (i = 0; i < SIMD; i = i + 1) begin : g_p32
            wire [31:0] a = v1[32*i +: 32];
            wire [31:0] b = v2[32*i +: 32];
            wire fa = (&a[31:15]) | (~|a[31:15]);
            wire fb = (&b[31:15]) | (~|b[31:15]);
            assign pack32[16*i +: 16]           = fa ? a[15:0]
                                                     : (a[31] ? 16'h8000 : 16'h7FFF);
            assign pack32[16*(i + SIMD) +: 16]  = fb ? b[15:0]
                                                     : (b[31] ? 16'h8000 : 16'h7FFF);
        end

        // ---- unpack: sign-extend half the elements. Wiring and a sign bit. ----
        wire [VW-1:0] unp8l, unp8h, unp16l, unp16h;
        for (i = 0; i < VW / 16; i = i + 1) begin : g_u8
            assign unp8l[16*i +: 16] = {{8{v1[8*i + 7]}}, v1[8*i +: 8]};
            assign unp8h[16*i +: 16] = {{8{v1[VW/2 + 8*i + 7]}},
                                        v1[VW/2 + 8*i +: 8]};
        end
        for (i = 0; i < SIMD; i = i + 1) begin : g_u16
            assign unp16l[32*i +: 32] = {{16{v1[16*i + 15]}}, v1[16*i +: 16]};
            assign unp16h[32*i +: 32] = {{16{v1[VW/2 + 16*i + 15]}},
                                         v1[VW/2 + 16*i +: 16]};
        end

        reg [VW-1:0] sel;
        always @(*) begin
            case (op4)
                KHD_PRM_PACK_S16:  sel = pack16;
                KHD_PRM_PACK_S32:  sel = pack32;
                KHD_PRM_UNPKL_S8:  sel = unp8l;
                KHD_PRM_UNPKH_S8:  sel = unp8h;
                KHD_PRM_UNPKL_S16: sel = unp16l;
                KHD_PRM_UNPKH_S16: sel = unp16h;
                default:           sel = sldw;
            endcase
        end
        assign y = sel;
    end
    endgenerate

endmodule

`default_nettype wire

// khs_perm -- the cross-lane network: slide, saturating pack, and widening
// unpack. Everything in the SIMD datapath that moves data BETWEEN lanes.
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
// A SLIDE IS A ROTATE OF THE CONCATENATION, not a clamp. Every index 0..7 is
// then defined at every SIMD width, so the RTL and the golden model cannot
// disagree about what happens past the end -- which is the shape of hole a
// "shift in zeroes" or "clamp to the last lane" answer leaves.
//
// UNITS IS A WIDTH, AND THE ISA DOES NOT KNOW IT. The whole block is written
// PER OUTPUT WORD rather than as seven full-width results and a mux: unit u on
// pass p produces output word p*UNITS + u, and khs_unit walks SIMD/UNITS passes
// into a staging register. At UNITS = SIMD the pass index is a constant, every
// source index folds to a constant, and this is the fixed wiring it was.
//
// THE COST IS NOT THE SLIDE ALONE. `HAS_PERM` measures 1,884 LUT at SIMD 8 and
// the slide is roughly a quarter of it: the pack saturations are one test per
// output byte and the unpacks one sign per half, so all three scale with the
// OUTPUT width and all three shrink with UNITS.

`default_nettype none

module khs_perm #(
    parameter integer SIMD     = 8,
    parameter integer HAS_PERM = 1,
    // 0 = one unit per 32-bit output word, which is what every build had before
    // the width was separable and is what keeps them bit-identical.
    parameter integer UNITS    = 0,
    // Derived, and in the parameter list because the port list needs it: a
    // localparam in the body is declared too late to size a port.
    parameter integer PSW = ((SIMD / ((UNITS == 0) ? SIMD : UNITS)) > 1)
                          ? $clog2(SIMD / ((UNITS == 0) ? SIMD : UNITS)) : 1
)(
    input  wire [32*SIMD-1:0] v1,
    input  wire [32*SIMD-1:0] v2,
    input  wire [3:0]         op4,
    input  wire [2:0]         idx,
    // Which UNITS-sized slice of the result this pass produces.
    input  wire [PSW-1:0]     pass,
    output wire [32*((UNITS == 0) ? SIMD : UNITS)-1:0] y
);
    localparam integer U  = (UNITS == 0) ? SIMD : UNITS;
    localparam integer VW = 32 * SIMD;
    //: word, half and byte index widths into the OUTPUT.
    localparam integer EW = (SIMD > 1) ? $clog2(SIMD) : 1;
    localparam integer KW = EW + 1;
    localparam integer JW = EW + 2;
    //: int16 elements in one source vector -- the pack's two halves meet here.
    localparam integer H  = VW / 16;

`include "khs_isa.vh"

    genvar i, b;
    generate
    if (HAS_PERM == 0) begin : g_none
        assign y = {(32*U){1'b0}};

    end else if (U == SIMD) begin : g_full
        // ONE UNIT PER WORD KEEPS THE ORIGINAL FULL-WIDTH FORM, and that is a
        // measurement rather than tidiness: the per-word body below costs 476
        // LUT MORE than this at SIMD 8 (16,217 against 15,741 on the assembled
        // PE), because the tool shares the pack and unpack wiring across lanes
        // and the per-word form asks it to build each output separately. A
        // width nobody uses must cost nothing.

        // ---- slide: lane i takes lane (idx + i) mod 2*SIMD ----
        wire [63:0] cat_l [0:2*SIMD-1];
        wire [VW-1:0] sldw;
        for (i = 0; i < 2 * SIMD; i = i + 1) begin : g_cat
            assign cat_l[i] = (i < SIMD) ? {32'd0, v1[32*i +: 32]}
                                         : {32'd0, v2[32*(i-SIMD) +: 32]};
        end
        // LEAVE THIS AS AN INDEXED SELECT. `idx` is three bits so `idx + i`
        // never reaches 2*SIMD and the tool already prunes the modulo to an
        // 8-way mux. Rewriting it as an explicit `if (idx == k)` loop to "help"
        // measured 1,600 -> 1,824 LUT: a priority chain is not a mux.
        for (i = 0; i < SIMD; i = i + 1) begin : g_sldw
            assign sldw[32*i +: 32] = cat_l[(idx + i) % (2 * SIMD)][31:0];
        end

        // ---- saturating pack: {v2, v1} narrowed, one element per half ----
        // LEAVE THE TWO WIDTHS SEPARATE. Output half h reads the same 32-bit
        // source word in both forms, so folding them into one narrower with a
        // runtime width select looks free -- and measured 1,600 -> 1,765 LUT,
        // because the separate form lets the output mux below absorb the
        // saturation constants into LUTs it was paying for anyway.
        wire [VW-1:0] pack16, pack32;
        for (i = 0; i < H; i = i + 1) begin : g_p16
            wire [15:0] a = v1[16*i +: 16];
            wire [15:0] b16 = v2[16*i +: 16];
            wire fa = (&a[15:7]) | (~|a[15:7]);
            wire fb = (&b16[15:7]) | (~|b16[15:7]);
            assign pack16[8*i +: 8]       = fa ? a[7:0] : (a[15] ? 8'h80 : 8'h7F);
            assign pack16[8*(i + H) +: 8] = fb ? b16[7:0]
                                               : (b16[15] ? 8'h80 : 8'h7F);
        end
        for (i = 0; i < SIMD; i = i + 1) begin : g_p32
            wire [31:0] a = v1[32*i +: 32];
            wire [31:0] b32 = v2[32*i +: 32];
            wire fa = (&a[31:15]) | (~|a[31:15]);
            wire fb = (&b32[31:15]) | (~|b32[31:15]);
            assign pack32[16*i +: 16]          = fa ? a[15:0]
                                                    : (a[31] ? 16'h8000 : 16'h7FFF);
            assign pack32[16*(i + SIMD) +: 16] = fb ? b32[15:0]
                                                    : (b32[31] ? 16'h8000 : 16'h7FFF);
        end

        // ---- unpack: sign-extend half the elements. Wiring and a sign bit. ----
        wire [VW-1:0] unp8l, unp8h, unp16l, unp16h;
        for (i = 0; i < H; i = i + 1) begin : g_u8
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
                KHS_PRM_PACK_S16:  sel = pack16;
                KHS_PRM_PACK_S32:  sel = pack32;
                KHS_PRM_UNPKL_S8:  sel = unp8l;
                KHS_PRM_UNPKH_S8:  sel = unp8h;
                KHS_PRM_UNPKL_S16: sel = unp16l;
                KHS_PRM_UNPKH_S16: sel = unp16h;
                default:           sel = sldw;
            endcase
        end
        assign y = sel;
        wire _unused_pass = &{1'b0, pass, 1'b0};

    end else begin : g_perm

    for (i = 0; i < U; i = i + 1) begin : g_unit
        // THE OUTPUT WORD THIS UNIT OWNS ON THIS PASS. Constant at UNITS = SIMD.
        wire [EW-1:0] e = pass * U + i;

        // ---- slide: output word e takes {v2,v1} word (idx + e) mod 2*SIMD ----
        // Six bits before the truncation: 2*SIMD is a power of two, so taking
        // the low KW bits IS the modulo, and letting the sum truncate early
        // would wrap at the wrong width.
        wire [5:0]    s_raw = {3'd0, idx} + {{(6-EW){1'b0}}, e};
        wire [KW-1:0] s_ix  = s_raw[KW-1:0];
        wire [EW-1:0] s_w   = s_ix[EW-1:0];
        wire [31:0]   sldw  = s_ix[EW] ? v2[32*s_w +: 32] : v1[32*s_w +: 32];

        // ---- saturating pack, int16 -> int8: four output bytes ----
        // It fits when the discarded bits are all copies of the kept sign bit,
        // which is one AND and one NOR rather than two magnitude compares.
        // ONE SATURATION PER OUTPUT BYTE, not per source element: the source is
        // selected first, so a narrow build tests four and not 4*SIMD.
        wire [31:0] pack16;
        for (b = 0; b < 4; b = b + 1) begin : g_p16
            wire [JW-1:0] j  = {e, 2'd0} + b[1:0];
            wire [KW-1:0] jm = j[KW-1:0];
            wire [15:0]   s  = j[KW] ? v2[16*jm +: 16] : v1[16*jm +: 16];
            wire          f  = (&s[15:7]) | (~|s[15:7]);
            assign pack16[8*b +: 8] = f ? s[7:0] : (s[15] ? 8'h80 : 8'h7F);
        end

        // ---- saturating pack, int32 -> int16: two output halves ----
        wire [31:0] pack32;
        for (b = 0; b < 2; b = b + 1) begin : g_p32
            wire [KW-1:0] k  = {e, 1'd0} + b[0];
            wire [EW-1:0] km = k[EW-1:0];
            wire [31:0]   s  = k[EW] ? v2[32*km +: 32] : v1[32*km +: 32];
            wire          f  = (&s[31:15]) | (~|s[31:15]);
            assign pack32[16*b +: 16] = f ? s[15:0]
                                          : (s[31] ? 16'h8000 : 16'h7FFF);
        end

        // ---- unpack: sign-extend. Wiring and a sign bit. ----
        wire [31:0] unp8l, unp8h;
        for (b = 0; b < 2; b = b + 1) begin : g_u8
            wire [KW-1:0] k = {e, 1'd0} + b[0];
            assign unp8l[16*b +: 16] = {{8{v1[8*k + 7]}},          v1[8*k +: 8]};
            assign unp8h[16*b +: 16] = {{8{v1[VW/2 + 8*k + 7]}},
                                        v1[VW/2 + 8*k +: 8]};
        end
        wire [31:0] unp16l = {{16{v1[16*e + 15]}},         v1[16*e +: 16]};
        wire [31:0] unp16h = {{16{v1[VW/2 + 16*e + 15]}},
                              v1[VW/2 + 16*e +: 16]};

        reg [31:0] sel;
        always @(*) begin
            case (op4)
                KHS_PRM_PACK_S16:  sel = pack16;
                KHS_PRM_PACK_S32:  sel = pack32;
                KHS_PRM_UNPKL_S8:  sel = unp8l;
                KHS_PRM_UNPKH_S8:  sel = unp8h;
                KHS_PRM_UNPKL_S16: sel = unp16l;
                KHS_PRM_UNPKH_S16: sel = unp16h;
                default:           sel = sldw;
            endcase
        end
        assign y[32*i +: 32] = sel;
    end

    end
    endgenerate

    // A unit count that does not divide SIMD leaves output words unwritten by a
    // walk that still elaborates and reports an Fmax.
    generate
    if ((UNITS != 0) && (((SIMD / UNITS) * UNITS) != SIMD)) begin : g_bad_pu
        khs_perm_requires_UNITS_to_divide_SIMD u_bad ();
    end
    endgenerate

endmodule

`default_nettype wire

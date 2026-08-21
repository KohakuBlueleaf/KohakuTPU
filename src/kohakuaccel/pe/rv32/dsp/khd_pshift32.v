// khd_pshift32 -- one 32-bit lane's packed shifter.
//
// ONE ROTATE, NOT TWO SHIFTERS, and no bit reversal either.
//
// A packed shift looks like it needs a left barrel, a right barrel and a
// per-element bit reversal to share them. It needs none of that: a left shift
// by s is a 32-bit ROTATE right by 32-s, and a right shift by s is a rotate
// right by s, and in both cases every bit that arrived from the wrong element
// -- including the ones the rotate wrapped around the word -- lands exactly
// where the element mask is already zero. One barrel per lane, and the masks
// are the same in every lane and every element, so they are built ONCE for the
// whole unit rather than SIMD times.
//
//   right logical    (x rot>> s)      & keep      keep = low (EW-s) bits, per element
//   right arithmetic (x rot>> s)      & keep  | (~keep & sign)
//   left             (x rot>> 32-s)   & ~low      low  = low s bits, per element
//
// The bit reversal the base core's EX stage uses for the same trick is free
// there because it is one 32-bit word; here it would be a three-way mux on 32
// bits per lane -- 512 LUT at SIMD8 -- because the reversal has to happen
// WITHIN an element and the element width is a runtime field.
//
// ROUNDING IS AN INCREMENT, NOT AN ADD. `vsrari` is round-half-up, which is
// (x >>> s) + bit s-1 of x: the carry-in per element rather than a half-ulp
// added before the shift. The two agree exactly, and the increment reuses the
// lane's packed adder instead of needing one of its own.
//
// The round bit has to come out of the ORIGINAL word and not out of the rotate:
// the rotate lands x[e*EW + s - 1] at the top of the element BELOW e, so
// picking it out of the rotated word reads the wrong element's bit. `rmask`
// selects it in place -- one bit per element, built once per unit like the
// others -- and the OR-reduce over each element puts it at the element's LSB.

`default_nettype none

module khd_pshift32 (
    input  wire [31:0] x,
    input  wire [1:0]  et,           // 0 = s8, 1 = s16, 2 = s32
    input  wire [4:0]  rot,          // s for a right shift, 32-s for a left one
    input  wire [31:0] keep,         // per-element mask, built once per unit
    input  wire [31:0] rmask,        // per-element bit (s-1), likewise
    input  wire        arith,        // 1 = replicate the element's sign
    input  wire        left,

    output wire [31:0] y,
    // Bit (s-1) of each element, in the element's LSB position: the round-half-up
    // carry, which the lane's adder applies.
    output wire [31:0] round_bit
);
    localparam [1:0] ET_S8 = 2'd0, ET_S16 = 2'd1;

    wire [63:0] dbl = {x, x};
    wire [31:0] rr  = dbl[rot +: 32];       // rotate right by `rot`

    // Sign of the element each bit belongs to, replicated across the element.
    wire [31:0] sgn = (et == ET_S8)
                        ? {{8{x[31]}}, {8{x[23]}}, {8{x[15]}}, {8{x[7]}}}
                    : (et == ET_S16)
                        ? {{16{x[31]}}, {16{x[15]}}}
                        : {32{x[31]}};

    assign y = left ? (rr & keep)
                    : ((rr & keep) | (arith ? (~keep & sgn) : 32'd0));

    wire [31:0] rsel = x & rmask;
    assign round_bit = (et == ET_S8)
                        ? {7'd0, |rsel[31:24], 7'd0, |rsel[23:16],
                           7'd0, |rsel[15:8],  7'd0, |rsel[7:0]}
                    : (et == ET_S16)
                        ? {15'd0, |rsel[31:16], 15'd0, |rsel[15:0]}
                        : {31'd0, |rsel};

endmodule

`default_nettype wire

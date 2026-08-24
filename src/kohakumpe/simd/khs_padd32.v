// khs_padd32 -- one 32-bit lane's packed adder: add, subtract, saturate, and
// the signed compare that min/max and the reductions are built from.
//
// ONE NATIVE CARRY CHAIN, and that is the whole design. The obvious packed
// adder is four byte adders whose carries between them are gated by the element
// width -- and it is what this module was first, and it was measured at
// **2.05 ns of a 4.72 ns critical path**: gating a carry puts a LUT between the
// bytes, so the tool cannot use one CARRY8 chain and builds seven in series.
//
// The SWAR construction gets the same answer from ONE 32-bit add. Clear each
// element's most significant bit in both operands so no carry can leave an
// element, add, then XOR the cleared bits back:
//
//     M    = each element's MSB      s8 80808080  s16 80008000  s32 80000000
//     add    ((a & ~M) + (b & ~M)) ^ ((a ^ b) & M)
//     sub    ((a | M) - (b & ~M)) ^ ((a ^ ~b) & M)
//
// The subtract's `a | M` is what stops a borrow leaving an element: the field
// is then at least its own MSB, and a subtrahend below that cannot borrow out.
//
// SATURATION AND THE COMPARE COME FROM THE SIGNS, and they have to, because the
// carry out of an element's MSB is exactly what the construction suppresses.
// Everything needed is still there: the carry INTO the MSB is
// `y_ms ^ a_ms ^ b_ms`, the carry out is the majority of the three, and signed
// overflow is the two differing. Signed `a < b` is the difference's sign
// corrected by its overflow -- so min, max and the max-reduction cost a mux
// each rather than a comparator array of their own.
//
// `mask` IS AN INPUT, not derived here: it depends only on the element width,
// which is the same in every lane, so khs_unit builds it once in EX and
// registers it. Deriving it per lane would put a mux in front of the adder in
// the one cycle this module exists to keep short.

`default_nettype none

module khs_padd32 (
    input  wire [31:0] a,
    input  wire [31:0] b,
    input  wire        sub,          // 1 = a - b
    input  wire [1:0]  et,           // 0 = s8, 1 = s16, 2 = s32
    input  wire [31:0] mask,         // each element's MSB, built once per unit
    input  wire        sat,          // 1 = signed saturating

    output wire [31:0] y,
    // Per BYTE, meaningful only on a byte that ENDS an element: signed a < b.
    output wire [3:0]  lt,
    // The same flag over every byte of its element -- the form a min/max select
    // needs, and the form a caller would otherwise rebuild.
    output wire [3:0]  lt_s,
    output wire [3:0]  top           // which bytes end an element
);
    localparam [1:0] ET_S8 = 2'd0, ET_S16 = 2'd1;

    assign top = (et == ET_S8)  ? 4'b1111
               : (et == ET_S16) ? 4'b1010
                                : 4'b1000;

    // One add or one subtract. Both are a single 32-bit carry chain.
    wire [31:0] raw = sub ? ((a | mask) - (b & ~mask))
                          : ((a & ~mask) + (b & ~mask));
    wire [31:0] fix = sub ? ((a ^ ~b) & mask) : ((a ^ b) & mask);
    wire [31:0] sum = raw ^ fix;

    // ---- per-element flags, from the signs -------------------------------
    wire [3:0] a_ms = {a[31], a[23], a[15], a[7]};
    wire [3:0] b_ms = sub ? ~{b[31], b[23], b[15], b[7]}
                          :  {b[31], b[23], b[15], b[7]};
    wire [3:0] y_ms = {sum[31], sum[23], sum[15], sum[7]};

    // The carry the construction suppressed, recovered from the three signs.
    wire [3:0] cin  = y_ms ^ a_ms ^ b_ms;
    wire [3:0] cout = (a_ms & b_ms) | (cin & (a_ms ^ b_ms));
    wire [3:0] ovf  = (cin ^ cout) & top;

    assign lt = (y_ms ^ ovf) & top;

    // `e` IS AN ARGUMENT AND MUST STAY ONE. Read as a module-level net from
    // inside the function, a continuous assignment calling this is not reliably
    // sensitive to it, and the spread keeps whatever width was current when `f`
    // last changed. Measured: a vmin.s8 after a vsrli.s16 spread an s8 compare
    // with the s16 pattern and took four bytes from the wrong operand.
    function [3:0] spread;
        input [1:0] e;
        input [3:0] f;
        case (e)
            ET_S8:   spread = f;
            ET_S16:  spread = {f[3], f[3], f[1], f[1]};
            default: spread = {4{f[3]}};
        endcase
    endfunction

    assign lt_s = spread(et, lt);

    wire [3:0] hit = sat ? spread(et, ovf) : 4'd0;
    wire [3:0] neg = spread(et, a_ms & top);

    genvar k;
    generate
    for (k = 0; k < 4; k = k + 1) begin : g_out
        // Toward the operands' own sign. Only an element's TOP byte carries the
        // 0x80 / 0x7F pattern; the rest are all-zeros or all-ones.
        assign y[k*8 +: 8] = !hit[k]  ? sum[k*8 +: 8]
                           : top[k]   ? (neg[k] ? 8'h80 : 8'h7F)
                                      : (neg[k] ? 8'h00 : 8'hFF);
    end
    endgenerate

endmodule

`default_nettype wire

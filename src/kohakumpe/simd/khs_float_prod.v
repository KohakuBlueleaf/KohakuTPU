// khs_float_prod -- one product in the accumulator's float format. NARROW
// OPERANDS ONLY: the ports are 16 bits and the E7 argument below rests on that.
//
// The ONLY new float arithmetic the SIMD PE's float tier needs: everything
// around it is shipped RTL (vec_cvt for the conversions, mx_fpacc_align and
// mx_fpacc_round for the accumulate loop). It exists because a product must be
// finished BEFORE the accumulate loop begins -- a fused multiply-add puts the
// whole 14-cycle multiply inside the recurrence, which no amount of accumulator
// rotation makes cheap.
//
// NORMALISING A PRODUCT NEEDS NO LEADING-ONE SEARCH. Two normalised 16-bit
// significands multiply to a 32-bit value whose leading one is at bit 31 or bit
// 30 and nowhere else, so this is a one-bit shift and a round. The general
// search in mx_fpacc_norm is there for a sum of sixteen partial products; a
// single product does not need it.
//
// FORMAT OUT: S1 E7 M<MW>, the matmul accumulator's own, so the loop behind
// this is that accumulator's shipped align/add/round. At MW = 16 the word is 24
// bits -- the same width as E8M15 and one mantissa bit wider, so accumulating
// wider than the operands costs nothing.
//
// E7 IS ENOUGH, and that is a property of the source format rather than luck:
// an FP16 product's exponent spans roughly -48..+30 and E7 spans -63..64.

`default_nettype none

module khs_float_prod #(
    parameter integer MW = 16
)(
    input  wire [15:0]     a,          // FP16
    input  wire [15:0]     b,          // FP16
    output wire [MW+7:0]   p           // S1 E7 M<MW>, 0 exponent = zero
);
    wire [23:0] ae8, be8;
    vec_cvt_f16_to_e8 u_ca (.f16(a), .e8(ae8));
    vec_cvt_f16_to_e8 u_cb (.f16(b), .e8(be8));

    wire       sa = ae8[23], sb = be8[23];
    wire [7:0] ea = ae8[22:15], eb = be8[22:15];
    wire [15:0] siga = {1'b1, ae8[14:0]};
    wire [15:0] sigb = {1'b1, be8[14:0]};

    wire zero_in = (ea == 8'd0) || (eb == 8'd0);
    // An infinity or a NaN cannot be represented in the accumulator format, so
    // it saturates to the largest finite value rather than becoming a pattern
    // the accumulate loop would treat as an ordinary number.
    wire max_in  = (ea == 8'hFF) || (eb == 8'hFF);

    wire [31:0] prod = siga * sigb;

    // The leading one is at bit 31 or bit 30, so `up` IS the normalise.
    wire        up   = prod[31];
    wire [16:0] keep = up ? prod[31:15] : prod[30:14];
    wire        gd   = up ? prod[14]    : prod[13];
    wire        st   = up ? (|prod[13:0]) : (|prod[12:0]);

    // Round to nearest even at MW+1 significand bits.
    localparam integer DROP = 16 - MW;          // bits of `keep` below the format
    wire [16:0] trunc = keep >> DROP;
    wire        r_g   = (DROP == 0) ? gd : keep[(DROP > 0) ? (DROP - 1) : 0];
    wire        r_s   = (DROP == 0) ? st
                      : (st | gd | ((DROP > 1) ? (|keep[((DROP > 1) ? (DROP - 2) : 0):0]) : 1'b0));
    wire [17:0] rnd   = {1'b0, trunc} + {17'd0, (r_g & (r_s | trunc[0]))};
    wire        rcy   = rnd[MW + 1];            // rounded up to 2.0

    // value = keep/2^16 * 2^(ea+eb-254+up), so the E7-biased exponent is
    // ea + eb + up - 191, plus one if the rounding carried.
    wire signed [10:0] e7 = $signed({3'b0, ea}) + $signed({3'b0, eb})
                          + $signed({10'b0, up}) - 11'sd191
                          + $signed({10'b0, rcy});

    wire [MW-1:0] man = rcy ? {MW{1'b0}} : rnd[MW-1:0];
    wire          sgn = sa ^ sb;

    wire under = (e7 <= 11'sd0);
    wire over  = (e7 >= 11'sd127);

    assign p = (zero_in || under) ? {(MW+8){1'b0}}
             : (max_in || over)   ? {sgn, 7'd127, {MW{1'b1}}}
                                  : {sgn, e7[6:0], man};

endmodule

`default_nettype wire

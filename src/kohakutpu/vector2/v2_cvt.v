// E8M15 -> FP16 with ONE rounding for both FP16 ranges; bit-identical to
// vec_cvt_e8_to_f16 over all 2^24 inputs.
//
// The significand is shifted so its 11 kept bits are FP16's: by 5 for a
// normal result (e >= 113), by 118 - e (at most 31) for a subnormal one. The
// rounded 11 bits are then ADDED to the exponent field placed at bit 10:
// a normal significand's leading one lifts the field from e - 113 to e - 112,
// a rounding carry out of the significand lifts it once more, and a
// subnormal (field 0, no leading one) that rounds up to 2^10 becomes the
// smallest normal. One shifter and one adder serve both ranges: sixteen lanes
// with V2's F16/F32 pack select are 2,280 LUT against 3,183 with
// vec_cvt_e8_to_f16 (xcvu13p-2L, 0.73 ns slack at 3.0 ns).

`default_nettype none

module v2_e8_to_f16 (
    input  wire [23:0] e8,
    output wire [15:0] f16
);
    wire        s   = e8[23];
    wire [7:0]  e   = e8[22:15];
    wire [14:0] m   = e8[14:0];
    wire [15:0] sig = {1'b1, m};

    wire is_zero = (e == 8'd0);
    wire is_max  = (e == 8'hFF);
    wire is_nan  = is_max && (m != 15'd0);
    wire nrm     = (e >= 8'd113);

    wire [7:0]  sh_raw = 8'd118 - e;
    wire [5:0]  sh     = nrm ? 6'd5 : (sh_raw > 8'd31) ? 6'd31 : sh_raw[5:0];
    wire [47:0] wide   = {sig, 32'b0} >> sh;
    wire [10:0] k      = wide[42:32];
    wire        g      = wide[31];
    wire        st     = |wide[30:0];

    // e - 113 for e in 113..143 (113 = 17 mod 32).
    wire [4:0]  eb  = nrm ? (e[4:0] - 5'd17) : 5'd0;
    wire [15:0] sum = {1'b0, eb, 10'd0} + {5'd0, k} + {15'd0, g & (st | k[0])};
    wire        over = (e >= 8'd143) || (sum[14:10] == 5'h1F);

    assign f16 = is_zero ? {s, 15'd0}
               : is_nan  ? {s, 5'h1F, 1'b1, m[13:5]}
               : is_max  ? {s, 5'h1F, 10'd0}
               : over    ? {s, 5'h1E, 10'h3FF}
                         : {s, sum[14:0]};

endmodule

`default_nettype wire

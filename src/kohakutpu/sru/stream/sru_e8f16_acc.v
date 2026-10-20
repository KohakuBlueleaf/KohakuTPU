// E8M15 -> the clusters' accumulator FP16, as mx_fpacc_to_fp16 emits it: the
// fraction rounded to 10 bits (nearest even), a carry lifting the exponent,
// then below 2^-14 a signed zero and at 2^16 or above (inf and NaN included)
// a saturated +-65504. No subnormal, inf or NaN leaves.
// Two halves so a pipeline can register between them; `mid` is
// {s, e[7:0], fraction + round[10:0]}.

`default_nettype none

module sru_e8f16_acc_s1 (
    input  wire [23:0] e8,
    output wire [19:0] mid
);
    wire [14:0] m  = e8[14:0];
    wire        up = m[4] & ((|m[3:0]) | m[5]);
    assign mid = {e8[23:15], {1'b0, m[14:5]} + {10'd0, up}};
endmodule

module sru_e8f16_acc_s2 (
    input  wire [19:0] mid,
    output wire [15:0] f16
);
    wire        s  = mid[19];
    wire [7:0]  e  = mid[18:11];
    wire [10:0] mr = mid[10:0];
    // FP16 exponent e - 112, one more on a rounding carry
    wire [8:0]  ef = {1'b0, e} + {8'd0, mr[10]};

    assign f16 = (e == 8'd0) || (ef <= 9'd112) ? {s, 15'd0}
               : (ef >= 9'd143)                ? {s, 5'h1E, 10'h3FF}
                                               : {s, ef[4:0] - 5'd16, mr[10] ? 10'd0 : mr[9:0]};
endmodule

`default_nettype wire

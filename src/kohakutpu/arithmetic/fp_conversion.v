/*
Rounding haven't been implemented yet, so the result may not be accurate.
*/

module fp16_to_fp8e5m2(
    input [15:0] a,
    output [7:0] b
);
    wire a_sign = a[15];
    wire [4:0] a_exp = a[14:10];
    wire [9:0] a_mant = a[9:0];

    wire b_sign = a_sign;
    wire [6:2] b_exp = a_exp;
    wire [1:0] b_mant = a_mant[9:8];

    assign b = {b_sign, b_exp, b_mant};
endmodule


module fp16_to_fp8e4m3(
    input [15:0] a,
    output [7:0] b
);
    wire a_sign = a[15];
    wire [4:0] a_exp = a[14:10];
    wire [9:0] a_mant = a[9:0];

    // (exp + 15) - 15 -> (exp+7) - 7, match the e4m3 format
    wire [6:0] exp_adjusted = a_exp-4'b1000; 

    wire b_sign = a_sign;
    wire [6:5] b_exp = exp_adjusted[4] ? 4'b1111 : exp_adjusted[5] ? 4'b0000 : exp_adjusted[3:0];
    wire [1:0] b_mant = a_mant[9:7];

    assign b = {b_sign, b_exp, b_mant};
endmodule
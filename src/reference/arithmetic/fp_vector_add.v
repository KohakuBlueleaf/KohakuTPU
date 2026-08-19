module FracShift #(
    parameter EXP_BITS = 5,
    parameter MANT_BITS = 6,
    parameter MANT_INDEX_BITS = $clog2(MANT_BITS+1),
    parameter OFFSET = 1
) (
    input [MANT_BITS+1:0] frac,
    input [EXP_BITS-1:0] exp,
    output reg [MANT_INDEX_BITS-1:0] frac_shift,
    output reg [MANT_BITS+1:0] shifted_frac
);
    reg [MANT_INDEX_BITS-1:0] leading_zeros;
    reg [7:0] val8a;
    reg [3:0] val4a;
    reg [1:0] val2a;

    always @(*) begin
        if(MANT_BITS+1>=8) begin
            leading_zeros[3] = frac[MANT_BITS+1:MANT_BITS-6] == 0;
            val8a = leading_zeros[3] ? {frac[MANT_BITS-7:1], {15-MANT_BITS{1'b1}}} : frac[MANT_BITS+1:MANT_BITS-6];
            leading_zeros[2] = val8a[7:4] == 4'b0000;
            val4a = leading_zeros[2] ? val8a[3:0] : val8a[7:4];
            leading_zeros[1] = val4a[3:2] == 2'b00;
            val2a = leading_zeros[1] ? val4a[1:0] : val4a[3:2];
            leading_zeros[0] = ~val2a[1];
        end else if (MANT_BITS+1>=4) begin
            leading_zeros[2] = frac[MANT_BITS+1:MANT_BITS-2] == 0;
            val4a = leading_zeros[2] ? {frac[MANT_BITS-3:1], {7-MANT_BITS{1'b1}}} : frac[MANT_BITS+1:MANT_BITS-2];
            leading_zeros[1] = val4a[3:2] == 2'b00;
            val2a = leading_zeros[1] ? val4a[1:0] : val4a[3:2];
            leading_zeros[0] = ~val2a[1];
        end else if (MANT_BITS+1>=2) begin
            leading_zeros[1] = frac[MANT_BITS+1:MANT_BITS-1] == 0;
            val2a = leading_zeros[1] ? {frac[MANT_BITS-2:1], {3-MANT_BITS{1'b1}}} : frac[MANT_BITS+1:MANT_BITS-1];
            leading_zeros[0] = ~val2a[1];
        end else begin
            leading_zeros[0] = ~frac[MANT_BITS+1];
        end
        //maximumly get subnormal result
        //offset can be different since the input frac is fixed point number with unknown offset
        frac_shift = (leading_zeros > exp+(OFFSET-1) ? exp+OFFSET : leading_zeros);
        shifted_frac = frac << frac_shift;
    end
endmodule


module FPVectorAdd #(
    // 1,5,6 -> FP12
    parameter EXP_BITS = 5,
    parameter MANT_BITS = 6
) (
    input clk,
    input rst,
    input in_valid,
    input [EXP_BITS+MANT_BITS:0] a_1,
    input [EXP_BITS+MANT_BITS:0] b_1,
    input [EXP_BITS+MANT_BITS:0] c_1,
    input [EXP_BITS+MANT_BITS:0] d_1,

    input [EXP_BITS+MANT_BITS:0] a_2,
    input [EXP_BITS+MANT_BITS:0] b_2,
    input [EXP_BITS+MANT_BITS:0] c_2,
    input [EXP_BITS+MANT_BITS:0] d_2,

    output reg [EXP_BITS+MANT_BITS:0] a_out,
    output reg [EXP_BITS+MANT_BITS:0] b_out,
    output reg [EXP_BITS+MANT_BITS:0] c_out,
    output reg [EXP_BITS+MANT_BITS:0] d_out,
    output reg out_valid
);
    parameter MANT_INDEX_BITS = $clog2(MANT_BITS+1);

    wire [EXP_BITS-1:0] a_1_exp, b_1_exp, c_1_exp, d_1_exp;
    wire [EXP_BITS-1:0] a_2_exp, b_2_exp, c_2_exp, d_2_exp;

    assign a_1_exp = a_1[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign b_1_exp = b_1[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign c_1_exp = c_1[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign d_1_exp = d_1[EXP_BITS+MANT_BITS-1:MANT_BITS];

    assign a_2_exp = a_2[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign b_2_exp = b_2[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign c_2_exp = c_2[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign d_2_exp = d_2[EXP_BITS+MANT_BITS-1:MANT_BITS];

    wire signed [EXP_BITS:0] exp_diff_a = a_1_exp - a_2_exp;
    wire signed [EXP_BITS:0] exp_diff_b = b_1_exp - b_2_exp;
    wire signed [EXP_BITS:0] exp_diff_c = c_1_exp - c_2_exp;
    wire signed [EXP_BITS:0] exp_diff_d = d_1_exp - d_2_exp;

    wire swap_a = exp_diff_a[EXP_BITS];
    wire swap_b = exp_diff_b[EXP_BITS];
    wire swap_c = exp_diff_c[EXP_BITS];
    wire swap_d = exp_diff_d[EXP_BITS];

    // if swap is 1, then a_2_sign have larger exponent, so it is first operand
    // first operand have larger exponent, or at least same
    wire [EXP_BITS+MANT_BITS:0] a1, b1, c1, d1, a2, b2, c2, d2;
    wire a_sign, b_sign, c_sign, d_sign;
    wire a1_sign, b1_sign, c1_sign, d1_sign;
    wire [EXP_BITS-1:0] a1_exp, b1_exp, c1_exp, d1_exp;
    wire [MANT_BITS+1:0] a1_mant, b1_mant, c1_mant, d1_mant;
    wire a2_sign, b2_sign, c2_sign, d2_sign;
    wire [EXP_BITS-1:0] a2_exp, b2_exp, c2_exp, d2_exp;
    wire [MANT_BITS+1:0] a2_mant, b2_mant, c2_mant, d2_mant, a2_mant_temp, b2_mant_temp, c2_mant_temp, d2_mant_temp;
    assign a1 = swap_a ? a_2 : a_1;
    assign b1 = swap_b ? b_2 : b_1;
    assign c1 = swap_c ? c_2 : c_1;
    assign d1 = swap_d ? d_2 : d_1;
    assign a2 = swap_a ? a_1 : a_2;
    assign b2 = swap_b ? b_1 : b_2;
    assign c2 = swap_c ? c_1 : c_2;
    assign d2 = swap_d ? d_1 : d_2;

    assign a1_sign = a1[EXP_BITS+MANT_BITS];
    assign b1_sign = b1[EXP_BITS+MANT_BITS];
    assign c1_sign = c1[EXP_BITS+MANT_BITS];
    assign d1_sign = d1[EXP_BITS+MANT_BITS];
    assign a2_sign = a2[EXP_BITS+MANT_BITS];
    assign b2_sign = b2[EXP_BITS+MANT_BITS];
    assign c2_sign = c2[EXP_BITS+MANT_BITS];
    assign d2_sign = d2[EXP_BITS+MANT_BITS];
    assign a1_exp = a1[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign b1_exp = b1[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign c1_exp = c1[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign d1_exp = d1[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign a2_exp = a2[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign b2_exp = b2[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign c2_exp = c2[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign d2_exp = d2[EXP_BITS+MANT_BITS-1:MANT_BITS];
    assign a1_mant =      {1'b0, a1_exp>0, a1[MANT_BITS-1:0]};
    assign b1_mant =      {1'b0, b1_exp>0, b1[MANT_BITS-1:0]};
    assign c1_mant =      {1'b0, c1_exp>0, c1[MANT_BITS-1:0]};
    assign d1_mant =      {1'b0, d1_exp>0, d1[MANT_BITS-1:0]};
    assign a2_mant_temp = {1'b0, a2_exp>0, a2[MANT_BITS-1:0]};
    assign b2_mant_temp = {1'b0, b2_exp>0, b2[MANT_BITS-1:0]};
    assign c2_mant_temp = {1'b0, c2_exp>0, c2[MANT_BITS-1:0]};
    assign d2_mant_temp = {1'b0, d2_exp>0, d2[MANT_BITS-1:0]};
    assign a2_mant = a2_mant_temp << (swap_a ? -exp_diff_a : exp_diff_a);
    assign b2_mant = b2_mant_temp << (swap_b ? -exp_diff_b : exp_diff_b);
    assign c2_mant = c2_mant_temp << (swap_c ? -exp_diff_c : exp_diff_c);
    assign d2_mant = d2_mant_temp << (swap_d ? -exp_diff_d : exp_diff_d);

    // if swap happened, first operand is definitely larger than second operand
    // if exp_diff_a>0, first operand is larger than second operand
    // if first operand is negative and first operand is larger, result is negative
    // assign a_sign = ((swap_a | exp_diff_a>0) & a1_sign) | ((exp_diff_a==0) & a2_sign & (a1_mant < a2_mant));
    // assign b_sign = ((swap_b | exp_diff_b>0) & b1_sign) | ((exp_diff_b==0) & b2_sign & (b1_mant < b2_mant));
    // assign c_sign = ((swap_c | exp_diff_c>0) & c1_sign) | ((exp_diff_c==0) & c2_sign & (c1_mant < c2_mant));
    // assign d_sign = ((swap_d | exp_diff_d>0) & d1_sign) | ((exp_diff_d==0) & d2_sign & (d1_mant < d2_mant));
    assign a_sign = (exp_diff_a==0) ? ((a1_mant < a2_mant) ? a2_sign : a1_sign) : (swap_a ? a2_sign : a1_sign);
    assign b_sign = (exp_diff_b==0) ? ((b1_mant < b2_mant) ? b2_sign : b1_sign) : (swap_b ? b2_sign : b1_sign);
    assign c_sign = (exp_diff_c==0) ? ((c1_mant < c2_mant) ? c2_sign : c1_sign) : (swap_c ? c2_sign : c1_sign);
    assign d_sign = (exp_diff_d==0) ? ((d1_mant < d2_mant) ? d2_sign : d1_sign) : (swap_d ? d2_sign : d1_sign);

    wire neg_a = a1_sign != a2_sign;
    wire neg_b = b1_sign != b2_sign;
    wire neg_c = c1_sign != c2_sign;
    wire neg_d = d1_sign != d2_sign;
    wire [47:0] AB = {
        a1_mant,
        b1_mant,
        c1_mant,
        d1_mant
    };
    wire [47:0] C = {
        neg_a ? -a2_mant : a2_mant,
        neg_a ? -b2_mant : b2_mant,
        neg_a ? -c2_mant : c2_mant,
        neg_a ? -d2_mant : d2_mant
    };
    wire [47:0] P; // AB + C
    // DSP add here, will implement it latter
    DSP #(
        .INPUTREG(1),
        .OUTPUTREG(1),
        .DSPPIPEREG(1),
        .CONTROLREG(0),
        .NEEDPREADDER(0)
    ) dsp_unit (
        .clk(clk),
        .rst(rst),
        .enable(1'b1),
        .A(AB[47:18]),
        .B(AB[17:0]),
        .C(C),
        .P(P),
        .ALUMODE(4'b0000),     // Z + W + X + Y + CIN
        .INMODE(5'b00000),     // M = A * B
        .OPMODE(9'b110000011)  // X = A:B, Y = 0, W = C, Z = 0
    );

    wire [MANT_BITS+1:0] result_frac_a;
    wire [MANT_BITS+1:0] result_frac_b;
    wire [MANT_BITS+1:0] result_frac_c;
    wire [MANT_BITS+1:0] result_frac_d;
    wire [MANT_INDEX_BITS-1:0] frac_shift_a;
    wire [MANT_INDEX_BITS-1:0] frac_shift_b;
    wire [MANT_INDEX_BITS-1:0] frac_shift_c;
    wire [MANT_INDEX_BITS-1:0] frac_shift_d;
    reg [EXP_BITS-1:0] a1_exp_reg, b1_exp_reg, c1_exp_reg, d1_exp_reg;

    FracShift #(
        .EXP_BITS(EXP_BITS), 
        .MANT_BITS(MANT_BITS)
    ) frac_shift_unit_a(
        .frac(P[(MANT_BITS+2)*4-1:(MANT_BITS+2)*3]),
        .exp(a1_exp_reg),
        .frac_shift(frac_shift_a),
        .shifted_frac(result_frac_a)
    ),frac_shift_unit_b(
        .frac(P[(MANT_BITS+2)*3-1:(MANT_BITS+2)*2]),
        .exp(b1_exp_reg),
        .frac_shift(frac_shift_b),
        .shifted_frac(result_frac_b)
    ),frac_shift_unit_c(
        .frac(P[(MANT_BITS+2)*2-1:(MANT_BITS+2)*1]),
        .exp(c1_exp_reg),
        .frac_shift(frac_shift_c),
        .shifted_frac(result_frac_c)
    ),frac_shift_unit_d(
        .frac(P[(MANT_BITS+2)*1-1:(MANT_BITS+2)*0]),
        .exp(d1_exp_reg),
        .frac_shift(frac_shift_d),
        .shifted_frac(result_frac_d)
    );


    reg out_valid_reg, out_valid_reg2;
    reg [EXP_BITS:0] a_exp, b_exp, c_exp, d_exp;
    reg a_sign_reg, b_sign_reg, c_sign_reg, d_sign_reg;
    reg neg_a_reg, neg_b_reg, neg_c_reg, neg_d_reg;
    reg a_dec, b_dec, c_dec, d_dec;
    reg a_of, b_of, c_of, d_of;
    reg a_uf, b_uf, c_uf, d_uf;

    reg [EXP_BITS-1:0] a1_exp_reg_t, b1_exp_reg_t, c1_exp_reg_t, d1_exp_reg_t;
    reg a_sign_reg_t, b_sign_reg_t, c_sign_reg_t, d_sign_reg_t;
    reg neg_a_reg_t, neg_b_reg_t, neg_c_reg_t, neg_d_reg_t;

    always @(*) begin
        if(result_frac_a[MANT_BITS-1:0]==0 && neg_a_reg) begin
            a_exp = 0;
        end else begin
            a_exp = a1_exp_reg + 1'b1 - frac_shift_a;
            a_dec = frac_shift_a > 1;
            a_of = a_exp[EXP_BITS];
            a_exp = a_of ? {EXP_BITS{1'b1}} : a_exp;
        end
        
        if (result_frac_b[MANT_BITS-1:0]==0 && neg_b_reg) begin
            b_exp = 0;
        end else begin
            b_exp = b1_exp_reg + 1'b1 - frac_shift_b;
            b_dec = frac_shift_b > 1;
            b_of = b_exp[EXP_BITS];
            b_exp = b_of ? {EXP_BITS{1'b1}} : b_exp;
        end
        
        if (result_frac_c[MANT_BITS-1:0]==0 && neg_c_reg) begin
            c_exp = 0;
        end else begin
            c_exp = c1_exp_reg + 1'b1 - frac_shift_c;
            c_dec = frac_shift_c > 1;
            c_of = c_exp[EXP_BITS];
            c_exp = c_of ? {EXP_BITS{1'b1}} : c_exp;
        end
        
        if (result_frac_d[MANT_BITS-1:0]==0 && neg_d_reg) begin
            d_exp = 0;
        end else begin
            d_exp = d1_exp_reg + 1'b1 - frac_shift_d;
            d_dec = frac_shift_d > 1;
            d_of = d_exp[EXP_BITS];
            d_exp = d_of ? {EXP_BITS{1'b1}} : d_exp;
        end
    end

    always @(posedge clk) begin
        out_valid_reg <= in_valid;
        out_valid_reg2 <= out_valid_reg;
        out_valid <= out_valid_reg2;
        a1_exp_reg_t <= a1_exp; b1_exp_reg_t <= b1_exp; c1_exp_reg_t <= c1_exp; d1_exp_reg_t <= d1_exp;
        a_sign_reg_t <= a_sign; b_sign_reg_t <= b_sign; c_sign_reg_t <= c_sign; d_sign_reg_t <= d_sign;
        neg_a_reg_t <= neg_a; neg_b_reg_t <= neg_b; neg_c_reg_t <= neg_c; neg_d_reg_t <= neg_d;
        a1_exp_reg <= a1_exp_reg_t; b1_exp_reg <= b1_exp_reg_t; c1_exp_reg <= c1_exp_reg_t; d1_exp_reg <= d1_exp_reg_t;
        a_sign_reg <= a_sign_reg_t; b_sign_reg <= b_sign_reg_t; c_sign_reg <= c_sign_reg_t; d_sign_reg <= d_sign_reg_t;
        neg_a_reg <= neg_a_reg_t; neg_b_reg <= neg_b_reg_t; neg_c_reg <= neg_c_reg_t; neg_d_reg <= neg_d_reg_t;
        if (out_valid_reg) begin
            a_out <= {a_sign_reg, a_exp[EXP_BITS-1:0], result_frac_a[MANT_BITS:1]};
            b_out <= {b_sign_reg, b_exp[EXP_BITS-1:0], result_frac_b[MANT_BITS:1]};
            c_out <= {c_sign_reg, c_exp[EXP_BITS-1:0], result_frac_c[MANT_BITS:1]};
            d_out <= {d_sign_reg, d_exp[EXP_BITS-1:0], result_frac_d[MANT_BITS:1]};
        end
    end
endmodule
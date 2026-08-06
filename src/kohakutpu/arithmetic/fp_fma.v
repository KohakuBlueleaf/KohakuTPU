`timescale 1ns / 1ps


module FP16ExponentFMA(
    input clk,
    input rst,
    input ab_sign,
    input neg,
    input [4:0] a_exp,
    input [4:0] b_exp,
    input [4:0] c_exp,
    output ab_inf,
    output ab_zero,
    output [4:0] ab_exp,
    output [5:0] c_shift
);
    reg [4:0] c_exp_reg1, c_exp_reg2;
    reg possible_of;
    wire [17:0] B = 18'b010000000100000001;
    wire [29:0] A = a_exp;
    wire [26:0] D = {10'b110001000, b_exp};
    wire [47:0] C = {-{1'b0, c_exp_reg2}, 8'b0};
    wire [47:0] P;

    DSP #(
        .INPUTREG(0),
        .OUTPUTREG(0),
        .DSPPIPEREG(1),
        .CONTROLREG(0),
        .NEEDPREADDER(1)
    ) dsp_unit (
        .clk(clk),
        .rst(rst),
        .enable(1'b1),
        .A(A),
        .B(B),
        .C(C),
        .D(D),
        .P(P),
        .ALUMODE(4'b0000),     // Z + W + X + Y + CIN
        .INMODE(5'b00100),     // M = (A+D) * B
        .OPMODE(9'b110000101)  // X = M, Y = M, W = C, Z = 0
    );

    assign ab_exp = P[20:16];
    assign c_shift = P[13:8];
    assign ab_inf = P[21] & possible_of;
    assign ab_zero = P[21] & ~possible_of;

    always @(posedge clk) begin
        // c_exp_reg1 <= (c_exp==5'b00000 ? 5'b00001 : c_exp); 
        c_exp_reg1 <= c_exp;
        c_exp_reg2 <= c_exp_reg1;
        possible_of <= a_exp[4] & b_exp[4];
    end
endmodule


module FP16MantissaFMA(
    input clk,
    input rst,
    input ab_sign,
    input neg,
    input [4:0] ab_exp,
    input a_sign,
    input b_sign,
    input c_sign,
    input [4:0] a_exp,
    input [4:0] b_exp,
    input [4:0] c_exp,
    input [9:0] a_mant,
    input [9:0] b_mant,
    input [9:0] c_mant,
    input signed [5:0] c_shift,
    input ab_inf,
    input ab_zero,
    output reg out_sign,
    output reg [9:0] out,
    output reg [4:0] out_exp,
    output reg inf
);
    wire a_subnorm, b_subnorm, c_subnorm, ab_subnorm;
    reg signed [5:0] c_shift_reg1, c_shift_reg2;
    reg [4:0] c_exp_reg1, c_exp_reg2, ab_exp_reg1, ab_exp_reg2;
    reg [9:0] c_mant_reg1, c_mant_reg2;
    reg neg_reg1, neg_reg2, ab_sign_reg1, ab_sign_reg2, c_sign_reg1, c_sign_reg2;
    reg ab_inf_reg1, ab_inf_reg2, ab_zero_reg1, ab_zero_reg2;
    reg ab_subnorm_reg1, ab_subnorm_reg2, c_subnorm_reg1, c_subnorm_reg2;
    reg a_subnorm_reg1, b_subnorm_reg1, a_subnorm_reg2, b_subnorm_reg2;
    assign ab_subnorm = ab_exp == 5'b00000 | ab_zero;
    assign c_subnorm = c_exp == 5'b00000;
    assign a_subnorm = a_exp == 5'b00000;
    assign b_subnorm = b_exp == 5'b00000;

    always @(posedge clk) begin
        c_exp_reg1 <= c_exp; c_exp_reg2 <= c_exp_reg1;
        c_mant_reg1 <= c_mant; c_mant_reg2 <= c_mant_reg1;
        ab_exp_reg1 <= ab_exp; ab_exp_reg2 <= ab_exp_reg1;
        neg_reg1 <= neg; neg_reg2 <= neg_reg1;
        ab_sign_reg1 <= ab_sign; ab_sign_reg2 <= ab_sign_reg1;
        c_sign_reg1 <= c_sign; c_sign_reg2 <= c_sign_reg1;
        ab_inf_reg1 <= ab_inf; ab_inf_reg2 <= ab_inf_reg1;
        ab_zero_reg1 <= ab_zero; ab_zero_reg2 <= ab_zero_reg1;
        ab_subnorm_reg1 <= ab_subnorm; ab_subnorm_reg2 <= ab_subnorm_reg1;
        c_subnorm_reg1 <= c_subnorm; c_subnorm_reg2 <= c_subnorm_reg1;
        c_shift_reg1 <= c_shift; c_shift_reg2 <= c_shift_reg1;
        a_subnorm_reg1 <= a_subnorm; a_subnorm_reg2 <= a_subnorm_reg1;
        b_subnorm_reg1 <= b_subnorm; b_subnorm_reg2 <= b_subnorm_reg1;
    end

    wire [17:0] B = {~a_subnorm, a_mant};
    wire [29:0] A = {~b_subnorm, b_mant};
    wire [47:0] tempC = {~c_subnorm_reg1, c_mant_reg1, 10'b0};
    wire [47:0] C = (c_shift_reg1>=0 
                    ? (tempC >> c_shift_reg1) 
                    : (tempC << (-c_shift_reg1 - (ab_subnorm_reg1 & ~c_subnorm_reg1)))) + (c_shift_reg1!=0 & neg_reg1);
    wire [47:0] P;

    DSP #(
        .INPUTREG(1),
        .OUTPUTREG(0),
        .DSPPIPEREG(1),
        .CONTROLREG(1),
        .NEEDPREADDER(0)
    ) dsp_unit (
        .clk(clk),
        .rst(rst),
        .enable(1'b1),
        .A(A),
        .B(B),
        .C(C),
        .D(27'd0),
        .P(P),
        .ALUMODE({2'b00, neg_reg1, neg_reg1}),     // Z + W + X + Y + CIN
        .INMODE(5'b00000),     // M = A*B
        .OPMODE(9'b000110101)  // X = M, Y = M, W = C, Z = 0
    );

    reg [12:0] out_mant;
    reg [4:0] exp;
    wire [3:0] frac_shift;
    wire [12:0] result_frac;

    FracShift #(
        .EXP_BITS(5), 
        .MANT_BITS(11),
        .OFFSET(2)
    ) frac_shift_unit (
        .frac(out_mant),
        .exp(exp),
        .frac_shift(frac_shift),
        .shifted_frac(result_frac)
    );

    reg [47:0] P_temp;

    always @(*) begin
        P_temp = P;
        if((c_subnorm_reg2 & (~ab_subnorm_reg2 | ~neg_reg2)) 
            | (~c_subnorm_reg2 & ab_subnorm_reg2)) begin
            P_temp = P_temp << 1;
        end
        if(c_shift_reg2 > 0) begin
            out_mant = P_temp[22:10];
            if(neg_reg2) begin
                out_mant = ~out_mant;
            end
        end else if(c_shift_reg2 < 0) begin
            P_temp = P_temp >> (-c_shift_reg2);
            out_mant = P_temp[22:10];
        end else begin
            if(neg_reg2) begin
                out_mant = {1'b0, P_temp[21:10]};
            end else begin
                out_mant = P_temp[22:10];
            end
        end
        exp = c_shift_reg2 >= 0 ? ab_exp_reg2 : c_exp_reg2;
    end
    always @(*) begin
        out_sign = (c_shift_reg2==0)
                    //if no substraction, ab_sign or c_sign are same  
                    //if substraction and no overflow, c is larger than ab than c_sign is out sign
                    //if substraction and overflow, ab is larger than c than ab_sign is out sign
                    ? (neg_reg2 ? (P_temp[23] ? ab_sign_reg2 : c_sign_reg2) : c_sign_reg2) 
                    : (c_shift_reg2 > 0 ? ab_sign_reg2 : c_sign_reg2);
        if(c_shift_reg2 <= -12) begin
            out = c_mant_reg2;
            out_exp = c_exp_reg2;
        end else begin
            out_exp = exp + 2 - frac_shift - (c_subnorm_reg2 & ~(a_subnorm_reg2 || b_subnorm_reg2));
            out = result_frac[11:2];
        end

        if(ab_inf_reg1) begin
            out_sign = ab_sign_reg2;
            out = 10'b0;
            out_exp = 5'b11111;
        end 
    end
endmodule


module FP16FMA (
    input clk,
    input rst,
    input in_valid,
    input [15:0] a,
    input [15:0] b,
    input [15:0] c,
    output reg out_valid,
    output [15:0] out
);
    wire a_sign, b_sign, c_sign;
    wire [4:0] a_exp, b_exp, c_exp;
    wire [9:0] a_mant, b_mant, c_mant;
    reg a_sign_reg1, a_sign_reg2, b_sign_reg1, b_sign_reg2, c_sign_reg1, c_sign_reg2;
    reg [4:0] a_exp_reg1, a_exp_reg2;
    reg [4:0] b_exp_reg1, b_exp_reg2;
    reg [4:0] c_exp_reg1, c_exp_reg2;
    reg [9:0] a_mant_reg1, a_mant_reg2;
    reg [9:0] b_mant_reg1, b_mant_reg2;
    reg [9:0] c_mant_reg1, c_mant_reg2;

    assign a_sign = a[15];
    assign b_sign = b[15];
    assign c_sign = c[15];
    assign a_exp = a[14:10];
    assign b_exp = b[14:10];
    assign c_exp = c[14:10];
    assign a_mant = a[9:0];
    assign b_mant = b[9:0];
    assign c_mant = c[9:0];
    wire ab_sign = a_sign ^ b_sign;
    wire neg = ab_sign ^ c_sign;

    always @(posedge clk) begin
        a_sign_reg1 <= a_sign; a_sign_reg2 <= a_sign_reg1;
        b_sign_reg1 <= b_sign; b_sign_reg2 <= b_sign_reg1;
        c_sign_reg1 <= c_sign; c_sign_reg2 <= c_sign_reg1;
        a_exp_reg1 <= a_exp; a_exp_reg2 <= a_exp_reg1;
        b_exp_reg1 <= b_exp; b_exp_reg2 <= b_exp_reg1;
        c_exp_reg1 <= c_exp; c_exp_reg2 <= c_exp_reg1;
        a_mant_reg1 <= a_mant; a_mant_reg2 <= a_mant_reg1;
        b_mant_reg1 <= b_mant; b_mant_reg2 <= b_mant_reg1;
        c_mant_reg1 <= c_mant; c_mant_reg2 <= c_mant_reg1;
    end

    wire ab_inf, ab_zero;
    wire [4:0] ab_exp;
    wire signed [5:0] c_shift;
    FP16ExponentFMA fma_exp_unit(
        .clk(clk),
        .rst(rst),
        .ab_sign(ab_sign),
        .neg(neg),
        .a_exp(a_exp),
        .b_exp(b_exp),
        .c_exp(c_exp),
        .ab_exp(ab_exp),
        .c_shift(c_shift),
        .ab_inf(ab_inf),
        .ab_zero(ab_zero)
    );

    wire [9:0] out_mant;
    wire [4:0] out_exp;
    wire out_sign;
    FP16MantissaFMA fma_mant_unit(
        .clk(clk),
        .rst(rst),
        .ab_sign(a_sign_reg2 ^ b_sign_reg2),
        .neg(a_sign_reg2 ^ b_sign_reg2 ^ c_sign_reg2),
        .ab_exp(ab_exp),
        .a_sign(a_sign_reg2),
        .b_sign(b_sign_reg2),
        .c_sign(c_sign_reg2),
        .a_exp(a_exp_reg2),
        .b_exp(b_exp_reg2),
        .c_exp(c_exp_reg2),
        .a_mant(a_mant_reg2),
        .b_mant(b_mant_reg2),
        .c_mant(c_mant_reg2),
        .c_shift(c_shift),
        .ab_inf(ab_inf),
        .ab_zero(ab_zero),
        .out(out_mant),
        .out_exp(out_exp),
        .out_sign(out_sign)
    );

    assign out = {out_sign, out_exp, out_mant};

    reg out_valid_reg1, out_valid_reg2, out_valid_reg3;
    always @(posedge clk) begin
        out_valid_reg1 <= in_valid;
        out_valid_reg2 <= out_valid_reg1;
        out_valid_reg3 <= out_valid_reg2;
        out_valid <= out_valid_reg3;
    end
endmodule


module fp16_fma_synth_fake_top(
    input clk,
    input rst,
    output [7:0] led_tri_io
);
    wire clk_600m;
    wire pll_locked;

    // Instantiate the clocking wizard
    clk_wiz_0 clock_gen (
        .clk_out1   (clk_600m),
        .locked     (pll_locked),
        .clk_in1    (clk),
        .reset      (rst)
    );

    reg in_valid;
    reg [15:0] a, b, c;
    wire out_valid;
    wire [15:0] out;
    assign led_tri_io = out_valid ? out[7:0] | out[15:8] : 8'b0;

    FP16FMA fma_unit(
        .clk(clk_600m),
        .rst(rst),
        .in_valid(in_valid),
        .a(a),
        .b(b),
        .c(c),
        .out(out),
        .out_valid(out_valid)
    );

    always @(posedge clk_600m) begin
        if(rst) begin
            a <= 0;
            b <= 0;
            c <= 0;
            in_valid <= 0;
        end else begin
            in_valid <= 1;
            a <= a + 1;
            b <= b + 2;
            c <= c + 3;
        end
    end
endmodule
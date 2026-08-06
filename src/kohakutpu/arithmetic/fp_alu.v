module FP16_ALU(
    input clk,
    input rst,
    input in_valid,
    input [5:0] opmode,
    input [15:0] a,
    input [15:0] b,
    input [15:0] c,
    output reg [15:0] out,
    output reg out_valid
);
    /*
        Supported Operation:
        xx0000: a * b + c
        xx0001: a * b - c
        xx0010: 1/a * b + c
        xx0011: 1/a * b - c
        xx0100: log(a)
        xx1000: exp(a)

        00xxxx: rreturn result directly
        01xxxx: result is zero     (floating point 1.0 or 0.0 for True or False)
        10xxxx: result is positive (floating point 1.0 or 0.0 for True or False)
        11xxxx: result is negative (floating point 1.0 or 0.0 for True or False)

        First bit: invert sign of c
        Second big: use inversion of a
        Third bit: use log mode result = log(a.exp) + log(a.mant)
        Fourth bit: use exp mode result = exp(a.exp) * exp(a.mant)
    */

    wire [15:0] fp16_one = 16'b0_01111_0000000000;
    wire [15:0] fp16_zero = 16'b0_00000_0000000000;
    wire [15:0] fp16_inf = 16'b0_11111_0000000000;
    wire [15:0] fp16_nan = 16'b0_11111_1111111111;
    wire [11:0] a_12 = {a[15], a[14:10], a[9:4]};
    wire [15:0] a_inverse, a_log_exp, a_log_mant, a_partial_exp1, a_partial_exp2;

    FP12Inverse inverse_a(
        .a(a_12),
        .b(a_inverse)
    );

    FP16PartialExp exp_a(
        .a(a),
        .partial_exp1(a_partial_exp1),
        .partial_exp2(a_partial_exp2)
    );

    FP12PartialLog log_a(
        .a(a_12),
        .exp_log(a_log_exp),
        .mant_log(a_log_mant)
    );

    reg fma_in_valid;
    reg [15:0] fma_a, fma_b, fma_c;
    reg [3:0] post_opmode_reg1, post_opmode_reg2, post_opmode_reg3, post_opmode_reg4;
    reg a_neg1, a_neg2, a_neg3, a_neg4;
    reg a_nan1, a_nan2, a_nan3, a_nan4;
    reg b_nan1, b_nan2, b_nan3, b_nan4;
    reg c_nan1, c_nan2, c_nan3, c_nan4;
    reg a_inf1, a_inf2, a_inf3, a_inf4;
    reg b_inf1, b_inf2, b_inf3, b_inf4;
    reg c_inf1, c_inf2, c_inf3, c_inf4;
    reg a_zero1, a_zero2, a_zero3, a_zero4;
    reg b_zero1, b_zero2, b_zero3, b_zero4;
    reg c_zero1, c_zero2, c_zero3, c_zero4;

    wire a_inf = fma_a[14:10] == 5'b11111;
    wire b_inf = fma_b[14:10] == 5'b11111;
    wire c_inf = fma_c[14:10] == 5'b11111;
    wire a_zero = fma_a[14:0] == 0;
    wire b_zero = fma_b[14:0] == 0;
    wire c_zero = fma_c[14:0] == 0;
    wire a_nan = a_inf && fma_a[9:0] != 0;
    wire b_nan = b_inf && fma_b[9:0] != 0;
    wire c_nan = c_inf && fma_c[9:0] != 0;

    wire final_nan = a_nan4 || b_nan4 || c_nan4 
                    || (a_inf4 && b_zero4) || (a_zero4 && b_inf4) // 0 * inf 
                    || (post_opmode_reg4[0] && a_neg4); //log(-x)
    wire final_inf = a_inf4 || b_inf4 || c_inf4;

    always @(clk) begin
        fma_in_valid <= in_valid;
        post_opmode_reg1 <= opmode[5:2];
        post_opmode_reg2 <= post_opmode_reg1;
        post_opmode_reg3 <= post_opmode_reg2;
        post_opmode_reg4 <= post_opmode_reg3;
        a_neg1 <= a[15]; a_neg2 <= a_neg1; a_neg3 <= a_neg2; a_neg4 <= a_neg3;
        a_inf1 <= a_inf; a_inf2 <= a_inf1; a_inf3 <= a_inf2; a_inf4 <= a_inf3;
        b_inf1 <= b_inf; b_inf2 <= b_inf1; b_inf3 <= b_inf2; b_inf4 <= b_inf3;
        c_inf1 <= c_inf; c_inf2 <= c_inf1; c_inf3 <= c_inf2; c_inf4 <= c_inf3;
        a_zero1 <= a_zero; a_zero2 <= a_zero1; a_zero3 <= a_zero2; a_zero4 <= a_zero3;
        b_zero1 <= b_zero; b_zero2 <= b_zero1; b_zero3 <= b_zero2; b_zero4 <= b_zero3;
        c_zero1 <= c_zero; c_zero2 <= c_zero1; c_zero3 <= c_zero2; c_zero4 <= c_zero3;
        a_nan1 <= a_nan; a_nan2 <= a_nan1; a_nan3 <= a_nan2; a_nan4 <= a_nan3;
        b_nan1 <= b_nan; b_nan2 <= b_nan1; b_nan3 <= b_nan2; b_nan4 <= b_nan3;
        c_nan1 <= c_nan; c_nan2 <= c_nan1; c_nan3 <= c_nan2; c_nan4 <= c_nan3;

        if (opmode[3]) begin
            fma_a <= a_partial_exp1;
            fma_b <= a_partial_exp2;
            fma_c <= fp16_zero;
        end else begin
            fma_a <= opmode[2] ? a_log_exp : opmode[1] ? a_inverse : a;
            fma_b <= opmode[2] ? fp16_one : b;
            fma_c <= opmode[2] ? a_log_mant : {c[15]^opmode[0], c[14:0]};
        end
    end

    wire [15:0] fma_out;
    wire fma_valid;
    FP16FMA fma_unit(
        .clk(clk),
        .rst(rst),
        .in_valid(fma_in_valid),
        .a(fma_a),
        .b(fma_b),
        .c(fma_c),
        .out_valid(fma_valid),
        .out(fma_out)
    );

    always @(*) begin
        case ({final_inf, final_nan})
            2'b00: out = fma_out;
            2'b01: out = fp16_nan;
            2'b10: out = fp16_inf;
            2'b11: out = fp16_nan;
        endcase
        case (post_opmode_reg4[3:2])
            2'b00: out = out;
            2'b01: out = (out == fp16_zero) ? fp16_zero : fp16_one;
            2'b10: out = (out[15] == 0) ? fp16_one : fp16_zero;
            2'b11: out = (out[15] == 1) ? fp16_one : fp16_zero;
        endcase
    end

    always @(posedge clk) begin
        out_valid <= fma_valid;
    end
endmodule


module FP16ALUArray(
    input clk,
    input rst,
    input in_valid,
    input [5:0] opmode,
    input [0:15][15:0] a,
    input [0:15][15:0] b,
    input [0:15][15:0] c,
    output [0:15][15:0] out,
    output out_valid
);
    genvar i;
    wire [15:0] total_out_valid;
    assign out_valid = total_out_valid[0];
    generate
        for(i = 0; i < 16; i = i + 1) begin: FP16_ALU
            FP16_ALU alu(
                .clk(clk),
                .rst(rst),
                .in_valid(in_valid),
                .opmode(opmode),
                .a(a[i]),
                .b(b[i]),
                .c(c[i]),
                .out(out[i]),
                .out_valid(total_out_valid[i])
            );
        end
    endgenerate
endmodule
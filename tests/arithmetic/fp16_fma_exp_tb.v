`timescale 1ns / 1ps

module FP16ExponentFMA_tb();
    reg clk;
    reg rst;
    reg [4:0] a_exp;
    reg [4:0] b_exp;
    reg [4:0] c_exp;
    wire ab_inf;
    wire ab_zero;
    wire signed [5:0] ab_exp;
    wire signed [5:0] c_shift;

    FP16ExponentFMA fma_unit(
        .clk(clk),
        .rst(rst),
        .a_exp(a_exp),
        .b_exp(b_exp),
        .c_exp(c_exp),
        .ab_exp(ab_exp),
        .c_shift(c_shift),
        .ab_inf(ab_inf),
        .ab_zero(ab_zero)
    );

    initial begin
        clk = 0;
        forever #5 clk = ~clk;
    end

    initial begin
        rst = 1;
        #10 rst = 0;
        a_exp = 5'b00011;
        b_exp = 5'b00001;
        c_exp = 5'b00000;
        #10
        a_exp = 5'b10000;
        b_exp = 5'b01111;
        c_exp = 5'b10000;
        #10
        a_exp = 5'b10001;
        b_exp = 5'b11110;
        c_exp = 5'b10000;
        #30
        $finish;
    end

    initial begin
        $monitor(
            "T=%0d, a_exp=%d, b_exp=%d, c_exp=%d, ab_exp=%d, c_shift=%d || ab_inf=%b, ab_zero=%b",
            $time, a_exp, b_exp, c_exp, ab_exp, c_shift, ab_inf, ab_zero
        );
    end

    initial begin
        $dumpfile("fp16_fma_exp_tb.vcd");
        $dumpvars(0, FP16ExponentFMA_tb);
    end
endmodule
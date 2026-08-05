`timescale 1ns / 1ps

module FP16Exponent_tb();
    reg clk;
    reg rst;
    reg in_valid;
    reg [15:0] a;
    reg [15:0] b;
    reg [15:0] c;
    wire out_valid;
    wire [63:0] a_real, b_real, c_real, out_real;
    wire signed [15:0] out;
    float_display #("", 5, 10) fd1 (
        .float_num(a), .decoded(a_real)
    );
    float_display #("", 5, 10) fd2 (
        .float_num(b), .decoded(b_real)
    );
    float_display #("", 5, 10) fd3 (
        .float_num(c), .decoded(c_real)
    );
    float_display #("", 5, 10) fd4 (
        .float_num(out), .decoded(out_real)
    );

    FP16FMA fma_unit(
        .clk(clk),
        .rst(rst),
        .in_valid(in_valid),
        .a(a),
        .b(b),
        .c(c),
        .out(out),
        .out_valid(out_valid)
    );

    initial begin
        in_valid = 0;
        a = 0;
        b = 0;
        c = 0;
    end

    initial begin
        clk = 0;
        forever #5 clk = ~clk;
    end

    reg [3:0] i;
    initial begin
        rst = 1;
        #10 rst = 0;

        // a*b > c (exponent)
        $display("a*b > c (exponent)");
        a = 16'b0_10001_1110000000;
        b = 16'b0_01111_1000000000;
        c = 16'b0_10000_1111000000;
        for (i = 0; i < 8; i = i + 1) begin
            in_valid = 1;
            a[15] = i[0];
            b[15] = i[1];
            c[15] = i[2];
            #10
            in_valid = 0;
            #40
            $display(
                "T=%04d || a=%14.10f, b=%14.10f, c=%14.10f || out: %14.10f || ground truth: %14.10f",
                $time, $bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), $bitstoreal(out_real),
                $bitstoreal(a_real) * $bitstoreal(b_real) + $bitstoreal(c_real)
            );
        end

        // a*b < c (exponent)
        $display("a*b < c (exponent)");
        a = 16'b0_10000_1110000000;
        b = 16'b0_01111_1000000000;
        c = 16'b0_10010_1111000000;
        for (i = 0; i < 8; i = i + 1) begin
            in_valid = 1;
            a[15] = i[0];
            b[15] = i[1];
            c[15] = i[2];
            #10
            in_valid = 0;
            #40
            $display(
                "T=%04d || a=%14.10f, b=%14.10f, c=%14.10f || out: %14.10f || ground truth: %14.10f",
                $time, $bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), $bitstoreal(out_real),
                $bitstoreal(a_real) * $bitstoreal(b_real) + $bitstoreal(c_real)
            );
        end

        // a*b == c (exponent)
        $display("a*b == c (exponent)");
        a = 16'b0_10000_1110000000;
        b = 16'b0_01111_1000000000;
        c = 16'b0_10001_1111000000;
        for (i = 0; i < 8; i = i + 1) begin
            in_valid = 1;
            a[15] = i[0];
            b[15] = i[1];
            c[15] = i[2];
            #10
            in_valid = 0;
            #40
            $display(
                "T=%04d || a=%14.10f, b=%14.10f, c=%14.10f || out: %14.10f || ground truth: %14.10f",
                $time, $bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), $bitstoreal(out_real),
                $bitstoreal(a_real) * $bitstoreal(b_real) + $bitstoreal(c_real)
            );
        end

        // a*b is subnorm, c is not
        $display("a*b is subnorm, c is not");
        a = 16'b0_00000_0011000000;
        b = 16'b0_01111_1010000000;
        c = 16'b0_00001_1011010000;
        for (i = 0; i < 8; i = i + 1) begin
            in_valid = 1;
            a[15] = i[0];
            b[15] = i[1];
            c[15] = i[2];
            #10
            in_valid = 0;
            #40
            $display(
                "T=%04d || a=%14.10f, b=%14.10f, c=%14.10f || out: %14.10f || ground truth: %14.10f",
                $time, $bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), $bitstoreal(out_real),
                $bitstoreal(a_real) * $bitstoreal(b_real) + $bitstoreal(c_real)
            );
        end

        // c is subnorm, a*b is not
        $display("c is subnorm, a*b is not");
        a = 16'b0_00000_1101010000;
        b = 16'b0_10000_1001011100;
        c = 16'b0_00000_0111001010;
        for (i = 0; i < 8; i = i + 1) begin
            in_valid = 1;
            a[15] = i[0];
            b[15] = i[1];
            c[15] = i[2];
            #10
            in_valid = 0;
            #40
            $display(
                "T=%04d || a=%14.10f, b=%14.10f, c=%14.10f || out: %14.10f || ground truth: %14.10f",
                $time, $bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), $bitstoreal(out_real),
                $bitstoreal(a_real) * $bitstoreal(b_real) + $bitstoreal(c_real)
            );
        end

        // c and a*b are subnorm
        $display("c and a*b are subnorm");
        a = 16'b0_00000_0101100000;
        b = 16'b0_01110_1010001010;
        c = 16'b0_00000_1011101010;
        for (i = 0; i < 8; i = i + 1) begin
            in_valid = 1;
            a[15] = i[0];
            b[15] = i[1];
            c[15] = i[2];
            #10
            in_valid = 0;
            #40
            $display(
                "T=%04d || a=%14.10f, b=%14.10f, c=%14.10f || out: %14.10f || ground truth: %14.10f",
                $time, $bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), $bitstoreal(out_real),
                $bitstoreal(a_real) * $bitstoreal(b_real) + $bitstoreal(c_real)
            );
        end
        $finish;
    end

    // initial begin
    //     $monitor(
    //         "T=%0d, a=%f, b=%f, c=%f, out=%f",
    //         $time, $bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), $bitstoreal(out_real)
    //     );
    // end

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, FP16Exponent_tb);
    end
endmodule
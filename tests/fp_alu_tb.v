`timescale 1ns / 1ps

module FP16Exponent_tb();
    reg clk;
    reg rst;
    reg in_valid;
    reg [15:0] a;
    reg [15:0] b;
    reg [15:0] c;
    reg [5:0] opmode;
    reg [5:0] prev_opmode;
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

    FP16_ALU fma_unit(
        .clk(clk),
        .rst(rst),
        .in_valid(in_valid),
        .opmode(opmode),
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

    real e = 2.7182818285;
    function real alu_result;
        input real a, b, c;
        input [5:0] opmode;
        begin
            case (opmode[3:0])
                6'b0000: alu_result = a * b + c;
                6'b0001: alu_result = a * b - c;
                6'b0010: alu_result = (1/a) * b + c;
                6'b0011: alu_result = (1/a) * b - c;
                6'b0100: alu_result = $ln(a);
                6'b1000: alu_result = e**a;
                default: alu_result = 0;
            endcase
            case (opmode[5:4])
                2'b00: alu_result = alu_result;
                2'b01: alu_result = alu_result == 0 ? 1 : 0;
                2'b10: alu_result = alu_result > 0 ? 1 : 0;
                2'b11: alu_result = alu_result < 0 ? 1 : 0;
            endcase
        end
    endfunction

    reg [3:0] i;
    integer opmode_loop;
    initial begin
        rst = 1;
        #10 rst = 0;
        for(opmode_loop = 0; opmode_loop < 16; opmode_loop = opmode_loop + 1) begin: loop
            opmode = opmode_loop;
            if (opmode[3:2] != 2'b00) begin
                opmode[1:0] = 2'b00;
            end
            if (opmode == prev_opmode || opmode[3:2] == 2'b11) begin
                disable loop;
            end
            $display("opmode: %b", opmode);
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
                wait(out_valid); @ (posedge clk);
                $display(
                    "T=%04d || a=%14.10f, b=%14.10f, c=%14.10f || out: %14.10f || ground truth: %14.10f",
                    $time, $bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), $bitstoreal(out_real),
                    alu_result($bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), opmode)
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
                wait(out_valid); @ (posedge clk);
                $display(
                    "T=%04d || a=%14.10f, b=%14.10f, c=%14.10f || out: %14.10f || ground truth: %14.10f",
                    $time, $bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), $bitstoreal(out_real),
                    alu_result($bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), opmode)
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
                wait(out_valid); @ (posedge clk);
                $display(
                    "T=%04d || a=%14.10f, b=%14.10f, c=%14.10f || out: %14.10f || ground truth: %14.10f",
                    $time, $bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), $bitstoreal(out_real),
                    alu_result($bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), opmode)
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
                wait(out_valid); @ (posedge clk);
                $display(
                    "T=%04d || a=%14.10f, b=%14.10f, c=%14.10f || out: %14.10f || ground truth: %14.10f",
                    $time, $bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), $bitstoreal(out_real),
                    alu_result($bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), opmode)
                );
            end

            // c is subnorm, a*b is not
            $display("c is subnorm, a*b is not");
            a = 16'b0_00010_1101010000;
            // b = 16'b0_10000_1001011100;
            b = 16'b0_01111_0000000000;
            c = 16'b0_00000_0000000001;
            for (i = 0; i < 8; i = i + 1) begin
                in_valid = 1;
                a[15] = i[0];
                b[15] = i[1];
                c[15] = i[2];
                #10
                in_valid = 0;
                wait(out_valid); @ (posedge clk);
                $display(
                    "T=%04d || a=%14.10f, b=%14.10f, c=%14.10f || out: %14.10f || ground truth: %14.10f",
                    $time, $bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), $bitstoreal(out_real),
                    alu_result($bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), opmode)
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
                wait(out_valid); @ (posedge clk);
                $display(
                    "T=%04d || a=%14.10f, b=%14.10f, c=%14.10f || out: %14.10f || ground truth: %14.10f",
                    $time, $bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), $bitstoreal(out_real),
                    alu_result($bitstoreal(a_real), $bitstoreal(b_real), $bitstoreal(c_real), opmode)
                );
            end
            prev_opmode = opmode;
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
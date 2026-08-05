// test_fp8.v
`timescale 1ns / 1ps


module FP12Inverse_tb;
    reg [11:0] a;
    wire [15:0] ainv;
    real ainv_real;
    wire [63:0] a_real, ainv_out;

    FP12Inverse fp12inv (
        .a(a),
        .b(ainv)
    );
    float_display #("a", 5, 6) fd (
        .float_num(a), .decoded(a_real)
    );
    float_display #("1/a", 5, 10) fd2 (
        .float_num(ainv), .decoded(ainv_out)
    );

    always @(ainv_out) begin
        ainv_real = 1.0 / $bitstoreal(a_real);
        $display("Real: %g", ainv_real);
        $display("APE: %g%%", $abs($bitstoreal(ainv_out)-ainv_real)/ainv_real*100);
    end

    //main
    initial begin
        for (integer i = 0; i < 1 << 11; i = i + 1) begin
            a = i;
            #10; $display("--------------------");
        end
        $finish;
    end

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, FP12Inverse_tb);
    end

endmodule
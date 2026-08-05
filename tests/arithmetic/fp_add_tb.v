// test_fp8.v
`timescale 1ns / 1ps


module FP16Adder_tb;
    parameter EXP_BITS = 5;
    parameter MANT_BITS = 6;
    reg clk, rst;
    reg in_valid;
    wire out_valid;
    reg [31:0] id;
    reg [31:0] id_out;
    reg [EXP_BITS+MANT_BITS:0] a;
    reg [EXP_BITS+MANT_BITS:0] b;
    reg [EXP_BITS+MANT_BITS:0] c;
    reg [EXP_BITS+MANT_BITS:0] d;
    reg [EXP_BITS+MANT_BITS:0] na;
    reg [EXP_BITS+MANT_BITS:0] nb;
    reg [EXP_BITS+MANT_BITS:0] nc;
    reg [EXP_BITS+MANT_BITS:0] nd;
    reg  [EXP_BITS+MANT_BITS:0] aa;
    reg  [EXP_BITS+MANT_BITS:0] bb;
    reg  [EXP_BITS+MANT_BITS:0] cc;
    reg  [EXP_BITS+MANT_BITS:0] dd;
    wire [EXP_BITS+MANT_BITS:0] aa_out;
    wire [EXP_BITS+MANT_BITS:0] bb_out;
    wire [EXP_BITS+MANT_BITS:0] cc_out;
    wire [EXP_BITS+MANT_BITS:0] dd_out;

    always @(*) begin
        na = {~a[EXP_BITS+MANT_BITS], a[EXP_BITS+MANT_BITS-1:0]};
        nb = {~b[EXP_BITS+MANT_BITS], b[EXP_BITS+MANT_BITS-1:0]};
        nc = {~c[EXP_BITS+MANT_BITS], c[EXP_BITS+MANT_BITS-1:0]};
        nd = {~d[EXP_BITS+MANT_BITS], d[EXP_BITS+MANT_BITS-1:0]};
    end

    always @(posedge clk) begin
        if(out_valid) begin
            aa <= aa_out;
            bb <= bb_out;
            cc <= cc_out;
            dd <= dd_out;
            id_out <= id_out + 1;
        end else begin
            id_out <= id_out;
            aa <= aa;
            bb <= bb;
            cc <= cc;
            dd <= dd;
        end
    end

    FPVectorAdd #(
        .EXP_BITS(EXP_BITS),
        .MANT_BITS(MANT_BITS)
    ) adder (
        .clk(clk),
        .rst(rst),
        .a_1(a),
        .b_1(b),
        .c_1(c),
        .d_1(d),
        // .a_2(na),
        // .b_2(nb),
        // .c_2(nc),
        // .d_2(nd),
        .a_2(a),
        .b_2(b),
        .c_2(c),
        .d_2(12'b0),
        .a_out(aa_out),
        .b_out(bb_out),
        .c_out(cc_out),
        .d_out(dd_out),
        .in_valid(in_valid),
        .out_valid(out_valid)
    );

    //clock
    initial begin
        clk = 0;
        forever #5 clk = ~clk;
    end

    //setup
    initial begin
        in_valid = 0;
        id_out = 0;
        id = 0;
        a = 0;
        b = 0;
        c = 0;
        d = 0;
        aa = 0;
        bb = 0;
        cc = 0;
        dd = 0;
    end

    //main
    initial begin
        rst = 1;
        #10
        rst = 0;
        in_valid = 1;
        // #10
        id = 1;
        a = 16'b0_10000_100100; //3.14
        b = 16'b0_01111_110100; //1.821
        c = 16'b1_01111_110100; //-1.821
        d = 16'b1_10001_010100; //-5.282
        #10;
        id = 2;
        a = 16'b0;
        b = 16'b0;
        c = 16'b0;
        d = 16'b0;
        #10
        in_valid = 0;
        #30;
        $finish;
    end

    initial begin
        $monitor(
            "Time = %f, rst=%b, id=%d, AA = %b, BB = %b, CC = %b , DD = %b", 
            $time, rst, id_out, aa, bb, cc, dd
        );
        $dumpfile("dump.vcd");
        $dumpvars(0, FP16Adder_tb);
    end

endmodule
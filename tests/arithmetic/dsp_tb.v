// DSP48E2_example_tb.v
`timescale 1ns / 1ps

module DSP48E2_example_tb;
  reg clk;
  reg rst;
  reg signed [47:0] AB;
  reg signed [47:0] C;
  wire signed [47:0] P;

  DSP #(
    .INPUTREG(1),
    .OUTPUTREG(1),
    .DSPPIPEREG(1),
    .CONTROLREG(0),
    .NEEDPREADDER(0)
  ) Expodsp(
    .enable(1'b1),
    .clk(clk),
    .rst(rst),
    .A(AB[47:18]),
    .B(AB[17:0]),
    .C(C),
    .ALUMODE(4'b0000),     // Z + W + X + Y + CIN
    .INMODE(5'b00000),     // M = A * B
    .OPMODE(9'b110000011), // X = A:B, Y = 0, W = C, Z = 0
    .P(P)
  );

  initial begin
    clk = 0;
    forever #5 clk = ~clk;
  end

  initial begin
    rst = 1;
    #10;
    rst = 0;
  end

  initial begin
    AB = 48'd0;
    C = 48'd0;
    #20;
    AB = 48'd1;
    C = 48'd2;
    #20;
    AB = 48'd3;
    C = 48'd4;
    #20;
    $finish;
  end

  initial begin
    $monitor(
      "Time = %0d, AB = %0d, C = %0d, P = %0d", 
      $time, AB, C, P
    );
    // $dumpfile("dump.vcd");
    $dumpvars(0, DSP48E2_example_tb);
  end

endmodule
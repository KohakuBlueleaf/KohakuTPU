// DSP48E2_example_tb.v
`timescale 1ns / 1ps

module LUT6_example_tb;
  reg [5:0] inp;
  wire out;
  wire out2, out3;
  wire [3:0] out4;

  LUT6 #(
    .INIT(64'b010101010101010101010101010101010101010101010101010101010101010)
  ) lut (
    .O(out),
    .I0(inp[0]),
    .I1(inp[1]),
    .I2(inp[2]),
    .I3(inp[3]),
    .I4(inp[4]),
    .I5(inp[5])
  );

  LUT6_2 #(
    .INIT(64'b1100110011001100110011001100110010101010101010101010101010101010)
  ) lut2 (
    .O5(out2),
    .O6(out3),
    .I0(inp[0]),
    .I1(inp[1]),
    .I2(inp[2]),
    .I3(inp[3]),
    .I4(inp[4]),
    .I5(1'b1)
  );


  MultiBitLut #(
    .input_bits(5),
    .output_bits(4),
    .INIT({
        32'b11001100110011001100110011001100,
        32'b10101010101010101010101010101010,
        32'b11001100110011001100110011001100,
        32'b10101010101010101010101010101010
    })
  ) mlut (
    .in(inp[4:0]),
    .out(out4)
  );

  initial begin
    inp = 6'b000000;
    #10 inp = 6'b000001;
    #10 inp = 6'b000010;
    #10 inp = 6'b000011;
    #10 inp = 6'b000100;
    #10 inp = 6'b000101;
    #10 inp = 6'b000110;
  end

  initial begin
    $monitor(
      "Time = %0d, inp= %0d, out = %0d, out2 = %0d, out3 = %0d, out4 = %b", 
      $time, inp, out, out2, out3, out4
    );
    // $dumpfile("dump.vcd");
    $dumpvars(0, LUT6_example_tb);
  end

endmodule
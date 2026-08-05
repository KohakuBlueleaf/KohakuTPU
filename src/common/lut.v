module MultiBitLut #(
    parameter input_bits = 6,
    parameter output_bits = 10,
    parameter [64*output_bits/(7-input_bits)-1:0] INIT = 0
) (
    input [input_bits-1:0] in,
    output [output_bits-1:0] out
);
    genvar i;
    generate
        for(i=0; i<output_bits/(7-input_bits); i=i+1) begin
            if(input_bits == 5) begin
                LUT6_2 #(
                    .INIT(INIT[i*64+63:i*64])
                ) lut (
                    .I0(in[0]),
                    .I1(in[1]),
                    .I2(in[2]),
                    .I3(in[3]),
                    .I4(in[4]),
                    .I5(1'b1),
                    .O5(out[i*2]),
                    .O6(out[i*2+1])
                );
            end
            else if(input_bits == 6) begin
                LUT6 #(
                    .INIT(INIT[i*64+63:i*64])
                ) lut (
                    .I0(in[0]),
                    .I1(in[1]),
                    .I2(in[2]),
                    .I3(in[3]),
                    .I4(in[4]),
                    .I5(in[5]),
                    .O(out[i])
                );
            end
        end
    endgenerate
endmodule
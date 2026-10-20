// 16 FP16 -> 16 E8M15, exact (every FP16 is an E8M15), one word a cycle,
// registered. IEEE = 0 reads the clusters' accumulator form (sru_f16e8.vh).

`default_nettype none

module sru_unpack #(
    parameter integer IEEE = 1
)(
    input  wire         clk,
    input  wire [255:0] in_data,
    output reg  [383:0] out_data
);
    `include "sru_f16e8.vh"

    integer j;
    always @(posedge clk) begin
        for (j = 0; j < 16; j = j + 1) begin
            out_data[j*24 +: 24] <= f16e8k(in_data[j*16 +: 16], IEEE);
        end
    end
endmodule

`default_nettype wire

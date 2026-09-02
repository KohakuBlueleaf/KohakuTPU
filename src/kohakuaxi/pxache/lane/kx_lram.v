// kx_lram -- a simple-dual-port LUTRAM with one registered read, inferred, no
// wrapper. At 515 x 256 it is the array (2,060 LUT at 64 bits a LUT) plus its
// 4:1 read mux (515), and nothing else.

`default_nettype none

module kx_lram #(
    parameter integer WIDTH = 64,
    parameter integer DEPTH = 256
)(
    input  wire                     clk,
    input  wire                     wr_en,
    input  wire [$clog2(DEPTH)-1:0] wr_addr,
    input  wire [WIDTH-1:0]         wr_data,
    input  wire                     rd_en,
    input  wire [$clog2(DEPTH)-1:0] rd_addr,
    output reg  [WIDTH-1:0]         rd_data
);
    (* ram_style = "distributed" *) reg [WIDTH-1:0] mem [0:DEPTH-1];
    always @(posedge clk) begin
        if (wr_en) begin mem[wr_addr] <= wr_data; end
        if (rd_en) begin rd_data <= mem[rd_addr]; end
    end
endmodule

`default_nettype wire

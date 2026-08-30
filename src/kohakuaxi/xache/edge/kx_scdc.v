// Stream clock-domain crossing at a Xache edge: one valid/ready beat of WIDTH bits,
// wr_clk to rd_clk, over async_fifo (FWFT). Instantiated ONLY where a port's clock
// differs from the fabric clock; shared-clock ports never elaborate it.

`default_nettype none

module kx_scdc #(
    parameter integer WIDTH = 64,
    parameter integer DEPTH = 16,              // power of two, >= 16 (xpm minimum)
    parameter         MEM   = "distributed"
)(
    input  wire              wr_clk,
    input  wire              wr_rst,           // active high, wr_clk domain
    input  wire              s_valid,
    output wire              s_ready,
    input  wire [WIDTH-1:0]  s_data,

    input  wire              rd_clk,
    output wire              m_valid,
    input  wire              m_ready,
    output wire [WIDTH-1:0]  m_data
);
    wire full, empty;
    assign s_ready = !full;
    assign m_valid = !empty;

    async_fifo #(.DATA_WIDTH(WIDTH), .FIFO_DEPTH(DEPTH), .MEMORY_TYPE(MEM)) u_fifo (
        .wr_clk(wr_clk), .wr_rst(wr_rst), .wr_en(s_valid && !full), .wr_data(s_data),
        .wr_full(full),
        .rd_clk(rd_clk), .rd_en(m_valid && m_ready), .rd_data(m_data), .rd_empty(empty)
    );
endmodule

`default_nettype wire

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

    generate if (MEM == "distributed") begin : g_lean
        // The read side's reset is the write side's, landed through two flops
        // -- the same derivation xpm_fifo_async makes inside itself.
        reg rd_r1, rd_r2;
        always @(posedge rd_clk) begin
            rd_r1 <= !wr_rst;
            rd_r2 <= rd_r1;
        end
        kohaku_aring #(.WIDTH(WIDTH), .DEPTH(DEPTH), .FULL(1)) u_r (
            .wr_clk(wr_clk), .wr_rstn(!wr_rst), .wr_en(s_valid && !full),
            .wr_data(s_data), .wr_busy(full),
            .clk(rd_clk), .rstn(rd_r2), .rd_en(m_valid && m_ready),
            .rd_data(m_data), .rd_busy(empty));
    end else begin : g_xpm
        async_fifo #(.DATA_WIDTH(WIDTH), .FIFO_DEPTH(DEPTH), .MEMORY_TYPE(MEM)) u_fifo (
            .wr_clk(wr_clk), .wr_rst(wr_rst), .wr_en(s_valid && !full), .wr_data(s_data),
            .wr_full(full),
            .rd_clk(rd_clk), .rd_en(m_valid && m_ready), .rd_data(m_data), .rd_empty(empty)
        );
    end endgenerate
endmodule

`default_nettype wire

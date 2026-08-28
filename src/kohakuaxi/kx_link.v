// One internal fabric link, resolved at elaboration: SAME=1 (the two ends share a
// clock) is a bare combinational passthrough that costs nothing; SAME=0 crosses
// clock domains through kx_scdc. This is where "a crossing only where clocks
// differ" is enforced -- kx_mempath instantiates one per (master,home) path with
// SAME = (MCLK[m] == HCLK[h]).

`default_nettype none

module kx_link #(
    parameter integer WIDTH = 64,
    parameter integer SAME  = 1,
    parameter integer DEPTH = 16,
    parameter         MEM   = "distributed"
)(
    input  wire              wr_clk,
    input  wire              wr_rst,
    input  wire              s_valid,
    output wire              s_ready,
    input  wire [WIDTH-1:0]  s_data,

    input  wire              rd_clk,
    output wire              m_valid,
    input  wire              m_ready,
    output wire [WIDTH-1:0]  m_data
);
    generate
    if (SAME) begin : g_direct
        assign m_valid = s_valid;
        assign s_ready = m_ready;
        assign m_data  = s_data;
    end else begin : g_cdc
        kx_scdc #(.WIDTH(WIDTH), .DEPTH(DEPTH), .MEM(MEM)) u_cdc (
            .wr_clk(wr_clk), .wr_rst(wr_rst),
            .s_valid(s_valid), .s_ready(s_ready), .s_data(s_data),
            .rd_clk(rd_clk), .m_valid(m_valid), .m_ready(m_ready), .m_data(m_data)
        );
    end
    endgenerate
endmodule

`default_nettype wire

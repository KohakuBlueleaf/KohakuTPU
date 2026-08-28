// One AXI4 master, its own clock, crossed to the fabric clock -- five async FIFOs,
// one per channel. Sits in front of the concentrator so the shared core sees a
// single-clock merged stream. This is the per-master crossing that does NOT
// collapse when ports fuse (masters are on different clocks), so it is the honest
// cost the multi-clock fused engine still pays M times.

`default_nettype none

module sb_axi_cdc #(
    parameter integer DW   = 512,
    parameter integer AW   = 40,
    parameter integer IDW  = 4,
    parameter integer DEPTH = 16,
    parameter         MEM  = "distributed",
    localparam integer AWW = IDW + AW + 8 + 3 + 2,
    localparam integer WW  = DW + DW/8 + 1,
    localparam integer BW  = IDW + 2,
    localparam integer ARW = IDW + AW + 8 + 3 + 2,
    localparam integer RW  = IDW + DW + 2 + 1
)(
    // ---- slave side: the external master's clock ---------------------------
    input  wire                s_aclk,
    input  wire                s_arst,
    input  wire [IDW-1:0]      s_awid,
    input  wire [AW-1:0]       s_awaddr,
    input  wire [7:0]          s_awlen,
    input  wire [2:0]          s_awsize,
    input  wire [1:0]          s_awburst,
    input  wire                s_awvalid,
    output wire                s_awready,
    input  wire [DW-1:0]       s_wdata,
    input  wire [DW/8-1:0]     s_wstrb,
    input  wire                s_wlast,
    input  wire                s_wvalid,
    output wire                s_wready,
    output wire [IDW-1:0]      s_bid,
    output wire [1:0]          s_bresp,
    output wire                s_bvalid,
    input  wire                s_bready,
    input  wire [IDW-1:0]      s_arid,
    input  wire [AW-1:0]       s_araddr,
    input  wire [7:0]          s_arlen,
    input  wire [2:0]          s_arsize,
    input  wire [1:0]          s_arburst,
    input  wire                s_arvalid,
    output wire                s_arready,
    output wire [IDW-1:0]      s_rid,
    output wire [DW-1:0]       s_rdata,
    output wire [1:0]          s_rresp,
    output wire                s_rlast,
    output wire                s_rvalid,
    input  wire                s_rready,
    // ---- master side: the fabric clock -------------------------------------
    input  wire                m_aclk,
    input  wire                m_arst,
    output wire [IDW-1:0]      m_awid,
    output wire [AW-1:0]       m_awaddr,
    output wire [7:0]          m_awlen,
    output wire [2:0]          m_awsize,
    output wire [1:0]          m_awburst,
    output wire                m_awvalid,
    input  wire                m_awready,
    output wire [DW-1:0]       m_wdata,
    output wire [DW/8-1:0]     m_wstrb,
    output wire                m_wlast,
    output wire                m_wvalid,
    input  wire                m_wready,
    input  wire [IDW-1:0]      m_bid,
    input  wire [1:0]          m_bresp,
    input  wire                m_bvalid,
    output wire                m_bready,
    output wire [IDW-1:0]      m_arid,
    output wire [AW-1:0]       m_araddr,
    output wire [7:0]          m_arlen,
    output wire [2:0]          m_arsize,
    output wire [1:0]          m_arburst,
    output wire                m_arvalid,
    input  wire                m_arready,
    input  wire [IDW-1:0]      m_rid,
    input  wire [DW-1:0]       m_rdata,
    input  wire [1:0]          m_rresp,
    input  wire                m_rlast,
    input  wire                m_rvalid,
    output wire                m_rready
);
    wire aw_full, w_full, ar_full, b_full, r_full;
    wire aw_emp,  w_emp,  ar_emp,  b_emp,  r_emp;

    async_fifo #(.DATA_WIDTH(AWW), .FIFO_DEPTH(DEPTH), .MEMORY_TYPE(MEM)) u_aw (
        .wr_clk(s_aclk), .wr_rst(s_arst), .wr_en(s_awvalid && !aw_full),
        .wr_data({s_awid, s_awaddr, s_awlen, s_awsize, s_awburst}), .wr_full(aw_full),
        .rd_clk(m_aclk), .rd_en(m_awready && !aw_emp),
        .rd_data({m_awid, m_awaddr, m_awlen, m_awsize, m_awburst}), .rd_empty(aw_emp));
    assign s_awready = !aw_full;
    assign m_awvalid = !aw_emp;

    async_fifo #(.DATA_WIDTH(WW), .FIFO_DEPTH(DEPTH), .MEMORY_TYPE(MEM)) u_w (
        .wr_clk(s_aclk), .wr_rst(s_arst), .wr_en(s_wvalid && !w_full),
        .wr_data({s_wdata, s_wstrb, s_wlast}), .wr_full(w_full),
        .rd_clk(m_aclk), .rd_en(m_wready && !w_emp),
        .rd_data({m_wdata, m_wstrb, m_wlast}), .rd_empty(w_emp));
    assign s_wready = !w_full;
    assign m_wvalid = !w_emp;

    async_fifo #(.DATA_WIDTH(BW), .FIFO_DEPTH(DEPTH), .MEMORY_TYPE(MEM)) u_b (
        .wr_clk(m_aclk), .wr_rst(m_arst), .wr_en(m_bvalid && !b_full),
        .wr_data({m_bid, m_bresp}), .wr_full(b_full),
        .rd_clk(s_aclk), .rd_en(s_bready && !b_emp),
        .rd_data({s_bid, s_bresp}), .rd_empty(b_emp));
    assign m_bready = !b_full;
    assign s_bvalid = !b_emp;

    async_fifo #(.DATA_WIDTH(ARW), .FIFO_DEPTH(DEPTH), .MEMORY_TYPE(MEM)) u_ar (
        .wr_clk(s_aclk), .wr_rst(s_arst), .wr_en(s_arvalid && !ar_full),
        .wr_data({s_arid, s_araddr, s_arlen, s_arsize, s_arburst}), .wr_full(ar_full),
        .rd_clk(m_aclk), .rd_en(m_arready && !ar_emp),
        .rd_data({m_arid, m_araddr, m_arlen, m_arsize, m_arburst}), .rd_empty(ar_emp));
    assign s_arready = !ar_full;
    assign m_arvalid = !ar_emp;

    async_fifo #(.DATA_WIDTH(RW), .FIFO_DEPTH(DEPTH), .MEMORY_TYPE(MEM)) u_r (
        .wr_clk(m_aclk), .wr_rst(m_arst), .wr_en(m_rvalid && !r_full),
        .wr_data({m_rid, m_rdata, m_rresp, m_rlast}), .wr_full(r_full),
        .rd_clk(s_aclk), .rd_en(s_rready && !r_emp),
        .rd_data({s_rid, s_rdata, s_rresp, s_rlast}), .rd_empty(r_emp));
    assign m_rready = !r_full;
    assign s_rvalid = !r_emp;
endmodule

`default_nettype wire

// Fused station OUTPUT engine, the eject mirror of sb_station_in: one flit stream ->
// ONE shared sb_nsu core -> de-concentrate 1->N. N subs share one core (in-station
// SASD) instead of one sb_nsu each. Multi-clock adds a per-sub egress CDC after u_dc.

`default_nettype none

module sb_station_out #(
    parameter integer N        = 4,
    parameter integer DW       = 512,
    parameter integer AW       = 40,
    parameter integer IDW      = 4,
    parameter integer FW       = 512,
    parameter integer TAGW     = 4,
    parameter integer SRCW     = 2,
    parameter integer WOST     = 4,
    parameter integer ROST     = 4,
    parameter integer REQ_DEPTH = 16,
    parameter integer RSP_DEPTH = 16,
    parameter         REQ_MEM  = "block",
    parameter         RSP_MEM  = "block",
    parameter         CHAN_MEM = "distributed",
    parameter integer PSEL_LSB = 16
)(
    input  wire                clk,
    input  wire                rst,

    input  wire                req_valid,
    output wire                req_ready,
    input  wire [SRCW-1:0]     req_src,
    input  wire [TAGW-1:0]     req_tag,
    input  wire                req_wr,
    input  wire                req_head,
    input  wire                req_last,
    input  wire [AW-1:0]       req_addr,
    input  wire [7:0]          req_len,
    input  wire [2:0]          req_size,
    input  wire [FW-1:0]       req_data,
    input  wire [FW/8-1:0]     req_strb,

    output wire                rsp_valid,
    input  wire                rsp_ready,
    output wire [SRCW-1:0]     rsp_dst,
    output wire [TAGW-1:0]     rsp_tag,
    output wire                rsp_wr,
    output wire                rsp_last,
    output wire [1:0]          rsp_resp,
    output wire [FW-1:0]       rsp_data,

    output wire [N*IDW-1:0]    m_awid,
    output wire [N*AW-1:0]     m_awaddr,
    output wire [N*8-1:0]      m_awlen,
    output wire [N*3-1:0]      m_awsize,
    output wire [N*2-1:0]      m_awburst,
    output wire [N-1:0]        m_awvalid,
    input  wire [N-1:0]        m_awready,
    output wire [N*DW-1:0]     m_wdata,
    output wire [N*(DW/8)-1:0] m_wstrb,
    output wire [N-1:0]        m_wlast,
    output wire [N-1:0]        m_wvalid,
    input  wire [N-1:0]        m_wready,
    input  wire [N*IDW-1:0]    m_bid,
    input  wire [N*2-1:0]      m_bresp,
    input  wire [N-1:0]        m_bvalid,
    output wire [N-1:0]        m_bready,
    output wire [N*IDW-1:0]    m_arid,
    output wire [N*AW-1:0]     m_araddr,
    output wire [N*8-1:0]      m_arlen,
    output wire [N*3-1:0]      m_arsize,
    output wire [N*2-1:0]      m_arburst,
    output wire [N-1:0]        m_arvalid,
    input  wire [N-1:0]        m_arready,
    input  wire [N*IDW-1:0]    m_rid,
    input  wire [N*DW-1:0]     m_rdata,
    input  wire [N*2-1:0]      m_rresp,
    input  wire [N-1:0]        m_rlast,
    input  wire [N-1:0]        m_rvalid,
    output wire [N-1:0]        m_rready
);
    // one shared core drives a single AXI stream
    wire [IDW-1:0] c_awid;  wire [AW-1:0] c_awaddr;  wire [7:0] c_awlen;
    wire [2:0]     c_awsize; wire [1:0]   c_awburst; wire c_awvalid, c_awready;
    wire [DW-1:0]  c_wdata; wire [DW/8-1:0] c_wstrb; wire c_wlast, c_wvalid, c_wready;
    wire [IDW-1:0] c_bid;   wire [1:0]    c_bresp;   wire c_bvalid, c_bready;
    wire [IDW-1:0] c_arid;  wire [AW-1:0] c_araddr;  wire [7:0] c_arlen;
    wire [2:0]     c_arsize; wire [1:0]   c_arburst; wire c_arvalid, c_arready;
    wire [IDW-1:0] c_rid;   wire [DW-1:0] c_rdata;   wire [1:0] c_rresp;
    wire           c_rlast, c_rvalid, c_rready;

    sb_nsu #(.SDW(DW), .SIDW(IDW), .AW(AW), .FW(FW), .TAGW(TAGW), .SRCW(SRCW),
             .WOST(WOST), .ROST(ROST), .REQ_DEPTH(REQ_DEPTH), .RSP_DEPTH(RSP_DEPTH),
             .REQ_MEM(REQ_MEM), .RSP_MEM(RSP_MEM), .CHAN_MEM(CHAN_MEM)) u_core (
        .bus_clk(clk), .bus_rst(rst),
        .req_valid(req_valid), .req_ready(req_ready), .req_src(req_src),
        .req_tag(req_tag), .req_wr(req_wr), .req_head(req_head), .req_last(req_last),
        .req_addr(req_addr), .req_len(req_len), .req_size(req_size),
        .req_data(req_data), .req_strb(req_strb),
        .rsp_valid(rsp_valid), .rsp_ready(rsp_ready), .rsp_dst(rsp_dst),
        .rsp_tag(rsp_tag), .rsp_wr(rsp_wr), .rsp_last(rsp_last),
        .rsp_resp(rsp_resp), .rsp_data(rsp_data),
        .m_aclk(clk), .m_aresetn(~rst),
        .m_awid(c_awid), .m_awaddr(c_awaddr), .m_awlen(c_awlen), .m_awsize(c_awsize),
        .m_awburst(c_awburst), .m_awvalid(c_awvalid), .m_awready(c_awready),
        .m_wdata(c_wdata), .m_wstrb(c_wstrb), .m_wlast(c_wlast),
        .m_wvalid(c_wvalid), .m_wready(c_wready),
        .m_bid(c_bid), .m_bresp(c_bresp), .m_bvalid(c_bvalid), .m_bready(c_bready),
        .m_arid(c_arid), .m_araddr(c_araddr), .m_arlen(c_arlen), .m_arsize(c_arsize),
        .m_arburst(c_arburst), .m_arvalid(c_arvalid), .m_arready(c_arready),
        .m_rid(c_rid), .m_rdata(c_rdata), .m_rresp(c_rresp), .m_rlast(c_rlast),
        .m_rvalid(c_rvalid), .m_rready(c_rready)
    );

    sb_axi_deconcentrate #(.N(N), .DW(DW), .AW(AW), .IDW(IDW), .PSEL_LSB(PSEL_LSB)) u_dc (
        .clk(clk), .rst(rst),
        .s_awid(c_awid), .s_awaddr(c_awaddr), .s_awlen(c_awlen), .s_awsize(c_awsize),
        .s_awburst(c_awburst), .s_awvalid(c_awvalid), .s_awready(c_awready),
        .s_wdata(c_wdata), .s_wstrb(c_wstrb), .s_wlast(c_wlast),
        .s_wvalid(c_wvalid), .s_wready(c_wready),
        .s_bid(c_bid), .s_bresp(c_bresp), .s_bvalid(c_bvalid), .s_bready(c_bready),
        .s_arid(c_arid), .s_araddr(c_araddr), .s_arlen(c_arlen), .s_arsize(c_arsize),
        .s_arburst(c_arburst), .s_arvalid(c_arvalid), .s_arready(c_arready),
        .s_rid(c_rid), .s_rdata(c_rdata), .s_rresp(c_rresp), .s_rlast(c_rlast),
        .s_rvalid(c_rvalid), .s_rready(c_rready),
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen), .m_awsize(m_awsize),
        .m_awburst(m_awburst), .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(m_bvalid), .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen), .m_arsize(m_arsize),
        .m_arburst(m_arburst), .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_rvalid(m_rvalid), .m_rready(m_rready)
    );
endmodule

`default_nettype wire

// AXI4-Lite subordinate port: sb_nsu plus a real burst-to-Lite protocol
// converter (sb_axi2lite). The NSU re-expresses fabric flits as SDW-wide
// bursts; a Lite slave cannot see AWLEN, so the converter walks the burst
// itself -- one Lite handshake per strobed beat, one read per slice, IDs and
// last regenerated. Terminating the burst signals instead (the pre-2026-08-22
// version) leaves orphan W beats parked at the endpoint: every later AW pairs
// with a stale beat, and reads answer only at flit lane 0.

`default_nettype none

module sb_nsu_lite #(
    parameter integer SDW       = 32,
    parameter integer AW        = 40,
    parameter integer FW        = 512,
    parameter integer TAGW      = 4,
    parameter integer SRCW      = 2,
    parameter integer WOST      = 1,
    parameter integer ROST      = 1,
    parameter integer REQ_DEPTH = 4,
    parameter integer RSP_DEPTH = 4
)(
    input  wire            bus_clk,
    input  wire            bus_rst,

    input  wire            req_valid,
    output wire            req_ready,
    input  wire [SRCW-1:0] req_src,
    input  wire [TAGW-1:0] req_tag,
    input  wire            req_wr,
    input  wire            req_head,
    input  wire            req_last,
    input  wire [AW-1:0]   req_addr,
    input  wire [7:0]      req_len,
    input  wire [2:0]      req_size,
    input  wire [FW-1:0]   req_data,
    input  wire [FW/8-1:0] req_strb,

    output wire            rsp_valid,
    input  wire            rsp_ready,
    output wire [SRCW-1:0] rsp_dst,
    output wire [TAGW-1:0] rsp_tag,
    output wire            rsp_wr,
    output wire            rsp_last,
    output wire [1:0]      rsp_resp,
    output wire [FW-1:0]   rsp_data,

    input  wire            m_aclk,
    input  wire            m_aresetn,

    output wire [AW-1:0]   m_awaddr,
    output wire [2:0]      m_awprot,
    output wire            m_awvalid,
    input  wire            m_awready,
    output wire [SDW-1:0]  m_wdata,
    output wire [SDW/8-1:0] m_wstrb,
    output wire            m_wvalid,
    input  wire            m_wready,
    input  wire [1:0]      m_bresp,
    input  wire            m_bvalid,
    output wire            m_bready,
    output wire [AW-1:0]   m_araddr,
    output wire [2:0]      m_arprot,
    output wire            m_arvalid,
    input  wire            m_arready,
    input  wire [SDW-1:0]  m_rdata,
    input  wire [1:0]      m_rresp,
    input  wire            m_rvalid,
    output wire            m_rready
);
    assign m_awprot = 3'b000;
    assign m_arprot = 3'b000;

    wire [0:0]       n_awid, n_arid, n_bid, n_rid;
    wire [AW-1:0]    n_awaddr, n_araddr;
    wire [7:0]       n_awlen, n_arlen;
    wire             n_awvalid, n_awready, n_wvalid, n_wready;
    wire             n_wlast, n_bvalid, n_bready;
    wire             n_arvalid, n_arready, n_rvalid, n_rready, n_rlast;
    wire [SDW-1:0]   n_wdata, n_rdata;
    wire [SDW/8-1:0] n_wstrb;
    wire [1:0]       n_bresp, n_rresp;

    sb_nsu #(
        .SDW(SDW), .SIDW(1), .AW(AW), .FW(FW), .TAGW(TAGW), .SRCW(SRCW),
        .WOST(WOST), .ROST(ROST),
        .REQ_DEPTH(REQ_DEPTH), .RSP_DEPTH(RSP_DEPTH)
    ) u_nsu (
        .bus_clk(bus_clk), .bus_rst(bus_rst),
        .req_valid(req_valid), .req_ready(req_ready), .req_src(req_src),
        .req_tag(req_tag), .req_wr(req_wr), .req_head(req_head),
        .req_last(req_last), .req_addr(req_addr), .req_len(req_len),
        .req_size(req_size), .req_data(req_data), .req_strb(req_strb),
        .rsp_valid(rsp_valid), .rsp_ready(rsp_ready), .rsp_dst(rsp_dst),
        .rsp_tag(rsp_tag), .rsp_wr(rsp_wr), .rsp_last(rsp_last),
        .rsp_resp(rsp_resp), .rsp_data(rsp_data),
        .m_aclk(m_aclk), .m_aresetn(m_aresetn),
        .m_awid(n_awid), .m_awaddr(n_awaddr), .m_awlen(n_awlen),
        .m_awsize(), .m_awburst(),
        .m_awvalid(n_awvalid), .m_awready(n_awready),
        .m_wdata(n_wdata), .m_wstrb(n_wstrb), .m_wlast(n_wlast),
        .m_wvalid(n_wvalid), .m_wready(n_wready),
        .m_bid(n_bid), .m_bresp(n_bresp), .m_bvalid(n_bvalid),
        .m_bready(n_bready),
        .m_arid(n_arid), .m_araddr(n_araddr), .m_arlen(n_arlen),
        .m_arsize(), .m_arburst(),
        .m_arvalid(n_arvalid), .m_arready(n_arready),
        .m_rid(n_rid), .m_rdata(n_rdata), .m_rresp(n_rresp),
        .m_rlast(n_rlast), .m_rvalid(n_rvalid), .m_rready(n_rready)
    );

    sb_axi2lite #(.DW(SDW), .AW(AW), .IDW(1)) u_conv (
        .clk(m_aclk), .resetn(m_aresetn),
        .s_awid(n_awid), .s_awaddr(n_awaddr), .s_awlen(n_awlen),
        .s_awvalid(n_awvalid), .s_awready(n_awready),
        .s_wdata(n_wdata), .s_wstrb(n_wstrb), .s_wlast(n_wlast),
        .s_wvalid(n_wvalid), .s_wready(n_wready),
        .s_bid(n_bid), .s_bresp(n_bresp), .s_bvalid(n_bvalid),
        .s_bready(n_bready),
        .s_arid(n_arid), .s_araddr(n_araddr), .s_arlen(n_arlen),
        .s_arvalid(n_arvalid), .s_arready(n_arready),
        .s_rid(n_rid), .s_rdata(n_rdata), .s_rresp(n_rresp),
        .s_rlast(n_rlast), .s_rvalid(n_rvalid), .s_rready(n_rready),
        .m_awaddr(m_awaddr), .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bresp(m_bresp), .m_bvalid(m_bvalid), .m_bready(m_bready),
        .m_araddr(m_araddr), .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rdata(m_rdata), .m_rresp(m_rresp),
        .m_rvalid(m_rvalid), .m_rready(m_rready)
    );
endmodule

`default_nettype wire

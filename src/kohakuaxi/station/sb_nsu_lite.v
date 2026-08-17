// AXI4-Lite subordinate port: sb_nsu with the ID, burst and last signals
// terminated. WOST/ROST default to 1 because Lite has no ordering to preserve.

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
        .m_awid(), .m_awaddr(m_awaddr), .m_awlen(), .m_awsize(),
        .m_awburst(), .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(1'b0), .m_bresp(m_bresp), .m_bvalid(m_bvalid),
        .m_bready(m_bready),
        .m_arid(), .m_araddr(m_araddr), .m_arlen(), .m_arsize(),
        .m_arburst(), .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rid(1'b0), .m_rdata(m_rdata), .m_rresp(m_rresp), .m_rlast(1'b1),
        .m_rvalid(m_rvalid), .m_rready(m_rready)
    );
endmodule

`default_nettype wire

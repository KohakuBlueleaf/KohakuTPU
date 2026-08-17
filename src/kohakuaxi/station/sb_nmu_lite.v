// AXI4-Lite manager port. A Lite manager is an AXI4 manager with LEN=0,
// SIZE=full width, BURST=INCR and no ID, so this is sb_nmu with constants.

// The constants propagate, so the tag table collapses to a flag and the burst
// counters disappear. Depths default small because a Lite packet is one flit.

`default_nettype none

module sb_nmu_lite #(
    parameter integer MW        = 32,
    parameter integer AW        = 40,
    parameter integer FW        = 512,
    parameter integer TAGW      = 2,
    parameter integer DSTW      = 4,
    parameter integer NSEG      = 8,
    parameter integer REQ_DEPTH = 4,
    parameter integer RSP_DEPTH = 4,
    parameter [NSEG*AW-1:0]   SEG_BASE = {NSEG*AW{1'b0}},
    parameter [NSEG*AW-1:0]   SEG_MASK = {NSEG*AW{1'b0}},
    parameter [NSEG*AW-1:0]   SEG_XLT  = {NSEG*AW{1'b0}},
    parameter [NSEG*DSTW-1:0] SEG_DST  = {NSEG*DSTW{1'b0}},
    parameter [NSEG-1:0]      SEG_VLD  = {NSEG{1'b0}}
)(
    input  wire            s_aclk,
    input  wire            s_aresetn,

    input  wire [AW-1:0]   s_awaddr,
    input  wire [2:0]      s_awprot,
    input  wire            s_awvalid,
    output wire            s_awready,
    input  wire [MW-1:0]   s_wdata,
    input  wire [MW/8-1:0] s_wstrb,
    input  wire            s_wvalid,
    output wire            s_wready,
    output wire [1:0]      s_bresp,
    output wire            s_bvalid,
    input  wire            s_bready,
    input  wire [AW-1:0]   s_araddr,
    input  wire [2:0]      s_arprot,
    input  wire            s_arvalid,
    output wire            s_arready,
    output wire [MW-1:0]   s_rdata,
    output wire [1:0]      s_rresp,
    output wire            s_rvalid,
    input  wire            s_rready,

    input  wire            bus_clk,
    input  wire            bus_rst,

    output wire            req_valid,
    input  wire            req_ready,
    output wire [DSTW-1:0] req_dst,
    output wire [TAGW-1:0] req_tag,
    output wire            req_wr,
    output wire            req_head,
    output wire            req_last,
    output wire [AW-1:0]   req_addr,
    output wire [7:0]      req_len,
    output wire [2:0]      req_size,
    output wire [FW-1:0]   req_data,
    output wire [FW/8-1:0] req_strb,

    input  wire            rsp_valid,
    output wire            rsp_ready,
    input  wire [TAGW-1:0] rsp_tag,
    input  wire            rsp_wr,
    input  wire            rsp_last,
    input  wire [1:0]      rsp_resp,
    input  wire [FW-1:0]   rsp_data,

    output wire [31:0]     stat_decerr
);
    localparam [2:0] SZ = $clog2(MW/8);

    sb_nmu #(
        .MW(MW), .MIDW(1), .AW(AW), .FW(FW), .TAGW(TAGW), .DSTW(DSTW),
        .NSEG(NSEG), .REQ_DEPTH(REQ_DEPTH), .RSP_DEPTH(RSP_DEPTH),
        .SEG_BASE(SEG_BASE), .SEG_MASK(SEG_MASK), .SEG_XLT(SEG_XLT),
        .SEG_DST(SEG_DST), .SEG_VLD(SEG_VLD)
    ) u_nmu (
        .s_aclk(s_aclk), .s_aresetn(s_aresetn),
        .s_awid(1'b0), .s_awaddr(s_awaddr), .s_awlen(8'd0), .s_awsize(SZ),
        .s_awburst(2'b01), .s_awvalid(s_awvalid), .s_awready(s_awready),
        .s_wdata(s_wdata), .s_wstrb(s_wstrb), .s_wlast(1'b1),
        .s_wvalid(s_wvalid), .s_wready(s_wready),
        .s_bid(), .s_bresp(s_bresp), .s_bvalid(s_bvalid), .s_bready(s_bready),
        .s_arid(1'b0), .s_araddr(s_araddr), .s_arlen(8'd0), .s_arsize(SZ),
        .s_arburst(2'b01), .s_arvalid(s_arvalid), .s_arready(s_arready),
        .s_rid(), .s_rdata(s_rdata), .s_rresp(s_rresp), .s_rlast(),
        .s_rvalid(s_rvalid), .s_rready(s_rready),
        .bus_clk(bus_clk), .bus_rst(bus_rst),
        .req_valid(req_valid), .req_ready(req_ready), .req_dst(req_dst),
        .req_tag(req_tag), .req_wr(req_wr), .req_head(req_head),
        .req_last(req_last), .req_addr(req_addr), .req_len(req_len),
        .req_size(req_size), .req_data(req_data), .req_strb(req_strb),
        .rsp_valid(rsp_valid), .rsp_ready(rsp_ready), .rsp_tag(rsp_tag),
        .rsp_wr(rsp_wr), .rsp_last(rsp_last), .rsp_resp(rsp_resp),
        .rsp_data(rsp_data),
        .stat_decerr(stat_decerr)
    );
endmodule

`default_nettype wire

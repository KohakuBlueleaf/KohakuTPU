// Area/Fmax probe for one sb_nmu with a realistic 2-segment decode, so the
// decode comparators and translate are present (SEG_MASK=0 would fold them).
// Exposes every AXI slave and flit master port at the top so nothing prunes.

`default_nettype none

module nmu_probe #(
    parameter integer MW          = 32,
    parameter integer FW          = 256,
    parameter integer AW          = 43,
    parameter integer OUTST       = 0,
    parameter integer FORCE_PLACE = 0
)(
    input  wire                s_aclk,
    input  wire                s_aresetn,
    input  wire [3:0]          s_awid,
    input  wire [AW-1:0]       s_awaddr,
    input  wire [7:0]          s_awlen,
    input  wire [2:0]          s_awsize,
    input  wire [1:0]          s_awburst,
    input  wire                s_awvalid,
    output wire                s_awready,
    input  wire [MW-1:0]       s_wdata,
    input  wire [MW/8-1:0]     s_wstrb,
    input  wire                s_wlast,
    input  wire                s_wvalid,
    output wire                s_wready,
    output wire [3:0]          s_bid,
    output wire [1:0]          s_bresp,
    output wire                s_bvalid,
    input  wire                s_bready,
    input  wire [3:0]          s_arid,
    input  wire [AW-1:0]       s_araddr,
    input  wire [7:0]          s_arlen,
    input  wire [2:0]          s_arsize,
    input  wire [1:0]          s_arburst,
    input  wire                s_arvalid,
    output wire                s_arready,
    output wire [3:0]          s_rid,
    output wire [MW-1:0]       s_rdata,
    output wire [1:0]          s_rresp,
    output wire                s_rlast,
    output wire                s_rvalid,
    input  wire                s_rready,
    input  wire                bus_clk,
    input  wire                bus_rst,
    output wire                req_valid,
    input  wire                req_ready,
    output wire [3:0]          req_dst,
    output wire [3:0]          req_dport,
    output wire [3:0]          req_tag,
    output wire                req_wr,
    output wire                req_head,
    output wire                req_last,
    output wire [AW-1:0]       req_addr,
    output wire [7:0]          req_len,
    output wire [2:0]          req_size,
    output wire [FW-1:0]       req_data,
    output wire [FW/8-1:0]     req_strb,
    input  wire                rsp_valid,
    output wire                rsp_ready,
    input  wire [3:0]          rsp_tag,
    input  wire                rsp_wr,
    input  wire                rsp_last,
    input  wire [1:0]          rsp_resp,
    input  wire [FW-1:0]       rsp_data,
    output wire [31:0]         stat_decerr
);
    localparam integer NSEG = 8;
    localparam integer DSTW = 4;
    // seg0 -> dst0 (low half), seg1 -> dst1 (bit-42 half): top 12 bits compared.
    localparam [AW-1:0] M  = {12'hFFF, {(AW-12){1'b0}}};
    localparam [AW-1:0] B0 = {AW{1'b0}};
    localparam [AW-1:0] B1 = {1'b1, {(AW-1){1'b0}}};

    sb_nmu #(
        .MW(MW), .MIDW(4), .AW(AW), .FW(FW), .TAGW(4), .DSTW(DSTW), .NSEG(NSEG),
        .OUTST(OUTST), .FORCE_PLACE(FORCE_PLACE),
        .SEG_VLD (8'h03),
        .SEG_MASK({{(6*AW){1'b0}}, M, M}),
        .SEG_BASE({{(6*AW){1'b0}}, B1, B0}),
        .SEG_XLT ({{(6*AW){1'b0}}, B1, B0}),
        .SEG_DST ({{(6*DSTW){1'b0}}, 4'd1, 4'd0}),
        .SEG_DPORT({NSEG*DSTW{1'b0}})
    ) u_nmu (
        .s_aclk(s_aclk), .s_aresetn(s_aresetn),
        .s_awid(s_awid), .s_awaddr(s_awaddr), .s_awlen(s_awlen),
        .s_awsize(s_awsize), .s_awburst(s_awburst),
        .s_awvalid(s_awvalid), .s_awready(s_awready),
        .s_wdata(s_wdata), .s_wstrb(s_wstrb), .s_wlast(s_wlast),
        .s_wvalid(s_wvalid), .s_wready(s_wready),
        .s_bid(s_bid), .s_bresp(s_bresp), .s_bvalid(s_bvalid), .s_bready(s_bready),
        .s_arid(s_arid), .s_araddr(s_araddr), .s_arlen(s_arlen),
        .s_arsize(s_arsize), .s_arburst(s_arburst),
        .s_arvalid(s_arvalid), .s_arready(s_arready),
        .s_rid(s_rid), .s_rdata(s_rdata), .s_rresp(s_rresp),
        .s_rlast(s_rlast), .s_rvalid(s_rvalid), .s_rready(s_rready),
        .bus_clk(bus_clk), .bus_rst(bus_rst),
        .req_valid(req_valid), .req_ready(req_ready),
        .req_dst(req_dst), .req_dport(req_dport), .req_tag(req_tag),
        .req_wr(req_wr), .req_head(req_head), .req_last(req_last),
        .req_addr(req_addr), .req_len(req_len), .req_size(req_size),
        .req_data(req_data), .req_strb(req_strb),
        .rsp_valid(rsp_valid), .rsp_ready(rsp_ready), .rsp_tag(rsp_tag),
        .rsp_wr(rsp_wr), .rsp_last(rsp_last), .rsp_resp(rsp_resp),
        .rsp_data(rsp_data), .stat_decerr(stat_decerr)
    );
endmodule

`default_nettype wire

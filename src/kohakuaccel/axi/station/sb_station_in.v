// Fused station INPUT engine: M AXI masters -> one shared decode/tag/pack -> one
// flit stream. "In-station SASD": the concentrator arbitrates the M masters onto a
// single AXI stream (owner-tagged ID), and ONE sb_nmu core does the decode + tag +
// pack that used to be replicated per master. The cross-station parallelism (SAMD)
// lives in the backbone, not here.

// v1 is single-clock (masters + fabric on `clk`) so the fusion win is measured
// without CDC noise -- CDC is per-master either way, a wash vs independent NMUs.
// Multi-clock adds a per-master AXI CDC in front of the concentrator; the core is
// unchanged. Uniform master width DW (the general-shape comparison is uniform).

`default_nettype none

module sb_station_in #(
    parameter integer M     = 4,
    parameter integer DW    = 512,
    parameter integer AW    = 40,
    parameter integer IDW   = 4,
    parameter integer FW    = 256,
    parameter integer TAGW  = 4,
    parameter integer DSTW  = 4,
    parameter integer NSEG  = 8,
    parameter integer OUTST = 0,
    parameter integer STORE_FWD = 1,
    parameter         MEM   = "block",         // req/rsp FIFO storage
    parameter integer OW    = (M <= 1) ? 1 : $clog2(M),
    parameter integer OIDW  = IDW + OW,
    parameter integer REQ_DEPTH = 256,
    parameter integer RSP_DEPTH = 256,
    parameter [NSEG*AW-1:0]   SEG_BASE  = {NSEG*AW{1'b0}},
    parameter [NSEG*AW-1:0]   SEG_MASK  = {NSEG*AW{1'b0}},
    parameter [NSEG*AW-1:0]   SEG_XLT   = {NSEG*AW{1'b0}},
    parameter [NSEG*DSTW-1:0] SEG_DST   = {NSEG*DSTW{1'b0}},
    parameter [NSEG*DSTW-1:0] SEG_DPORT = {NSEG*DSTW{1'b0}},
    parameter [NSEG-1:0]      SEG_VLD   = {NSEG{1'b0}}
)(
    input  wire                clk,
    input  wire                rst,

    input  wire [M*IDW-1:0]    s_awid,
    input  wire [M*AW-1:0]     s_awaddr,
    input  wire [M*8-1:0]      s_awlen,
    input  wire [M*3-1:0]      s_awsize,
    input  wire [M*2-1:0]      s_awburst,
    input  wire [M-1:0]        s_awvalid,
    output wire [M-1:0]        s_awready,
    input  wire [M*DW-1:0]     s_wdata,
    input  wire [M*(DW/8)-1:0] s_wstrb,
    input  wire [M-1:0]        s_wlast,
    input  wire [M-1:0]        s_wvalid,
    output wire [M-1:0]        s_wready,
    output wire [M*IDW-1:0]    s_bid,
    output wire [M*2-1:0]      s_bresp,
    output wire [M-1:0]        s_bvalid,
    input  wire [M-1:0]        s_bready,
    input  wire [M*IDW-1:0]    s_arid,
    input  wire [M*AW-1:0]     s_araddr,
    input  wire [M*8-1:0]      s_arlen,
    input  wire [M*3-1:0]      s_arsize,
    input  wire [M*2-1:0]      s_arburst,
    input  wire [M-1:0]        s_arvalid,
    output wire [M-1:0]        s_arready,
    output wire [M*IDW-1:0]    s_rid,
    output wire [M*DW-1:0]     s_rdata,
    output wire [M*2-1:0]      s_rresp,
    output wire [M-1:0]        s_rlast,
    output wire [M-1:0]        s_rvalid,
    input  wire [M-1:0]        s_rready,

    output wire                req_valid,
    input  wire                req_ready,
    output wire [DSTW-1:0]     req_dst,
    output wire [DSTW-1:0]     req_dport,
    output wire [TAGW-1:0]     req_tag,
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
    input  wire [TAGW-1:0]     rsp_tag,
    input  wire                rsp_wr,
    input  wire                rsp_last,
    input  wire [1:0]          rsp_resp,
    input  wire [FW-1:0]       rsp_data,

    output wire [31:0]         stat_decerr
);
    // ---- concentrator: M masters -> one owner-tagged AXI stream --------------
    wire [OIDW-1:0]  c_awid;   wire [AW-1:0] c_awaddr;  wire [7:0] c_awlen;
    wire [2:0]       c_awsize; wire [1:0]    c_awburst; wire c_awvalid, c_awready;
    wire [DW-1:0]    c_wdata;  wire [DW/8-1:0] c_wstrb;  wire c_wlast, c_wvalid, c_wready;
    wire [OIDW-1:0]  c_bid;    wire [1:0]    c_bresp;   wire c_bvalid, c_bready;
    wire [OIDW-1:0]  c_arid;   wire [AW-1:0] c_araddr;  wire [7:0] c_arlen;
    wire [2:0]       c_arsize; wire [1:0]    c_arburst; wire c_arvalid, c_arready;
    wire [OIDW-1:0]  c_rid;    wire [DW-1:0] c_rdata;   wire [1:0] c_rresp;
    wire             c_rlast, c_rvalid, c_rready;

    sb_axi_concentrate #(.M(M), .DW(DW), .AW(AW), .IDW(IDW)) u_cc (
        .clk(clk), .rst(rst),
        .s_awid(s_awid), .s_awaddr(s_awaddr), .s_awlen(s_awlen), .s_awsize(s_awsize),
        .s_awburst(s_awburst), .s_awvalid(s_awvalid), .s_awready(s_awready),
        .s_wdata(s_wdata), .s_wstrb(s_wstrb), .s_wlast(s_wlast),
        .s_wvalid(s_wvalid), .s_wready(s_wready),
        .s_bid(s_bid), .s_bresp(s_bresp), .s_bvalid(s_bvalid), .s_bready(s_bready),
        .s_arid(s_arid), .s_araddr(s_araddr), .s_arlen(s_arlen), .s_arsize(s_arsize),
        .s_arburst(s_arburst), .s_arvalid(s_arvalid), .s_arready(s_arready),
        .s_rid(s_rid), .s_rdata(s_rdata), .s_rresp(s_rresp), .s_rlast(s_rlast),
        .s_rvalid(s_rvalid), .s_rready(s_rready),
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

    // ---- one shared core: decode + tag + pack -> flit ------------------------
    sb_nmu #(.MW(DW), .MIDW(OIDW), .AW(AW), .FW(FW), .TAGW(TAGW), .DSTW(DSTW),
             .NSEG(NSEG), .OUTST(OUTST), .STORE_FWD(STORE_FWD),
             .REQ_MEM(MEM), .RSP_MEM(MEM),
             .REQ_DEPTH(REQ_DEPTH), .RSP_DEPTH(RSP_DEPTH),
             .SEG_BASE(SEG_BASE), .SEG_MASK(SEG_MASK), .SEG_XLT(SEG_XLT),
             .SEG_DST(SEG_DST), .SEG_DPORT(SEG_DPORT), .SEG_VLD(SEG_VLD)) u_core (
        .s_aclk(clk), .s_aresetn(~rst),
        .s_awid(c_awid), .s_awaddr(c_awaddr), .s_awlen(c_awlen), .s_awsize(c_awsize),
        .s_awburst(c_awburst), .s_awvalid(c_awvalid), .s_awready(c_awready),
        .s_wdata(c_wdata), .s_wstrb(c_wstrb), .s_wlast(c_wlast),
        .s_wvalid(c_wvalid), .s_wready(c_wready),
        .s_bid(c_bid), .s_bresp(c_bresp), .s_bvalid(c_bvalid), .s_bready(c_bready),
        .s_arid(c_arid), .s_araddr(c_araddr), .s_arlen(c_arlen), .s_arsize(c_arsize),
        .s_arburst(c_arburst), .s_arvalid(c_arvalid), .s_arready(c_arready),
        .s_rid(c_rid), .s_rdata(c_rdata), .s_rresp(c_rresp), .s_rlast(c_rlast),
        .s_rvalid(c_rvalid), .s_rready(c_rready),
        .bus_clk(clk), .bus_rst(rst),
        .req_valid(req_valid), .req_ready(req_ready), .req_dst(req_dst),
        .req_dport(req_dport), .req_tag(req_tag), .req_wr(req_wr), .req_head(req_head),
        .req_last(req_last), .req_addr(req_addr), .req_len(req_len), .req_size(req_size),
        .req_data(req_data), .req_strb(req_strb),
        .rsp_valid(rsp_valid), .rsp_ready(rsp_ready), .rsp_tag(rsp_tag),
        .rsp_wr(rsp_wr), .rsp_last(rsp_last), .rsp_resp(rsp_resp), .rsp_data(rsp_data),
        .stat_decerr(stat_decerr)
    );
endmodule

`default_nettype wire

// The ship memory path: kaxi_xbar5 (M masters -> N homes, configurable SASD/SAMD) with
// a per-home kaxi_l3 cache behind each home, DRAM masters exposed. Never shipped as a
// bare crossbar -- the home port and the cache slave port are the SAME AXI, so the
// crossbar's owner-tagged home ID (SID_W = ID_W + log2 M) IS the cache's ID width.

`default_nettype none

module kaxi_mempath #(
    parameter integer M        = 4,
    parameter integer N_HOME   = 4,
    parameter integer ADDR_W   = 40,
    parameter integer DATA_W   = 512,
    parameter integer ID_W     = 4,
    parameter integer HOME_LSB = 32,
    parameter integer WR_MODE  = 0,                 // ship: SASD write
    parameter integer RD_MODE  = 1,                 // ship: SAMD read
    parameter integer SETS     = 32768,             // 2 MB/home at 512b
    parameter integer SET_W    = 15,
    parameter         RAM_STYLE = "ultra",
    parameter integer MIDX_W   = (M <= 1) ? 1 : $clog2(M),
    parameter integer SID_W    = ID_W + MIDX_W
)(
    input  wire                    clk,
    input  wire                    resetn,

    input  wire [M*ID_W-1:0]       s_awid,
    input  wire [M*ADDR_W-1:0]     s_awaddr,
    input  wire [M*8-1:0]          s_awlen,
    input  wire [M*3-1:0]          s_awsize,
    input  wire [M*2-1:0]          s_awburst,
    input  wire [M-1:0]            s_awvalid,
    output wire [M-1:0]            s_awready,
    input  wire [M*DATA_W-1:0]     s_wdata,
    input  wire [M*(DATA_W/8)-1:0] s_wstrb,
    input  wire [M-1:0]            s_wlast,
    input  wire [M-1:0]            s_wvalid,
    output wire [M-1:0]            s_wready,
    output wire [M*ID_W-1:0]       s_bid,
    output wire [M*2-1:0]          s_bresp,
    output wire [M-1:0]            s_bvalid,
    input  wire [M-1:0]            s_bready,
    input  wire [M*ID_W-1:0]       s_arid,
    input  wire [M*ADDR_W-1:0]     s_araddr,
    input  wire [M*8-1:0]          s_arlen,
    input  wire [M*3-1:0]          s_arsize,
    input  wire [M*2-1:0]          s_arburst,
    input  wire [M-1:0]            s_arvalid,
    output wire [M-1:0]            s_arready,
    output wire [M*ID_W-1:0]       s_rid,
    output wire [M*DATA_W-1:0]     s_rdata,
    output wire [M*2-1:0]          s_rresp,
    output wire [M-1:0]            s_rlast,
    output wire [M-1:0]            s_rvalid,
    input  wire [M-1:0]            s_rready,

    // ---- N DRAM masters (cache -> MIG) --------------------------------------
    output wire [N_HOME*SID_W-1:0]     d_awid,
    output wire [N_HOME*ADDR_W-1:0]    d_awaddr,
    output wire [N_HOME*8-1:0]         d_awlen,
    output wire [N_HOME*3-1:0]         d_awsize,
    output wire [N_HOME*2-1:0]         d_awburst,
    output wire [N_HOME-1:0]           d_awvalid,
    input  wire [N_HOME-1:0]           d_awready,
    output wire [N_HOME*DATA_W-1:0]    d_wdata,
    output wire [N_HOME*(DATA_W/8)-1:0] d_wstrb,
    output wire [N_HOME-1:0]           d_wlast,
    output wire [N_HOME-1:0]           d_wvalid,
    input  wire [N_HOME-1:0]           d_wready,
    input  wire [N_HOME*SID_W-1:0]     d_bid,
    input  wire [N_HOME*2-1:0]         d_bresp,
    input  wire [N_HOME-1:0]           d_bvalid,
    output wire [N_HOME-1:0]           d_bready,
    output wire [N_HOME*SID_W-1:0]     d_arid,
    output wire [N_HOME*ADDR_W-1:0]    d_araddr,
    output wire [N_HOME*8-1:0]         d_arlen,
    output wire [N_HOME*3-1:0]         d_arsize,
    output wire [N_HOME*2-1:0]         d_arburst,
    output wire [N_HOME-1:0]           d_arvalid,
    input  wire [N_HOME-1:0]           d_arready,
    input  wire [N_HOME*SID_W-1:0]     d_rid,
    input  wire [N_HOME*DATA_W-1:0]    d_rdata,
    input  wire [N_HOME*2-1:0]         d_rresp,
    input  wire [N_HOME-1:0]           d_rlast,
    input  wire [N_HOME-1:0]           d_rvalid,
    output wire [N_HOME-1:0]           d_rready
);
    wire [N_HOME*SID_W-1:0]     h_awid, h_arid, h_bid, h_rid;
    wire [N_HOME*ADDR_W-1:0]    h_awaddr, h_araddr;
    wire [N_HOME*8-1:0]         h_awlen, h_arlen;
    wire [N_HOME*3-1:0]         h_awsize, h_arsize;
    wire [N_HOME*2-1:0]         h_awburst, h_arburst, h_bresp, h_rresp;
    wire [N_HOME-1:0]           h_awvalid, h_awready, h_wvalid, h_wready, h_wlast;
    wire [N_HOME-1:0]           h_bvalid, h_bready, h_arvalid, h_arready, h_rvalid, h_rready, h_rlast;
    wire [N_HOME*DATA_W-1:0]    h_wdata, h_rdata;
    wire [N_HOME*(DATA_W/8)-1:0] h_wstrb;

    kaxi_xbar5 #(.M(M), .N_HOME(N_HOME), .ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(ID_W),
                 .HOME_LSB(HOME_LSB), .WR_MODE(WR_MODE), .RD_MODE(RD_MODE)) u_xbar (
        .clk(clk), .resetn(resetn),
        .s_awid(s_awid), .s_awaddr(s_awaddr), .s_awlen(s_awlen), .s_awsize(s_awsize),
        .s_awburst(s_awburst), .s_awvalid(s_awvalid), .s_awready(s_awready),
        .s_wdata(s_wdata), .s_wstrb(s_wstrb), .s_wlast(s_wlast),
        .s_wvalid(s_wvalid), .s_wready(s_wready),
        .s_bid(s_bid), .s_bresp(s_bresp), .s_bvalid(s_bvalid), .s_bready(s_bready),
        .s_arid(s_arid), .s_araddr(s_araddr), .s_arlen(s_arlen), .s_arsize(s_arsize),
        .s_arburst(s_arburst), .s_arvalid(s_arvalid), .s_arready(s_arready),
        .s_rid(s_rid), .s_rdata(s_rdata), .s_rresp(s_rresp), .s_rlast(s_rlast),
        .s_rvalid(s_rvalid), .s_rready(s_rready),
        .m_awid(h_awid), .m_awaddr(h_awaddr), .m_awlen(h_awlen), .m_awsize(h_awsize),
        .m_awburst(h_awburst), .m_awvalid(h_awvalid), .m_awready(h_awready),
        .m_wdata(h_wdata), .m_wstrb(h_wstrb), .m_wlast(h_wlast),
        .m_wvalid(h_wvalid), .m_wready(h_wready),
        .m_bid(h_bid), .m_bresp(h_bresp), .m_bvalid(h_bvalid), .m_bready(h_bready),
        .m_arid(h_arid), .m_araddr(h_araddr), .m_arlen(h_arlen), .m_arsize(h_arsize),
        .m_arburst(h_arburst), .m_arvalid(h_arvalid), .m_arready(h_arready),
        .m_rid(h_rid), .m_rdata(h_rdata), .m_rresp(h_rresp), .m_rlast(h_rlast),
        .m_rvalid(h_rvalid), .m_rready(h_rready)
    );

    genvar h;
    generate for (h = 0; h < N_HOME; h = h + 1) begin : g_cache
        kaxi_l3 #(.ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(SID_W), .SETS(SETS),
                  .SET_W(SET_W), .RAM_STYLE(RAM_STYLE)) u_l3 (
            .clk(clk), .resetn(resetn),
            .s_awid(h_awid[h*SID_W +: SID_W]), .s_awaddr(h_awaddr[h*ADDR_W +: ADDR_W]),
            .s_awlen(h_awlen[h*8 +: 8]), .s_awsize(h_awsize[h*3 +: 3]),
            .s_awburst(h_awburst[h*2 +: 2]), .s_awvalid(h_awvalid[h]), .s_awready(h_awready[h]),
            .s_wdata(h_wdata[h*DATA_W +: DATA_W]), .s_wstrb(h_wstrb[h*(DATA_W/8) +: DATA_W/8]),
            .s_wlast(h_wlast[h]), .s_wvalid(h_wvalid[h]), .s_wready(h_wready[h]),
            .s_bid(h_bid[h*SID_W +: SID_W]), .s_bresp(h_bresp[h*2 +: 2]),
            .s_bvalid(h_bvalid[h]), .s_bready(h_bready[h]),
            .s_arid(h_arid[h*SID_W +: SID_W]), .s_araddr(h_araddr[h*ADDR_W +: ADDR_W]),
            .s_arlen(h_arlen[h*8 +: 8]), .s_arsize(h_arsize[h*3 +: 3]),
            .s_arburst(h_arburst[h*2 +: 2]), .s_arvalid(h_arvalid[h]), .s_arready(h_arready[h]),
            .s_rid(h_rid[h*SID_W +: SID_W]), .s_rdata(h_rdata[h*DATA_W +: DATA_W]),
            .s_rresp(h_rresp[h*2 +: 2]), .s_rlast(h_rlast[h]),
            .s_rvalid(h_rvalid[h]), .s_rready(h_rready[h]),
            .m_awid(d_awid[h*SID_W +: SID_W]), .m_awaddr(d_awaddr[h*ADDR_W +: ADDR_W]),
            .m_awlen(d_awlen[h*8 +: 8]), .m_awsize(d_awsize[h*3 +: 3]),
            .m_awburst(d_awburst[h*2 +: 2]), .m_awvalid(d_awvalid[h]), .m_awready(d_awready[h]),
            .m_wdata(d_wdata[h*DATA_W +: DATA_W]), .m_wstrb(d_wstrb[h*(DATA_W/8) +: DATA_W/8]),
            .m_wlast(d_wlast[h]), .m_wvalid(d_wvalid[h]), .m_wready(d_wready[h]),
            .m_bid(d_bid[h*SID_W +: SID_W]), .m_bresp(d_bresp[h*2 +: 2]),
            .m_bvalid(d_bvalid[h]), .m_bready(d_bready[h]),
            .m_arid(d_arid[h*SID_W +: SID_W]), .m_araddr(d_araddr[h*ADDR_W +: ADDR_W]),
            .m_arlen(d_arlen[h*8 +: 8]), .m_arsize(d_arsize[h*3 +: 3]),
            .m_arburst(d_arburst[h*2 +: 2]), .m_arvalid(d_arvalid[h]), .m_arready(d_arready[h]),
            .m_rid(d_rid[h*SID_W +: SID_W]), .m_rdata(d_rdata[h*DATA_W +: DATA_W]),
            .m_rresp(d_rresp[h*2 +: 2]), .m_rlast(d_rlast[h]),
            .m_rvalid(d_rvalid[h]), .m_rready(d_rready[h])
        );
    end endgenerate
endmodule

`default_nettype wire

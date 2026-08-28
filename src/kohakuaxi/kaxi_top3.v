// KohakuAXI system v3 = kaxi_xbar3 (SASD-write) + per-home URAM L3 -> MIG.
// Lean-xbar + URAM-cache config; measures the SLR1-budget number.

`default_nettype none

module kaxi_top3 #(
    parameter integer M        = 4,
    parameter integer N_HOME   = 4,
    parameter integer ADDR_W   = 40,
    parameter integer DATA_W   = 512,
    parameter integer ID_W     = 4,
    parameter integer HOME_LSB = 32,
    parameter integer L3_SETS  = 32768,
    parameter integer L3_SET_W = 15,
    parameter integer L3_LSB   = 6,
    parameter         L3_RAM   = "ultra",
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

    output wire [N_HOME*SID_W-1:0]     m_awid,
    output wire [N_HOME*ADDR_W-1:0]    m_awaddr,
    output wire [N_HOME*8-1:0]         m_awlen,
    output wire [N_HOME*3-1:0]         m_awsize,
    output wire [N_HOME*2-1:0]         m_awburst,
    output wire [N_HOME-1:0]           m_awvalid,
    input  wire [N_HOME-1:0]           m_awready,
    output wire [N_HOME*DATA_W-1:0]    m_wdata,
    output wire [N_HOME*(DATA_W/8)-1:0] m_wstrb,
    output wire [N_HOME-1:0]           m_wlast,
    output wire [N_HOME-1:0]           m_wvalid,
    input  wire [N_HOME-1:0]           m_wready,
    input  wire [N_HOME*SID_W-1:0]     m_bid,
    input  wire [N_HOME*2-1:0]         m_bresp,
    input  wire [N_HOME-1:0]           m_bvalid,
    output wire [N_HOME-1:0]           m_bready,
    output wire [N_HOME*SID_W-1:0]     m_arid,
    output wire [N_HOME*ADDR_W-1:0]    m_araddr,
    output wire [N_HOME*8-1:0]         m_arlen,
    output wire [N_HOME*3-1:0]         m_arsize,
    output wire [N_HOME*2-1:0]         m_arburst,
    output wire [N_HOME-1:0]           m_arvalid,
    input  wire [N_HOME-1:0]           m_arready,
    input  wire [N_HOME*SID_W-1:0]     m_rid,
    input  wire [N_HOME*DATA_W-1:0]    m_rdata,
    input  wire [N_HOME*2-1:0]         m_rresp,
    input  wire [N_HOME-1:0]           m_rlast,
    input  wire [N_HOME-1:0]           m_rvalid,
    output wire [N_HOME-1:0]           m_rready
);
    localparam integer STRB_W = DATA_W/8;

    wire [N_HOME*SID_W-1:0]  x_awid, x_arid, x_bid, x_rid;
    wire [N_HOME*ADDR_W-1:0] x_awaddr, x_araddr;
    wire [N_HOME*8-1:0]      x_awlen, x_arlen;
    wire [N_HOME*3-1:0]      x_awsize, x_arsize;
    wire [N_HOME*2-1:0]      x_awburst, x_arburst, x_bresp, x_rresp;
    wire [N_HOME-1:0]        x_awvalid, x_awready, x_arvalid, x_arready;
    wire [N_HOME*DATA_W-1:0] x_wdata, x_rdata;
    wire [N_HOME*STRB_W-1:0] x_wstrb;
    wire [N_HOME-1:0]        x_wlast, x_wvalid, x_wready;
    wire [N_HOME-1:0]        x_bvalid, x_bready, x_rvalid, x_rready, x_rlast;

    kaxi_xbar3 #(.M(M), .N_HOME(N_HOME), .ADDR_W(ADDR_W), .DATA_W(DATA_W),
                 .ID_W(ID_W), .HOME_LSB(HOME_LSB)) u_xbar (
        .clk(clk), .resetn(resetn),
        .s_awid(s_awid), .s_awaddr(s_awaddr), .s_awlen(s_awlen), .s_awsize(s_awsize),
        .s_awburst(s_awburst), .s_awvalid(s_awvalid), .s_awready(s_awready),
        .s_wdata(s_wdata), .s_wstrb(s_wstrb), .s_wlast(s_wlast), .s_wvalid(s_wvalid),
        .s_wready(s_wready),
        .s_bid(s_bid), .s_bresp(s_bresp), .s_bvalid(s_bvalid), .s_bready(s_bready),
        .s_arid(s_arid), .s_araddr(s_araddr), .s_arlen(s_arlen), .s_arsize(s_arsize),
        .s_arburst(s_arburst), .s_arvalid(s_arvalid), .s_arready(s_arready),
        .s_rid(s_rid), .s_rdata(s_rdata), .s_rresp(s_rresp), .s_rlast(s_rlast),
        .s_rvalid(s_rvalid), .s_rready(s_rready),
        .m_awid(x_awid), .m_awaddr(x_awaddr), .m_awlen(x_awlen), .m_awsize(x_awsize),
        .m_awburst(x_awburst), .m_awvalid(x_awvalid), .m_awready(x_awready),
        .m_wdata(x_wdata), .m_wstrb(x_wstrb), .m_wlast(x_wlast), .m_wvalid(x_wvalid),
        .m_wready(x_wready),
        .m_bid(x_bid), .m_bresp(x_bresp), .m_bvalid(x_bvalid), .m_bready(x_bready),
        .m_arid(x_arid), .m_araddr(x_araddr), .m_arlen(x_arlen), .m_arsize(x_arsize),
        .m_arburst(x_arburst), .m_arvalid(x_arvalid), .m_arready(x_arready),
        .m_rid(x_rid), .m_rdata(x_rdata), .m_rresp(x_rresp), .m_rlast(x_rlast),
        .m_rvalid(x_rvalid), .m_rready(x_rready)
    );

    genvar h;
    generate for (h = 0; h < N_HOME; h = h + 1) begin : g_l3
        kaxi_l3 #(.ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(SID_W), .SETS(L3_SETS),
                  .LINE_LSB(L3_LSB), .SET_W(L3_SET_W), .RAM_STYLE(L3_RAM)) u_l3 (
            .clk(clk), .resetn(resetn),
            .s_awid(x_awid[h*SID_W +: SID_W]), .s_awaddr(x_awaddr[h*ADDR_W +: ADDR_W]),
            .s_awlen(x_awlen[h*8 +: 8]), .s_awsize(x_awsize[h*3 +: 3]),
            .s_awburst(x_awburst[h*2 +: 2]), .s_awvalid(x_awvalid[h]),
            .s_awready(x_awready[h]),
            .s_wdata(x_wdata[h*DATA_W +: DATA_W]), .s_wstrb(x_wstrb[h*STRB_W +: STRB_W]),
            .s_wlast(x_wlast[h]), .s_wvalid(x_wvalid[h]), .s_wready(x_wready[h]),
            .s_bid(x_bid[h*SID_W +: SID_W]), .s_bresp(x_bresp[h*2 +: 2]),
            .s_bvalid(x_bvalid[h]), .s_bready(x_bready[h]),
            .s_arid(x_arid[h*SID_W +: SID_W]), .s_araddr(x_araddr[h*ADDR_W +: ADDR_W]),
            .s_arlen(x_arlen[h*8 +: 8]), .s_arsize(x_arsize[h*3 +: 3]),
            .s_arburst(x_arburst[h*2 +: 2]), .s_arvalid(x_arvalid[h]),
            .s_arready(x_arready[h]),
            .s_rid(x_rid[h*SID_W +: SID_W]), .s_rdata(x_rdata[h*DATA_W +: DATA_W]),
            .s_rresp(x_rresp[h*2 +: 2]), .s_rlast(x_rlast[h]), .s_rvalid(x_rvalid[h]),
            .s_rready(x_rready[h]),
            .m_awid(m_awid[h*SID_W +: SID_W]), .m_awaddr(m_awaddr[h*ADDR_W +: ADDR_W]),
            .m_awlen(m_awlen[h*8 +: 8]), .m_awsize(m_awsize[h*3 +: 3]),
            .m_awburst(m_awburst[h*2 +: 2]), .m_awvalid(m_awvalid[h]),
            .m_awready(m_awready[h]),
            .m_wdata(m_wdata[h*DATA_W +: DATA_W]), .m_wstrb(m_wstrb[h*STRB_W +: STRB_W]),
            .m_wlast(m_wlast[h]), .m_wvalid(m_wvalid[h]), .m_wready(m_wready[h]),
            .m_bid(m_bid[h*SID_W +: SID_W]), .m_bresp(m_bresp[h*2 +: 2]),
            .m_bvalid(m_bvalid[h]), .m_bready(m_bready[h]),
            .m_arid(m_arid[h*SID_W +: SID_W]), .m_araddr(m_araddr[h*ADDR_W +: ADDR_W]),
            .m_arlen(m_arlen[h*8 +: 8]), .m_arsize(m_arsize[h*3 +: 3]),
            .m_arburst(m_arburst[h*2 +: 2]), .m_arvalid(m_arvalid[h]),
            .m_arready(m_arready[h]),
            .m_rid(m_rid[h*SID_W +: SID_W]), .m_rdata(m_rdata[h*DATA_W +: DATA_W]),
            .m_rresp(m_rresp[h*2 +: 2]), .m_rlast(m_rlast[h]), .m_rvalid(m_rvalid[h]),
            .m_rready(m_rready[h])
        );
    end endgenerate
endmodule

`default_nettype wire

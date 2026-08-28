// Configurable M x N crossbar: read and write modes set INDEPENDENTLY.
//   WR_MODE / RD_MODE : 0 = SASD (one shared lane), 1 = SAMD (N parallel lanes)
// All four combinations from one module (kaxi_wr + kaxi_rd). Same ports as
// kaxi_xbar2/3/4. WR=0,RD=1 == xbar3 (SASD-wr/SAMD-rd); WR=1,RD=1 == full SAMD.

`default_nettype none

module kaxi_xbar5 #(
    parameter integer M        = 4,
    parameter integer N_HOME   = 4,
    parameter integer ADDR_W   = 40,
    parameter integer DATA_W   = 512,
    parameter integer ID_W     = 4,
    parameter integer HOME_LSB = 32,
    parameter integer WR_MODE  = 1,
    parameter integer RD_MODE  = 1,
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
    kaxi_wr #(.M(M), .N_HOME(N_HOME), .ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(ID_W),
              .HOME_LSB(HOME_LSB), .MODE(WR_MODE)) u_wr (
        .clk(clk), .resetn(resetn),
        .s_awid(s_awid), .s_awaddr(s_awaddr), .s_awlen(s_awlen), .s_awsize(s_awsize),
        .s_awburst(s_awburst), .s_awvalid(s_awvalid), .s_awready(s_awready),
        .s_wdata(s_wdata), .s_wstrb(s_wstrb), .s_wlast(s_wlast),
        .s_wvalid(s_wvalid), .s_wready(s_wready),
        .s_bid(s_bid), .s_bresp(s_bresp), .s_bvalid(s_bvalid), .s_bready(s_bready),
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen), .m_awsize(m_awsize),
        .m_awburst(m_awburst), .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(m_bvalid), .m_bready(m_bready)
    );
    kaxi_rd #(.M(M), .N_HOME(N_HOME), .ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(ID_W),
              .HOME_LSB(HOME_LSB), .MODE(RD_MODE)) u_rd (
        .clk(clk), .resetn(resetn),
        .s_arid(s_arid), .s_araddr(s_araddr), .s_arlen(s_arlen), .s_arsize(s_arsize),
        .s_arburst(s_arburst), .s_arvalid(s_arvalid), .s_arready(s_arready),
        .s_rid(s_rid), .s_rdata(s_rdata), .s_rresp(s_rresp), .s_rlast(s_rlast),
        .s_rvalid(s_rvalid), .s_rready(s_rready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen), .m_arsize(m_arsize),
        .m_arburst(m_arburst), .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_rvalid(m_rvalid), .m_rready(m_rready)
    );
endmodule

`default_nettype wire

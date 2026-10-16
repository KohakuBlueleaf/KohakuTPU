// kx_pxache_lk -- DIAGNOSTIC: kx_pxache_tb's TB_LOCKSTEP twin, the Xache netlist
// (top XACHE_LK_TOP; submodules renamed so it compiles beside the RTL).

`default_nettype none

module kx_pxache_lk #(
    parameter integer P         = 4,
    parameter integer M         = 4,
    parameter integer N_HOME    = 4,
    parameter integer AW        = 40,
    parameter integer W         = 512,
    parameter integer ID_W      = 4,
    parameter integer MIDX_W    = (M <= 1) ? 1 : $clog2(M),
    parameter integer IDW       = ID_W + MIDX_W,
    parameter integer STRB      = W/8
)(
    input  wire [P-1:0]            clk_p,
    input  wire [P-1:0]            rstn_p,
    input  wire [N_HOME-1:0]       h_clk,
    input  wire [N_HOME-1:0]       h_rstn,

    input  wire [M*ID_W-1:0]       s_awid,
    input  wire [M*AW-1:0]         s_awaddr,
    input  wire [M*8-1:0]          s_awlen,
    input  wire [M*3-1:0]          s_awsize,
    input  wire [M*2-1:0]          s_awburst,
    input  wire [M-1:0]            s_awvalid,
    output wire [M-1:0]            s_awready,
    input  wire [M*W-1:0]          s_wdata,
    input  wire [M*STRB-1:0]       s_wstrb,
    input  wire [M-1:0]            s_wlast,
    input  wire [M-1:0]            s_wvalid,
    output wire [M-1:0]            s_wready,
    output wire [M*ID_W-1:0]       s_bid,
    output wire [M*2-1:0]          s_bresp,
    output wire [M-1:0]            s_bvalid,
    input  wire [M-1:0]            s_bready,
    input  wire [M*ID_W-1:0]       s_arid,
    input  wire [M*AW-1:0]         s_araddr,
    input  wire [M*8-1:0]          s_arlen,
    input  wire [M*3-1:0]          s_arsize,
    input  wire [M*2-1:0]          s_arburst,
    input  wire [M-1:0]            s_arvalid,
    output wire [M-1:0]            s_arready,
    output wire [M*ID_W-1:0]       s_rid,
    output wire [M*W-1:0]          s_rdata,
    output wire [M*2-1:0]          s_rresp,
    output wire [M-1:0]            s_rlast,
    output wire [M-1:0]            s_rvalid,
    input  wire [M-1:0]            s_rready,

    output wire [N_HOME*IDW-1:0]   d_awid,
    output wire [N_HOME*AW-1:0]    d_awaddr,
    output wire [N_HOME*8-1:0]     d_awlen,
    output wire [N_HOME*3-1:0]     d_awsize,
    output wire [N_HOME*2-1:0]     d_awburst,
    output wire [N_HOME-1:0]       d_awvalid,
    input  wire [N_HOME-1:0]       d_awready,
    output wire [N_HOME*W-1:0]     d_wdata,
    output wire [N_HOME*STRB-1:0]  d_wstrb,
    output wire [N_HOME-1:0]       d_wlast,
    output wire [N_HOME-1:0]       d_wvalid,
    input  wire [N_HOME-1:0]       d_wready,
    input  wire [N_HOME*IDW-1:0]   d_bid,
    input  wire [N_HOME*2-1:0]     d_bresp,
    input  wire [N_HOME-1:0]       d_bvalid,
    output wire [N_HOME-1:0]       d_bready,
    output wire [N_HOME*IDW-1:0]   d_arid,
    output wire [N_HOME*AW-1:0]    d_araddr,
    output wire [N_HOME*8-1:0]     d_arlen,
    output wire [N_HOME*3-1:0]     d_arsize,
    output wire [N_HOME*2-1:0]     d_arburst,
    output wire [N_HOME-1:0]       d_arvalid,
    input  wire [N_HOME-1:0]       d_arready,
    input  wire [N_HOME*IDW-1:0]   d_rid,
    input  wire [N_HOME*W-1:0]     d_rdata,
    input  wire [N_HOME*2-1:0]     d_rresp,
    input  wire [N_HOME-1:0]       d_rlast,
    input  wire [N_HOME-1:0]       d_rvalid,
    output wire [N_HOME-1:0]       d_rready
);
`define KX_S(i) \
        .S0``i``_AXI_awid(s_awid[i*ID_W +: ID_W]), .S0``i``_AXI_awaddr(s_awaddr[i*AW +: AW]), \
        .S0``i``_AXI_awlen(s_awlen[i*8 +: 8]), .S0``i``_AXI_awsize(s_awsize[i*3 +: 3]), \
        .S0``i``_AXI_awburst(s_awburst[i*2 +: 2]), .S0``i``_AXI_awvalid(s_awvalid[i]), \
        .S0``i``_AXI_awready(s_awready[i]), .S0``i``_AXI_wdata(s_wdata[i*W +: W]), \
        .S0``i``_AXI_wstrb(s_wstrb[i*STRB +: STRB]), .S0``i``_AXI_wlast(s_wlast[i]), \
        .S0``i``_AXI_wvalid(s_wvalid[i]), .S0``i``_AXI_wready(s_wready[i]), \
        .S0``i``_AXI_bid(s_bid[i*ID_W +: ID_W]), .S0``i``_AXI_bresp(s_bresp[i*2 +: 2]), \
        .S0``i``_AXI_bvalid(s_bvalid[i]), .S0``i``_AXI_bready(s_bready[i]), \
        .S0``i``_AXI_arid(s_arid[i*ID_W +: ID_W]), .S0``i``_AXI_araddr(s_araddr[i*AW +: AW]), \
        .S0``i``_AXI_arlen(s_arlen[i*8 +: 8]), .S0``i``_AXI_arsize(s_arsize[i*3 +: 3]), \
        .S0``i``_AXI_arburst(s_arburst[i*2 +: 2]), .S0``i``_AXI_arvalid(s_arvalid[i]), \
        .S0``i``_AXI_arready(s_arready[i]), .S0``i``_AXI_rid(s_rid[i*ID_W +: ID_W]), \
        .S0``i``_AXI_rdata(s_rdata[i*W +: W]), .S0``i``_AXI_rresp(s_rresp[i*2 +: 2]), \
        .S0``i``_AXI_rlast(s_rlast[i]), .S0``i``_AXI_rvalid(s_rvalid[i]), \
        .S0``i``_AXI_rready(s_rready[i]),
`define KX_M(i) \
        .M0``i``_AXI_awid(d_awid[i*IDW +: IDW]), .M0``i``_AXI_awaddr(d_awaddr[i*AW +: AW]), \
        .M0``i``_AXI_awlen(d_awlen[i*8 +: 8]), .M0``i``_AXI_awsize(d_awsize[i*3 +: 3]), \
        .M0``i``_AXI_awburst(d_awburst[i*2 +: 2]), .M0``i``_AXI_awvalid(d_awvalid[i]), \
        .M0``i``_AXI_awready(d_awready[i]), .M0``i``_AXI_wdata(d_wdata[i*W +: W]), \
        .M0``i``_AXI_wstrb(d_wstrb[i*STRB +: STRB]), .M0``i``_AXI_wlast(d_wlast[i]), \
        .M0``i``_AXI_wvalid(d_wvalid[i]), .M0``i``_AXI_wready(d_wready[i]), \
        .M0``i``_AXI_bid(d_bid[i*IDW +: IDW]), .M0``i``_AXI_bresp(d_bresp[i*2 +: 2]), \
        .M0``i``_AXI_bvalid(d_bvalid[i]), .M0``i``_AXI_bready(d_bready[i]), \
        .M0``i``_AXI_arid(d_arid[i*IDW +: IDW]), .M0``i``_AXI_araddr(d_araddr[i*AW +: AW]), \
        .M0``i``_AXI_arlen(d_arlen[i*8 +: 8]), .M0``i``_AXI_arsize(d_arsize[i*3 +: 3]), \
        .M0``i``_AXI_arburst(d_arburst[i*2 +: 2]), .M0``i``_AXI_arvalid(d_arvalid[i]), \
        .M0``i``_AXI_arready(d_arready[i]), .M0``i``_AXI_rid(d_rid[i*IDW +: IDW]), \
        .M0``i``_AXI_rdata(d_rdata[i*W +: W]), .M0``i``_AXI_rresp(d_rresp[i*2 +: 2]), \
        .M0``i``_AXI_rlast(d_rlast[i]), .M0``i``_AXI_rvalid(d_rvalid[i]), \
        .M0``i``_AXI_rready(d_rready[i]),
`ifndef XACHE_LK_TOP
`define XACHE_LK_TOP multimesh_v9t5_xache_0
`endif
    `XACHE_LK_TOP u_net (
        `KX_S(0) `KX_S(1) `KX_S(2) `KX_S(3)
        `KX_M(0) `KX_M(1) `KX_M(2) `KX_M(3)
        .p_clk0(clk_p[0]), .p_clk1(clk_p[1]), .p_clk2(clk_p[2]), .p_clk3(clk_p[3]),
        .d_rstn0(rstn_p[0]), .d_rstn1(rstn_p[1]), .d_rstn2(rstn_p[2]), .d_rstn3(rstn_p[3]),
        .h_clk0(h_clk[0]), .h_clk1(h_clk[1]), .h_clk2(h_clk[2]), .h_clk3(h_clk[3]),
        .h_rstn0(h_rstn[0]), .h_rstn1(h_rstn[1]), .h_rstn2(h_rstn[2]), .h_rstn3(h_rstn[3]));
`undef KX_S
`undef KX_M
endmodule

`default_nettype wire

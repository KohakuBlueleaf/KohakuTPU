// ktpu_quad_ref -- the both-sided reference: four system nodes (ILINK on),
// one per die, chained through kts_pipe_bd, and with XACHE the KTS-line
// Kohaku Xache on their DRAM path -- the v8t3 memory core without stations.
// A 1-node run cannot see the pull of terminating a link on EACH side.

`default_nettype none

module ktpu_quad_ref #(
    parameter integer N        = 4,
    parameter integer FW       = 288,
    parameter integer PW       = 4,
    parameter integer DW       = 256,
    parameter integer AW       = 40,
    parameter integer IDW      = 4,
    parameter integer MW       = 512,
    parameter integer PORTS    = 2,
    parameter integer L2_MAG_BANKS   = 4,
    parameter integer L2_MAG_ENTRIES = 16384,
    parameter integer XACHE    = 1,
    parameter integer KX_SETS  = 16384,
    parameter integer KX_SET_W = 14,
    parameter integer KX_K     = 2,
    parameter integer KX_BANKS = 4,
    parameter integer LKW      = 288,
    parameter integer LKC      = 4,
    parameter integer IL_STAGES = 1
)(
    input  wire               clk,
    input  wire [N-1:0]       rstn,          // one registered copy per die

    input  wire [N*IDW-1:0]   s_mem_awid,
    input  wire [N*AW-1:0]    s_mem_awaddr,
    input  wire [N*8-1:0]     s_mem_awlen,
    input  wire [N-1:0]       s_mem_awvalid,
    output wire [N-1:0]       s_mem_awready,
    input  wire [N*DW-1:0]    s_mem_wdata,
    input  wire [N*DW/8-1:0]  s_mem_wstrb,
    input  wire [N-1:0]       s_mem_wlast,
    input  wire [N-1:0]       s_mem_wvalid,
    output wire [N-1:0]       s_mem_wready,
    output wire [N*IDW-1:0]   s_mem_bid,
    output wire [N*2-1:0]     s_mem_bresp,
    output wire [N-1:0]       s_mem_bvalid,
    input  wire [N-1:0]       s_mem_bready,
    input  wire [N*IDW-1:0]   s_mem_arid,
    input  wire [N*AW-1:0]    s_mem_araddr,
    input  wire [N*8-1:0]     s_mem_arlen,
    input  wire [N-1:0]       s_mem_arvalid,
    output wire [N-1:0]       s_mem_arready,
    output wire [N*IDW-1:0]   s_mem_rid,
    output wire [N*DW-1:0]    s_mem_rdata,
    output wire [N*2-1:0]     s_mem_rresp,
    output wire [N-1:0]       s_mem_rlast,
    output wire [N-1:0]       s_mem_rvalid,
    input  wire [N-1:0]       s_mem_rready,

    input  wire [N*IDW-1:0]   s_ctl_awid,
    input  wire [N*32-1:0]    s_ctl_awaddr,
    input  wire [N*8-1:0]     s_ctl_awlen,
    input  wire [N-1:0]       s_ctl_awvalid,
    output wire [N-1:0]       s_ctl_awready,
    input  wire [N*64-1:0]    s_ctl_wdata,
    input  wire [N*8-1:0]     s_ctl_wstrb,
    input  wire [N-1:0]       s_ctl_wlast,
    input  wire [N-1:0]       s_ctl_wvalid,
    output wire [N-1:0]       s_ctl_wready,
    output wire [N*IDW-1:0]   s_ctl_bid,
    output wire [N*2-1:0]     s_ctl_bresp,
    output wire [N-1:0]       s_ctl_bvalid,
    input  wire [N-1:0]       s_ctl_bready,
    input  wire [N*IDW-1:0]   s_ctl_arid,
    input  wire [N*32-1:0]    s_ctl_araddr,
    input  wire [N*8-1:0]     s_ctl_arlen,
    input  wire [N-1:0]       s_ctl_arvalid,
    output wire [N-1:0]       s_ctl_arready,
    output wire [N*IDW-1:0]   s_ctl_rid,
    output wire [N*64-1:0]    s_ctl_rdata,
    output wire [N*2-1:0]     s_ctl_rresp,
    output wire [N-1:0]       s_ctl_rlast,
    output wire [N-1:0]       s_ctl_rvalid,
    input  wire [N-1:0]       s_ctl_rready,

    // XACHE=0: the nodes' DRAM masters; XACHE=1: the Xache's DRAM masters
    output wire [N*(IDW+2)-1:0] d_awid,
    output wire [N*AW-1:0]    d_awaddr,
    output wire [N*8-1:0]     d_awlen,
    output wire [N*3-1:0]     d_awsize,
    output wire [N*2-1:0]     d_awburst,
    output wire [N-1:0]       d_awvalid,
    input  wire [N-1:0]       d_awready,
    output wire [N*MW-1:0]    d_wdata,
    output wire [N*MW/8-1:0]  d_wstrb,
    output wire [N-1:0]       d_wlast,
    output wire [N-1:0]       d_wvalid,
    input  wire [N-1:0]       d_wready,
    input  wire [N*(IDW+2)-1:0] d_bid,
    input  wire [N*2-1:0]     d_bresp,
    input  wire [N-1:0]       d_bvalid,
    output wire [N-1:0]       d_bready,
    output wire [N*(IDW+2)-1:0] d_arid,
    output wire [N*AW-1:0]    d_araddr,
    output wire [N*8-1:0]     d_arlen,
    output wire [N*3-1:0]     d_arsize,
    output wire [N*2-1:0]     d_arburst,
    output wire [N-1:0]       d_arvalid,
    input  wire [N-1:0]       d_arready,
    input  wire [N*(IDW+2)-1:0] d_rid,
    input  wire [N*MW-1:0]    d_rdata,
    input  wire [N*2-1:0]     d_rresp,
    input  wire [N-1:0]       d_rlast,
    input  wire [N-1:0]       d_rvalid,
    output wire [N-1:0]       d_rready
);
    // node n's dram master, either exported or into the xache
    wire [N*IDW-1:0]   n_awid, n_arid, n_bid, n_rid;
    wire [N*AW-1:0]    n_awaddr, n_araddr;
    wire [N*8-1:0]     n_awlen, n_arlen;
    wire [N*3-1:0]     n_awsize, n_arsize;
    wire [N*2-1:0]     n_awburst, n_arburst, n_bresp, n_rresp;
    wire [N-1:0]       n_awvalid, n_awready, n_wvalid, n_wready, n_wlast;
    wire [N-1:0]       n_arvalid, n_arready, n_rvalid, n_rready, n_rlast;
    wire [N-1:0]       n_bvalid, n_bready;
    wire [N*MW-1:0]    n_wdata, n_rdata;
    wire [N*MW/8-1:0]  n_wstrb;

    // the interlink chain: node n's LINK1 up to node n+1's LINK0
    wire [N-1:0]       u_v, u_vc, u_l, u_cv, u_cvc;
    wire [N*LKW-1:0]   u_f;
    wire [N*LKC-1:0]   u_cn;
    wire [N-1:0]       dwn_v, dwn_vc, dwn_l, dwn_cv, dwn_cvc;
    wire [N*LKW-1:0]   dwn_f;
    wire [N*LKC-1:0]   dwn_cn;

    genvar n;
    generate for (n = 0; n < N; n = n + 1) begin : g_n
        wire l0_iv,  l0_ivc,  l0_il;
        wire [LKW-1:0] l0_if;
        wire l0_ocv, l0_ocvc;
        wire [LKC-1:0] l0_ocn;
        wire l0_ov,  l0_ovc,  l0_ol;
        wire [LKW-1:0] l0_of;
        wire l0_icv, l0_icvc;
        wire [LKC-1:0] l0_icn;
        wire l1_iv,  l1_ivc,  l1_il;
        wire [LKW-1:0] l1_if;
        wire l1_ocv, l1_ocvc;
        wire [LKC-1:0] l1_ocn;
        wire l1_ov,  l1_ovc,  l1_ol;
        wire [LKW-1:0] l1_of;
        wire l1_icv, l1_icvc;
        wire [LKC-1:0] l1_icn;
        if (n == 0) begin : g_end0
            assign l0_iv = 1'b0; assign l0_ivc = 1'b0; assign l0_il = 1'b0;
            assign l0_if = {LKW{1'b0}};
            assign l0_icv = 1'b0; assign l0_icvc = 1'b0; assign l0_icn = {LKC{1'b0}};
        end else begin : g_dn
            kts_pipe_bd #(.W(LKW), .VCW(1), .CN_W(LKC), .STAGES(IL_STAGES)) u_pu (
                .clk(clk), .clk_rx(clk),
                .rstn_tx(rstn[n-1]), .rstn_rx(rstn[n]),
                .i_valid(u_v[n-1]), .i_vc(u_vc[n-1]), .i_last(u_l[n-1]),
                .i_flit(u_f[(n-1)*LKW +: LKW]),
                .o_valid(l0_iv), .o_vc(l0_ivc), .o_last(l0_il), .o_flit(l0_if),
                .i_crd_valid(l0_ocv), .i_crd_vc(l0_ocvc), .i_crd_n(l0_ocn),
                .o_crd_valid(u_cv[n-1]), .o_crd_vc(u_cvc[n-1]),
                .o_crd_n(u_cn[(n-1)*LKC +: LKC]));
            kts_pipe_bd #(.W(LKW), .VCW(1), .CN_W(LKC), .STAGES(IL_STAGES)) u_pd (
                .clk(clk), .clk_rx(clk),
                .rstn_tx(rstn[n]), .rstn_rx(rstn[n-1]),
                .i_valid(l0_ov), .i_vc(l0_ovc), .i_last(l0_ol), .i_flit(l0_of),
                .o_valid(dwn_v[n-1]), .o_vc(dwn_vc[n-1]), .o_last(dwn_l[n-1]),
                .o_flit(dwn_f[(n-1)*LKW +: LKW]),
                .i_crd_valid(dwn_cv[n-1]), .i_crd_vc(dwn_cvc[n-1]),
                .i_crd_n(dwn_cn[(n-1)*LKC +: LKC]),
                .o_crd_valid(l0_icv), .o_crd_vc(l0_icvc), .o_crd_n(l0_icn));
        end
        if (n == N - 1) begin : g_endl
            assign l1_iv = 1'b0; assign l1_ivc = 1'b0; assign l1_il = 1'b0;
            assign l1_if = {LKW{1'b0}};
            assign l1_icv = 1'b0; assign l1_icvc = 1'b0; assign l1_icn = {LKC{1'b0}};
        end else begin : g_up
            assign u_v[n] = l1_ov;  assign u_vc[n] = l1_ovc;
            assign u_l[n] = l1_ol;  assign u_f[n*LKW +: LKW] = l1_of;
            assign l1_icv = u_cv[n]; assign l1_icvc = u_cvc[n];
            assign l1_icn = u_cn[n*LKC +: LKC];
            assign l1_iv = dwn_v[n]; assign l1_ivc = dwn_vc[n];
            assign l1_il = dwn_l[n]; assign l1_if = dwn_f[n*LKW +: LKW];
            assign dwn_cv[n] = l1_ocv; assign dwn_cvc[n] = l1_ocvc;
            assign dwn_cn[n*LKC +: LKC] = l1_ocn;
        end

        wire [PORTS*FW-1:0] mo_d;
        wire [PORTS-1:0]    mo_v, mi_b;
        wire [15:0]         mrd, mwr;
        wire        mvb;
        wire [3:0]  mvf;
        wire [31:0] mvd;
        wire [63:0] pes, hsr;
        wire        peb, hcw;
        wire [7:0]  hcs;
        sysnode #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DATA_W(DW), .ADDR_W(AW),
                  .ID_W(IDW), .PORTS(PORTS), .MEM_X(0), .MEM_Y(1), .MEM_X1(0),
                  .MEM_Y1(2), .GRID_LO(1), .GRID_HI(2), .STAGE_FLITS(128),
                  .ILINK(1), .MESH_ID(n), .LINK_W(LKW), .TUSER_W(96),
                  .MW(MW), .DRAM_CDC(0),
                  .STAGE(1), .STAGE_BANKS(L2_MAG_BANKS),
                  .STAGE_ENTRIES(L2_MAG_ENTRIES), .STAGE_AT_PORT(1)) u_mag (
            .clk(clk), .resetn(rstn[n]),
            .dram_aclk(clk), .dram_aresetn(rstn[n]),
            .sm_awid(s_mem_awid[n*IDW +: IDW]), .sm_awaddr(s_mem_awaddr[n*AW +: AW]),
            .sm_awlen(s_mem_awlen[n*8 +: 8]), .sm_awvalid(s_mem_awvalid[n]),
            .sm_awready(s_mem_awready[n]),
            .sm_wdata(s_mem_wdata[n*DW +: DW]), .sm_wstrb(s_mem_wstrb[n*DW/8 +: DW/8]),
            .sm_wlast(s_mem_wlast[n]), .sm_wvalid(s_mem_wvalid[n]),
            .sm_wready(s_mem_wready[n]),
            .sm_bid(s_mem_bid[n*IDW +: IDW]), .sm_bresp(s_mem_bresp[n*2 +: 2]),
            .sm_bvalid(s_mem_bvalid[n]), .sm_bready(s_mem_bready[n]),
            .sm_arid(s_mem_arid[n*IDW +: IDW]), .sm_araddr(s_mem_araddr[n*AW +: AW]),
            .sm_arlen(s_mem_arlen[n*8 +: 8]), .sm_arvalid(s_mem_arvalid[n]),
            .sm_arready(s_mem_arready[n]),
            .sm_rid(s_mem_rid[n*IDW +: IDW]), .sm_rdata(s_mem_rdata[n*DW +: DW]),
            .sm_rresp(s_mem_rresp[n*2 +: 2]), .sm_rlast(s_mem_rlast[n]),
            .sm_rvalid(s_mem_rvalid[n]), .sm_rready(s_mem_rready[n]),
            .sc_awid(s_ctl_awid[n*IDW +: IDW]), .sc_awaddr(s_ctl_awaddr[n*32 +: 32]),
            .sc_awlen(s_ctl_awlen[n*8 +: 8]), .sc_awvalid(s_ctl_awvalid[n]),
            .sc_awready(s_ctl_awready[n]),
            .sc_wdata(s_ctl_wdata[n*64 +: 64]), .sc_wstrb(s_ctl_wstrb[n*8 +: 8]),
            .sc_wlast(s_ctl_wlast[n]), .sc_wvalid(s_ctl_wvalid[n]),
            .sc_wready(s_ctl_wready[n]),
            .sc_bid(s_ctl_bid[n*IDW +: IDW]), .sc_bresp(s_ctl_bresp[n*2 +: 2]),
            .sc_bvalid(s_ctl_bvalid[n]), .sc_bready(s_ctl_bready[n]),
            .sc_arid(s_ctl_arid[n*IDW +: IDW]), .sc_araddr(s_ctl_araddr[n*32 +: 32]),
            .sc_arlen(s_ctl_arlen[n*8 +: 8]), .sc_arvalid(s_ctl_arvalid[n]),
            .sc_arready(s_ctl_arready[n]),
            .sc_rid(s_ctl_rid[n*IDW +: IDW]), .sc_rdata(s_ctl_rdata[n*64 +: 64]),
            .sc_rresp(s_ctl_rresp[n*2 +: 2]), .sc_rlast(s_ctl_rlast[n]),
            .sc_rvalid(s_ctl_rvalid[n]), .sc_rready(s_ctl_rready[n]),
            .dram_awid(n_awid[n*IDW +: IDW]), .dram_awaddr(n_awaddr[n*AW +: AW]),
            .dram_awlen(n_awlen[n*8 +: 8]), .dram_awsize(n_awsize[n*3 +: 3]),
            .dram_awburst(n_awburst[n*2 +: 2]), .dram_awvalid(n_awvalid[n]),
            .dram_awready(n_awready[n]),
            .dram_wdata(n_wdata[n*MW +: MW]), .dram_wstrb(n_wstrb[n*MW/8 +: MW/8]),
            .dram_wlast(n_wlast[n]), .dram_wvalid(n_wvalid[n]),
            .dram_wready(n_wready[n]),
            .dram_bid(n_bid[n*IDW +: IDW]), .dram_bresp(n_bresp[n*2 +: 2]),
            .dram_bvalid(n_bvalid[n]), .dram_bready(n_bready[n]),
            .dram_arid(n_arid[n*IDW +: IDW]), .dram_araddr(n_araddr[n*AW +: AW]),
            .dram_arlen(n_arlen[n*8 +: 8]), .dram_arsize(n_arsize[n*3 +: 3]),
            .dram_arburst(n_arburst[n*2 +: 2]), .dram_arvalid(n_arvalid[n]),
            .dram_arready(n_arready[n]),
            .dram_rid(n_rid[n*IDW +: IDW]), .dram_rdata(n_rdata[n*MW +: MW]),
            .dram_rresp(n_rresp[n*2 +: 2]), .dram_rlast(n_rlast[n]),
            .dram_rvalid(n_rvalid[n]), .dram_rready(n_rready[n]),
            .mem_in_data({PORTS*FW{1'b0}}), .mem_in_valid({PORTS{1'b0}}),
            .mem_in_busy(mi_b),
            .mem_out_data(mo_d), .mem_out_valid(mo_v),
            .mem_out_busy({PORTS{1'b0}}),
            .mem_rd_count(mrd), .mem_wr_count(mwr),
            .mv_busy(mvb), .mv_fault(mvf), .mv_done(mvd),
            .pe_halt_req(1'b0), .pe_status(pes), .pe_busy(peb),
            .hs_addr(32'd0), .hs_wr(1'b0), .hs_wdata(64'd0), .hs_wstrb(8'd0),
            .hs_rd(1'b0), .hs_rdata(hsr), .hs_console_we(hcw), .hs_console(hcs),
            .link0_out_valid(l0_ov), .link0_out_vc(l0_ovc),
            .link0_out_last(l0_ol), .link0_out_flit(l0_of),
            .link0_out_crd_valid(l0_icv), .link0_out_crd_vc(l0_icvc),
            .link0_out_crd_n(l0_icn),
            .link0_in_valid(l0_iv), .link0_in_vc(l0_ivc), .link0_in_last(l0_il),
            .link0_in_flit(l0_if), .link0_in_crd_valid(l0_ocv),
            .link0_in_crd_vc(l0_ocvc), .link0_in_crd_n(l0_ocn),
            .link1_out_valid(l1_ov), .link1_out_vc(l1_ovc),
            .link1_out_last(l1_ol), .link1_out_flit(l1_of),
            .link1_out_crd_valid(l1_icv), .link1_out_crd_vc(l1_icvc),
            .link1_out_crd_n(l1_icn),
            .link1_in_valid(l1_iv), .link1_in_vc(l1_ivc), .link1_in_last(l1_il),
            .link1_in_flit(l1_if), .link1_in_crd_valid(l1_ocv),
            .link1_in_crd_vc(l1_ocvc), .link1_in_crd_n(l1_ocn));
    end endgenerate

    generate if (XACHE != 0) begin : g_kx
        kx_pxache #(.P(N), .M(N), .N_HOME(N),
            .MP(8'b11100100), .HP(8'b11100100),
            .AW(AW), .W(MW), .ID_W(IDW), .HOME_LSB(32),
            .SETS(KX_SETS), .SET_W(KX_SET_W), .K(KX_K), .BANKS(KX_BANKS),
            .RING_WR_REG(1), .NSWAP(18),
            .SWAP_A(144'h1f1e1d1c1b1a191817161514131211100f0e),
            .SWAP_B(144'h21201f1e1d1c1b1a19181716151413121110),
            .BND_KTS(1)) u_kx (
            .clk(clk), .rstn_p(rstn),
            .m_clk({N{clk}}), .m_rstn(rstn), .h_clk({N{clk}}), .h_rstn(rstn),
            .s_awid(n_awid), .s_awaddr(n_awaddr), .s_awlen(n_awlen),
            .s_awsize(n_awsize), .s_awburst(n_awburst),
            .s_awvalid(n_awvalid), .s_awready(n_awready),
            .s_wdata(n_wdata), .s_wstrb(n_wstrb), .s_wlast(n_wlast),
            .s_wvalid(n_wvalid), .s_wready(n_wready),
            .s_bid(n_bid), .s_bresp(n_bresp), .s_bvalid(n_bvalid),
            .s_bready(n_bready),
            .s_arid(n_arid), .s_araddr(n_araddr), .s_arlen(n_arlen),
            .s_arsize(n_arsize), .s_arburst(n_arburst),
            .s_arvalid(n_arvalid), .s_arready(n_arready),
            .s_rid(n_rid), .s_rdata(n_rdata), .s_rresp(n_rresp),
            .s_rlast(n_rlast), .s_rvalid(n_rvalid), .s_rready(n_rready),
            .d_awid(d_awid), .d_awaddr(d_awaddr), .d_awlen(d_awlen),
            .d_awsize(d_awsize), .d_awburst(d_awburst),
            .d_awvalid(d_awvalid), .d_awready(d_awready),
            .d_wdata(d_wdata), .d_wstrb(d_wstrb), .d_wlast(d_wlast),
            .d_wvalid(d_wvalid), .d_wready(d_wready),
            .d_bid(d_bid), .d_bresp(d_bresp), .d_bvalid(d_bvalid),
            .d_bready(d_bready),
            .d_arid(d_arid), .d_araddr(d_araddr), .d_arlen(d_arlen),
            .d_arsize(d_arsize), .d_arburst(d_arburst),
            .d_arvalid(d_arvalid), .d_arready(d_arready),
            .d_rid(d_rid), .d_rdata(d_rdata), .d_rresp(d_rresp),
            .d_rlast(d_rlast), .d_rvalid(d_rvalid), .d_rready(d_rready));
    end else begin : g_nx
        genvar x;
        for (x = 0; x < N; x = x + 1) begin : g_x
            assign d_awid[x*(IDW+2) +: IDW+2] = {2'b00, n_awid[x*IDW +: IDW]};
            assign d_arid[x*(IDW+2) +: IDW+2] = {2'b00, n_arid[x*IDW +: IDW]};
            assign n_bid[x*IDW +: IDW] = d_bid[x*(IDW+2) +: IDW];
            assign n_rid[x*IDW +: IDW] = d_rid[x*(IDW+2) +: IDW];
        end
        assign d_awaddr = n_awaddr;  assign d_awlen = n_awlen;
        assign d_awsize = n_awsize;  assign d_awburst = n_awburst;
        assign d_awvalid = n_awvalid; assign n_awready = d_awready;
        assign d_wdata = n_wdata;    assign d_wstrb = n_wstrb;
        assign d_wlast = n_wlast;    assign d_wvalid = n_wvalid;
        assign n_wready = d_wready;
        assign n_bresp = d_bresp;    assign n_bvalid = d_bvalid;
        assign d_bready = n_bready;
        assign d_araddr = n_araddr;  assign d_arlen = n_arlen;
        assign d_arsize = n_arsize;  assign d_arburst = n_arburst;
        assign d_arvalid = n_arvalid; assign n_arready = d_arready;
        assign n_rdata = d_rdata;    assign n_rresp = d_rresp;
        assign n_rlast = d_rlast;    assign n_rvalid = d_rvalid;
        assign d_rready = n_rready;
    end endgenerate
endmodule

`default_nettype wire

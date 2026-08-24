// TARGET 1: two system nodes wired to each other, and a two-core algorithm.
//
//   node A                                    node B
//   processor -> mover -> interlink ------->  DRAM
//                                             processor polls the last word,
//                                             then runs its own mover on it
//
// No mesh between them: each node's memory-port NoC face is looped straight to
// its own processor, so the orchestrator's dispatched flits reach the processor
// with no router in the path. Host access is still ONLY through the station bus.
//
// THE SYNC IS A DOORBELL, NOT PRODUCER-IDLE. B cannot see A retire, and A's
// mover retires on its last write SENT. B polls the LAST word of the transfer
// and invalidates its L1 each time round -- a cached poll never observes the
// arrival, which is what C_INVAL being a blocking store is for.

`timescale 1ns / 1ps
`default_nettype none

module ctrlpe_pair_tb;
    localparam integer AW    = 43;
    localparam integer FW    = 256;
    localparam integer NQ    = 2;
    localparam integer PORTW = 1;
    localparam integer NM    = 3;
    localparam integer NS    = 4 * NQ;
    localparam integer MAXW  = 512;
    localparam integer MAXID = 4;
    localparam integer DSTW  = 2;

    localparam integer DW  = 256;
    localparam integer NAW = 40;
    localparam integer IDW = 4;
    localparam integer MFW = 288;
    // FINAL_OFF >> 5 is 98,304, so a 70,000-word model silently drops the
    // preload and the "nothing moved yet" check reads X.
    localparam integer RAMW = 110000;

    localparam integer NW = 8;
    localparam [NAW-1:0] SRC_OFF   = 40'h10_0000;
    localparam [NAW-1:0] DST_OFF   = 40'h20_0000;   // where A lands in B
    localparam [NAW-1:0] FINAL_OFF = 40'h30_0000;   // where B puts the result

    // The last 32-byte word A sends, and the word B waits on.
    localparam [31:0] SENTINEL = 32'hAC00_0000 | (NW - 1);

    localparam [15:0] A_PROG_DST = 16'h0040, A_PROG_LEN = 16'h0048;
    localparam [15:0] A_PROG_KICK = 16'h0050, A_PROG_CRED = 16'h0060;
    localparam [15:0] A_PROG_BASE = 16'h0068, A_STAGE = 16'h2000;

    integer errors = 0, checks = 0, spin, i, nbad;

    reg bclk0 = 0, bclk1 = 0, bclk2 = 0, bclk3 = 0;
    always begin
        #1.250 bclk0 = ~bclk0;
    end
    always begin
        #1.373 bclk1 = ~bclk1;
    end
    always begin
        #1.611 bclk2 = ~bclk2;
    end
    always begin
        #1.187 bclk3 = ~bclk3;
    end

    reg clk_ctrl = 0, clk_xdma = 0;
    always begin
        #5.000 clk_ctrl = ~clk_ctrl;
    end
    always begin
        #2.000 clk_xdma = ~clk_xdma;
    end

    reg clk_s2 = 0, clk_s3 = 0;
    always begin
        #2.773 clk_s2 = ~clk_s2;
    end
    always begin
        #2.373 clk_s3 = ~clk_s3;
    end

    reg na = 0, nb = 0, da = 0, db = 0;
    always begin
        #2.237 na = ~na;
    end
    always begin
        #1.907 nb = ~nb;
    end
    always begin
        #1.913 da = ~da;
    end
    always begin
        #2.111 db = ~db;
    end

    reg rstn = 0;
    wire bus_rst = !rstn;

    localparam [AW-1:0] FULL      = {AW{1'b1}};
    localparam [AW-1:0] MESH_MASK = {3'b111, {40{1'b0}}};
    localparam [AW-1:0] CTRL_MASK = FULL ^ {{(AW-16){1'b0}}, 16'hFFFF};

    function [AW-1:0] mesh_win;
        input integer id;
        reg [2:0] w;
        begin w = id[2:0] + 3'd1; mesh_win = {w, {40{1'b0}}}; end
    endfunction
    function [AW-1:0] ctrl_win;
        input integer stn;
        reg [2:0] w;
        begin w = stn[2:0] + 3'd1; ctrl_win = {{(AW-19){1'b0}}, w, 16'd0}; end
    endfunction

    localparam [NS*AW-1:0] SEG_BASE = { ctrl_win(3), mesh_win(3), ctrl_win(2), mesh_win(2), ctrl_win(1), mesh_win(1), ctrl_win(0), mesh_win(0) };
    localparam [NS*AW-1:0] SEG_MASK = { CTRL_MASK, MESH_MASK, CTRL_MASK, MESH_MASK, CTRL_MASK, MESH_MASK, CTRL_MASK, MESH_MASK };
    localparam [NS*AW-1:0] SEG_XLT = {NS*AW{1'b0}};
    localparam [NS*DSTW-1:0] SEG_DST = { 2'd3, 2'd3, 2'd2, 2'd2, 2'd1, 2'd1, 2'd0, 2'd0 };
    localparam [NS*DSTW-1:0] SEG_DPORT = { 2'd1, 2'd0, 2'd1, 2'd0, 2'd1, 2'd0, 2'd1, 2'd0 };

    reg  [NM*MAXID-1:0] mp_awid = 0, mp_arid = 0;
    reg  [NM*AW-1:0]    mp_awaddr = 0, mp_araddr = 0;
    reg  [NM*8-1:0]     mp_awlen = 0, mp_arlen = 0;
    reg  [NM*3-1:0]     mp_awsize = 0, mp_arsize = 0;
    reg  [NM-1:0]       mp_awvalid = 0, mp_arvalid = 0;
    reg  [NM*MAXW-1:0]  mp_wdata = 0;
    reg  [NM*(MAXW/8)-1:0] mp_wstrb = 0;
    reg  [NM-1:0]       mp_wlast = 0, mp_wvalid = 0;
    wire [NM-1:0]       mp_awready, mp_wready, mp_arready;
    wire [NM-1:0]       mp_bvalid, mp_rvalid, mp_rlast;
    wire [NM*2-1:0]     mp_bresp, mp_rresp;
    wire [NM*MAXW-1:0]  mp_rdata;
    wire [NM-1:0]       mp_bready = {NM{1'b1}};
    wire [NM-1:0]       mp_rready = {NM{1'b1}};

    wire [NS*MAXID-1:0]    sp_awid, sp_arid;
    wire [NS*AW-1:0]       sp_awaddr, sp_araddr;
    wire [NS*8-1:0]        sp_awlen, sp_arlen;
    wire [NS*3-1:0]        sp_awsize, sp_arsize;
    wire [NS*2-1:0]        sp_awburst, sp_arburst;
    wire [NS-1:0]          sp_awvalid, sp_wvalid, sp_wlast, sp_arvalid;
    wire [NS-1:0]          sp_bready, sp_rready;
    wire [NS*MAXW-1:0]     sp_wdata;
    wire [NS*(MAXW/8)-1:0] sp_wstrb;
    wire [NS-1:0]          sp_awready, sp_wready, sp_bvalid, sp_arready;
    wire [NS-1:0]          sp_rvalid, sp_rlast;
    wire [NS*MAXID-1:0]    sp_bid, sp_rid;
    wire [NS*2-1:0]        sp_bresp, sp_rresp;
    wire [NS*MAXW-1:0]     sp_rdata;
    wire [31:0]            stat_decerr;

    wire [3:0] bclk = {bclk3, bclk2, bclk1, bclk0};
    wire [3:0] sclk = {clk_s3, clk_s2, nb, na};

    sb_line4 #(.AW(AW), .FW(FW), .NQ(NQ), .PORTW(PORTW), .NM(NM),
               .MAXW(MAXW), .MAXID(MAXID), .WIDE_DW(FW),
               .LINK_CDC(1), .LINK_FULL(0), .MGR_STN(1),
               .SEG_OVERRIDE(1), .SEG_BASE_P(SEG_BASE), .SEG_MASK_P(SEG_MASK),
               .SEG_XLT_P(SEG_XLT), .SEG_DST_P(SEG_DST),
               .SEG_DPORT_P(SEG_DPORT), .SEG_VLD_P({NS{1'b1}})) u_bus (
        .bus_clk0(bclk[0]), .bus_rst0(bus_rst),
        .bus_clk1(bclk[1]), .bus_rst1(bus_rst),
        .bus_clk2(bclk[2]), .bus_rst2(bus_rst),
        .bus_clk3(bclk[3]), .bus_rst3(bus_rst),
        .clk_ctrl(clk_ctrl), .aresetn_ctrl(rstn),
        .clk_xdma(clk_xdma), .aresetn_xdma(rstn),
        .clk_s0(sclk[0]), .aresetn_s0(rstn),
        .clk_s1(sclk[1]), .aresetn_s1(rstn),
        .clk_s2(sclk[2]), .aresetn_s2(rstn),
        .clk_s3(sclk[3]), .aresetn_s3(rstn),
        .clk_ddr0(da), .aresetn_ddr0(rstn),
        .clk_ddr1(db), .aresetn_ddr1(rstn),
        .clk_ddr2(clk_s2), .aresetn_ddr2(rstn),
        .clk_ddr3(clk_s3), .aresetn_ddr3(rstn),
        .mp_awid(mp_awid), .mp_awaddr(mp_awaddr), .mp_awlen(mp_awlen),
        .mp_awsize(mp_awsize), .mp_awburst({NM{2'b01}}),
        .mp_awvalid(mp_awvalid), .mp_awready(mp_awready),
        .mp_wdata(mp_wdata), .mp_wstrb(mp_wstrb), .mp_wlast(mp_wlast),
        .mp_wvalid(mp_wvalid), .mp_wready(mp_wready),
        .mp_bid(), .mp_bresp(mp_bresp), .mp_bvalid(mp_bvalid),
        .mp_bready(mp_bready),
        .mp_arid(mp_arid), .mp_araddr(mp_araddr), .mp_arlen(mp_arlen),
        .mp_arsize(mp_arsize), .mp_arburst({NM{2'b01}}),
        .mp_arvalid(mp_arvalid), .mp_arready(mp_arready),
        .mp_rid(), .mp_rdata(mp_rdata), .mp_rresp(mp_rresp),
        .mp_rlast(mp_rlast), .mp_rvalid(mp_rvalid), .mp_rready(mp_rready),
        .sp_awid(sp_awid), .sp_awaddr(sp_awaddr), .sp_awlen(sp_awlen),
        .sp_awsize(sp_awsize), .sp_awburst(sp_awburst),
        .sp_awvalid(sp_awvalid), .sp_awready(sp_awready),
        .sp_wdata(sp_wdata), .sp_wstrb(sp_wstrb), .sp_wlast(sp_wlast),
        .sp_wvalid(sp_wvalid), .sp_wready(sp_wready),
        .sp_bid(sp_bid), .sp_bresp(sp_bresp), .sp_bvalid(sp_bvalid),
        .sp_bready(sp_bready),
        .sp_arid(sp_arid), .sp_araddr(sp_araddr), .sp_arlen(sp_arlen),
        .sp_arsize(sp_arsize), .sp_arburst(sp_arburst),
        .sp_arvalid(sp_arvalid), .sp_arready(sp_arready),
        .sp_rid(sp_rid), .sp_rdata(sp_rdata), .sp_rresp(sp_rresp),
        .sp_rlast(sp_rlast), .sp_rvalid(sp_rvalid), .sp_rready(sp_rready),
        .stat_decerr(stat_decerr)
    );

    // --------------------------------------------------------------- the pair
    wire [IDW-1:0]  dm_awid[0:1], dm_arid[0:1], dm_bid[0:1], dm_rid[0:1];
    wire [NAW-1:0]  dm_awaddr[0:1], dm_araddr[0:1];
    wire [7:0]      dm_awlen[0:1], dm_arlen[0:1];
    wire [2:0]      dm_awsize[0:1], dm_arsize[0:1];
    wire [1:0]      dm_awburst[0:1], dm_arburst[0:1];
    wire [1:0]      dm_bresp[0:1], dm_rresp[0:1];
    wire [1:0]      dm_awvalid, dm_awready, dm_arvalid, dm_arready;
    wire [DW-1:0]   dm_wdata[0:1], dm_rdata[0:1];
    wire [DW/8-1:0] dm_wstrb[0:1];
    wire [1:0]      dm_wlast, dm_wvalid, dm_wready;
    wire [1:0]      dm_bvalid, dm_bready, dm_rlast, dm_rvalid, dm_rready;

    wire [1:0]   pbusy, nbusy;
    wire [287:0] lk_d [0:1][0:1];
    wire [95:0]  lk_u [0:1][0:1];
    wire         lk_l [0:1][0:1];
    wire         lk_v [0:1][0:1];

    wire [IDW-1:0] cs_awid[0:1], cs_arid[0:1], cs_bid[0:1], cs_rid[0:1];
    wire [31:0]    cs_awaddr[0:1], cs_araddr[0:1];
    wire [7:0]     cs_awlen[0:1], cs_arlen[0:1];
    wire [63:0]    cs_wdata[0:1], cs_rdata[0:1];
    wire [7:0]     cs_wstrb[0:1];
    wire [1:0]     cs_bresp[0:1], cs_rresp[0:1];
    wire [1:0]     cs_awvalid, cs_awready, cs_wvalid, cs_wready;
    wire [1:0]     cs_bvalid, cs_arvalid, cs_arready, cs_rvalid, cs_rlast;
    wire [1:0]     cs_wlast;

    genvar g;
    generate for (g = 0; g < 2; g = g + 1) begin : nd
        localparam integer QM = g * NQ;
        localparam integer QC = g * NQ + 1;
        wire nclk = (g == 0) ? na : nb;
        wire dclk = (g == 0) ? da : db;

        axi_up32to64 #(.AW(32), .IDW(IDW)) u_up (
            .clk(nclk), .resetn(rstn),
            .s_awid(sp_awid[QC*MAXID +: IDW]),
            .s_awaddr(sp_awaddr[QC*AW +: 32]),
            .s_awlen(sp_awlen[QC*8 +: 8]), .s_awsize(sp_awsize[QC*3 +: 3]),
            .s_awburst(sp_awburst[QC*2 +: 2]),
            .s_awvalid(sp_awvalid[QC]), .s_awready(sp_awready[QC]),
            .s_wdata(sp_wdata[QC*MAXW +: 32]),
            .s_wstrb(sp_wstrb[QC*(MAXW/8) +: 4]),
            .s_wlast(sp_wlast[QC]), .s_wvalid(sp_wvalid[QC]),
            .s_wready(sp_wready[QC]),
            .s_bid(sp_bid[QC*MAXID +: IDW]), .s_bresp(sp_bresp[QC*2 +: 2]),
            .s_bvalid(sp_bvalid[QC]), .s_bready(sp_bready[QC]),
            .s_arid(sp_arid[QC*MAXID +: IDW]),
            .s_araddr(sp_araddr[QC*AW +: 32]),
            .s_arlen(sp_arlen[QC*8 +: 8]), .s_arsize(sp_arsize[QC*3 +: 3]),
            .s_arburst(sp_arburst[QC*2 +: 2]),
            .s_arvalid(sp_arvalid[QC]), .s_arready(sp_arready[QC]),
            .s_rid(sp_rid[QC*MAXID +: IDW]),
            .s_rdata(sp_rdata[QC*MAXW +: 32]),
            .s_rresp(sp_rresp[QC*2 +: 2]), .s_rlast(sp_rlast[QC]),
            .s_rvalid(sp_rvalid[QC]), .s_rready(sp_rready[QC]),
            .m_awid(cs_awid[g]), .m_awaddr(cs_awaddr[g]),
            .m_awlen(cs_awlen[g]), .m_awsize(), .m_awburst(),
            .m_awvalid(cs_awvalid[g]), .m_awready(cs_awready[g]),
            .m_wdata(cs_wdata[g]), .m_wstrb(cs_wstrb[g]),
            .m_wlast(cs_wlast[g]), .m_wvalid(cs_wvalid[g]),
            .m_wready(cs_wready[g]),
            .m_bid(cs_bid[g]), .m_bresp(cs_bresp[g]),
            .m_bvalid(cs_bvalid[g]), .m_bready(),
            .m_arid(cs_arid[g]), .m_araddr(cs_araddr[g]),
            .m_arlen(cs_arlen[g]), .m_arsize(), .m_arburst(),
            .m_arvalid(cs_arvalid[g]), .m_arready(cs_arready[g]),
            .m_rid(cs_rid[g]), .m_rdata(cs_rdata[g]), .m_rresp(cs_rresp[g]),
            .m_rlast(cs_rlast[g]), .m_rvalid(cs_rvalid[g]), .m_rready()
        );

        // The node's memory-port NoC face looped to its own processor: with no
        // router between them the orchestrator's dispatched flits still arrive.
        // The node has ONE port and the processor is behind its hub, so the
        // loopback is now the port onto itself. sb_skid's `i_ready` does not
        // depend on `o_ready`, which is what keeps that from being a
        // combinational loop -- a wire here would be one.
        wire [MFW-1:0] lo_d, li_d;
        wire           lo_v, lo_rdy, li_v, li_b;

        sb_skid #(.W(MFW)) u_lp (
            .clk(nclk), .rst(!rstn),
            .i_valid(lo_v), .i_ready(lo_rdy), .i_data(lo_d),
            .o_valid(li_v), .o_ready(!li_b), .o_data(li_d)
        );

        sysnode #(.FLIT_WIDTH(MFW), .POS_WIDTH(4), .DATA_W(DW), .ADDR_W(NAW),
               .ID_W(IDW), .PORTS(1), .ILINK(1), .MESH_ID(g), .MW(DW),
               .PE_MEM_PRIM("block")) u (
            .clk(nclk), .resetn(rstn),
            .sm_awid   (sp_awid  [QM*MAXID +: IDW]),
            .sm_awaddr (sp_awaddr[QM*AW    +: NAW]),
            .sm_awlen  (sp_awlen [QM*8     +: 8]),
            .sm_awvalid(sp_awvalid[QM]), .sm_awready(sp_awready[QM]),
            .sm_wdata  (sp_wdata [QM*MAXW  +: DW]),
            .sm_wstrb  (sp_wstrb [QM*(MAXW/8) +: DW/8]),
            .sm_wlast  (sp_wlast [QM]), .sm_wvalid(sp_wvalid[QM]),
            .sm_wready (sp_wready[QM]),
            .sm_bid    (sp_bid   [QM*MAXID +: IDW]),
            .sm_bresp  (sp_bresp [QM*2     +: 2]),
            .sm_bvalid (sp_bvalid[QM]), .sm_bready(sp_bready[QM]),
            .sm_arid   (sp_arid  [QM*MAXID +: IDW]),
            .sm_araddr (sp_araddr[QM*AW    +: NAW]),
            .sm_arlen  (sp_arlen [QM*8     +: 8]),
            .sm_arvalid(sp_arvalid[QM]), .sm_arready(sp_arready[QM]),
            .sm_rid    (sp_rid   [QM*MAXID +: IDW]),
            .sm_rdata  (sp_rdata [QM*MAXW  +: DW]),
            .sm_rresp  (sp_rresp [QM*2     +: 2]),
            .sm_rlast  (sp_rlast [QM]), .sm_rvalid(sp_rvalid[QM]),
            .sm_rready (sp_rready[QM]),

            .sc_awid(cs_awid[g]), .sc_awaddr(cs_awaddr[g]),
            .sc_awlen(cs_awlen[g]),
            .sc_awvalid(cs_awvalid[g]), .sc_awready(cs_awready[g]),
            .sc_wdata(cs_wdata[g]), .sc_wstrb(cs_wstrb[g]),
            .sc_wlast(cs_wlast[g]), .sc_wvalid(cs_wvalid[g]),
            .sc_wready(cs_wready[g]),
            .sc_bid(cs_bid[g]), .sc_bresp(cs_bresp[g]),
            .sc_bvalid(cs_bvalid[g]), .sc_bready(1'b1),
            .sc_arid(cs_arid[g]), .sc_araddr(cs_araddr[g]),
            .sc_arlen(cs_arlen[g]),
            .sc_arvalid(cs_arvalid[g]), .sc_arready(cs_arready[g]),
            .sc_rid(cs_rid[g]), .sc_rdata(cs_rdata[g]),
            .sc_rresp(cs_rresp[g]), .sc_rlast(cs_rlast[g]),
            .sc_rvalid(cs_rvalid[g]), .sc_rready(1'b1),

            .dram_aclk(dclk), .dram_aresetn(rstn),
            .dram_awid(dm_awid[g]), .dram_awaddr(dm_awaddr[g]),
            .dram_awlen(dm_awlen[g]), .dram_awsize(dm_awsize[g]),
            .dram_awburst(dm_awburst[g]),
            .dram_awvalid(dm_awvalid[g]), .dram_awready(dm_awready[g]),
            .dram_wdata(dm_wdata[g]), .dram_wstrb(dm_wstrb[g]),
            .dram_wlast(dm_wlast[g]), .dram_wvalid(dm_wvalid[g]),
            .dram_wready(dm_wready[g]),
            .dram_bid(dm_bid[g]), .dram_bresp(dm_bresp[g]),
            .dram_bvalid(dm_bvalid[g]), .dram_bready(dm_bready[g]),
            .dram_arid(dm_arid[g]), .dram_araddr(dm_araddr[g]),
            .dram_arlen(dm_arlen[g]), .dram_arsize(dm_arsize[g]),
            .dram_arburst(dm_arburst[g]),
            .dram_arvalid(dm_arvalid[g]), .dram_arready(dm_arready[g]),
            .dram_rid(dm_rid[g]), .dram_rdata(dm_rdata[g]),
            .dram_rresp(dm_rresp[g]), .dram_rlast(dm_rlast[g]),
            .dram_rvalid(dm_rvalid[g]), .dram_rready(dm_rready[g]),

            .mem_in_data(li_d), .mem_in_valid(li_v), .mem_in_busy(li_b),
            .mem_out_data(lo_d), .mem_out_valid(lo_v), .mem_out_busy(!lo_rdy),

            .mem_rd_count(), .mem_wr_count(),
            .mv_busy(nbusy[g]), .mv_fault(), .mv_done(),
            .pe_halt_req(1'b0), .pe_status(), .pe_busy(pbusy[g]),

            .link0_out_tdata (lk_d[g][0]), .link0_out_tuser(lk_u[g][0]),
            .link0_out_tlast (lk_l[g][0]), .link0_out_tvalid(lk_v[g][0]),
            .link0_out_tready(1'b1),
            .link0_in_tdata ((g == 1) ? lk_d[0][1] : 288'd0),
            .link0_in_tuser ((g == 1) ? lk_u[0][1] : 96'd0),
            .link0_in_tlast ((g == 1) ? lk_l[0][1] : 1'b0),
            .link0_in_tvalid((g == 1) ? lk_v[0][1] : 1'b0),
            .link0_in_tready(),
            .link1_out_tdata (lk_d[g][1]), .link1_out_tuser(lk_u[g][1]),
            .link1_out_tlast (lk_l[g][1]), .link1_out_tvalid(lk_v[g][1]),
            .link1_out_tready(1'b1),
            .link1_in_tdata ((g == 0) ? lk_d[1][0] : 288'd0),
            .link1_in_tuser ((g == 0) ? lk_u[1][0] : 96'd0),
            .link1_in_tlast ((g == 0) ? lk_l[1][0] : 1'b0),
            .link1_in_tvalid((g == 0) ? lk_v[1][0] : 1'b0),
            .link1_in_tready()
        );

        // A LOCAL move names its own mesh id, and that field stays on the
        // address all the way to the DRAM master: node 1 issues 0x10_0020_0000
        // for its own memory. The block design's address map strips the window
        // before the controller, so the model has to as well -- unmasked,
        // axi_ram indexes the first beat out of range and returns X for exactly
        // one word while the burst counter serves the rest correctly.
        wire [NAW-1:0] ram_aw = dm_awaddr[g] & 40'h00_FFFF_FFFF;
        wire [NAW-1:0] ram_ar = dm_araddr[g] & 40'h00_FFFF_FFFF;

        axi_ram #(.DATA_W(DW), .ADDR_W(NAW), .ID_W(IDW),
                  .WORDS(RAMW), .PORTS(1)) ram (
            .clk(dclk), .resetn(rstn),
            .s_awid(dm_awid[g]), .s_awaddr(ram_aw),
            .s_awlen(dm_awlen[g]), .s_awsize(dm_awsize[g]),
            .s_awburst(dm_awburst[g]),
            .s_awvalid(dm_awvalid[g]), .s_awready(dm_awready[g]),
            .s_wdata(dm_wdata[g]), .s_wstrb(dm_wstrb[g]),
            .s_wlast(dm_wlast[g]), .s_wvalid(dm_wvalid[g]),
            .s_wready(dm_wready[g]),
            .s_bid(dm_bid[g]), .s_bresp(dm_bresp[g]),
            .s_bvalid(dm_bvalid[g]), .s_bready(dm_bready[g]),
            .s_arid(dm_arid[g]), .s_araddr(ram_ar),
            .s_arlen(dm_arlen[g]), .s_arsize(dm_arsize[g]),
            .s_arburst(dm_arburst[g]),
            .s_arvalid(dm_arvalid[g]), .s_arready(dm_arready[g]),
            .s_rid(dm_rid[g]), .s_rdata(dm_rdata[g]), .s_rresp(dm_rresp[g]),
            .s_rlast(dm_rlast[g]), .s_rvalid(dm_rvalid[g]),
            .s_rready(dm_rready[g]),
            .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({DW{1'b0}}), .bd_rdata()
        );
    end endgenerate

    genvar q;
    generate for (q = 4; q < NS; q = q + 1) begin : ep
        localparam integer EW = (q % NQ == 0) ? FW : 32;
        axi_ram #(.DATA_W(EW), .ADDR_W(AW), .ID_W(MAXID),
                  .WORDS(512), .PORTS(1)) r (
            .clk(sclk[q / NQ]), .resetn(rstn),
            .s_awid(sp_awid[q*MAXID +: MAXID]),
            .s_awaddr(sp_awaddr[q*AW +: AW]),
            .s_awlen(sp_awlen[q*8 +: 8]), .s_awsize(sp_awsize[q*3 +: 3]),
            .s_awburst(sp_awburst[q*2 +: 2]),
            .s_awvalid(sp_awvalid[q]), .s_awready(sp_awready[q]),
            .s_wdata(sp_wdata[q*MAXW +: EW]),
            .s_wstrb(sp_wstrb[q*(MAXW/8) +: EW/8]),
            .s_wlast(sp_wlast[q]), .s_wvalid(sp_wvalid[q]),
            .s_wready(sp_wready[q]),
            .s_bid(sp_bid[q*MAXID +: MAXID]), .s_bresp(sp_bresp[q*2 +: 2]),
            .s_bvalid(sp_bvalid[q]), .s_bready(sp_bready[q]),
            .s_arid(sp_arid[q*MAXID +: MAXID]),
            .s_araddr(sp_araddr[q*AW +: AW]),
            .s_arlen(sp_arlen[q*8 +: 8]), .s_arsize(sp_arsize[q*3 +: 3]),
            .s_arburst(sp_arburst[q*2 +: 2]),
            .s_arvalid(sp_arvalid[q]), .s_arready(sp_arready[q]),
            .s_rid(sp_rid[q*MAXID +: MAXID]),
            .s_rdata(sp_rdata[q*MAXW +: EW]),
            .s_rresp(sp_rresp[q*2 +: 2]), .s_rlast(sp_rlast[q]),
            .s_rvalid(sp_rvalid[q]), .s_rready(sp_rready[q]),
            .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({EW{1'b0}}), .bd_rdata()
        );
    end endgenerate

    // ------------------------------------------------------------- the driver
    task ntick; begin @(negedge clk_ctrl); end endtask

    task wr64(input [AW-1:0] a, input [63:0] d);
        begin
            ntick;
            mp_awaddr[0*AW +: AW] = a;
            mp_awlen [0*8  +: 8]  = 8'd0;
            mp_awsize[0*3  +: 3]  = 3'd3;
            mp_awvalid[0] = 1'b1;
            spin = 0;
            while (spin < 20000) begin
                @(posedge clk_ctrl);
                if (mp_awready[0]) begin
                    spin = 30000;
                end
                else begin
                    spin = spin + 1;
                end
            end
            ntick; mp_awvalid[0] = 1'b0;
            ntick;
            mp_wdata[0*MAXW +: MAXW]       = {(MAXW/64){d}};
            mp_wstrb[0*(MAXW/8) +: MAXW/8] = {(MAXW/8){1'b1}};
            mp_wlast[0]  = 1'b1;
            mp_wvalid[0] = 1'b1;
            spin = 0;
            while (spin < 20000) begin
                @(posedge clk_ctrl);
                if (mp_wready[0]) begin
                    spin = 30000;
                end
                else begin
                    spin = spin + 1;
                end
            end
            ntick; mp_wvalid[0] = 1'b0; mp_wlast[0] = 1'b0;
            spin = 0;
            while (spin < 20000 && !mp_bvalid[0]) begin ntick; spin = spin + 1; end
            ntick;
        end
    endtask

    task cwr(input integer n, input [15:0] off, input [63:0] d);
        begin wr64(ctrl_win(n) | {{(AW-16){1'b0}}, off}, d); end
    endtask

    function [MFW-1:0] mkflit;
        input [3:0]   ty;
        input [7:0]   txn;
        input         lst;
        input [255:0] pay;
        begin mkflit = {16'd0, ty, txn, lst, 3'b000, pay}; end
    endfunction

    integer nslot;

    task stage_flit(input integer n, input integer s, input [MFW-1:0] f);
        begin
            cwr(n, A_STAGE + (s*5+0)*8, f[63:0]);
            cwr(n, A_STAGE + (s*5+1)*8, f[127:64]);
            cwr(n, A_STAGE + (s*5+2)*8, f[191:128]);
            cwr(n, A_STAGE + (s*5+3)*8, f[255:192]);
            cwr(n, A_STAGE + (s*5+4)*8, {32'd0, f[MFW-1:256]});
        end
    endtask

    task stage_granule(input integer n, input [7:0] buf_id,
                       input [15:0] off, input [255:0] payload);
        begin
            stage_flit(n, nslot, mkflit(4'h8, 8'd0, 1'b0,
                       {buf_id, off, 8'd0, 8'd0, 4'd0, 4'd0, 208'd0}));
            nslot = nslot + 1;
            stage_flit(n, nslot, mkflit(4'h8, 8'd0, 1'b1, payload));
            nslot = nslot + 1;
        end
    endtask

    task dispatch(input integer n, input [7:0] dst,
                  input [15:0] base, input [15:0] len);
        begin
            cwr(n, A_PROG_BASE, {48'd0, base});
            cwr(n, A_PROG_LEN,  {48'd0, len});
            cwr(n, A_PROG_DST,  {56'd0, dst});
            cwr(n, A_PROG_CRED, {48'd0, 16'd64});
            cwr(n, A_PROG_KICK, 64'd1);
        end
    endtask

    // ---- RV32I ------------------------------------------------------------
    function [31:0] i_lui;  input [4:0] rd; input [19:0] imm;
        begin i_lui = {imm, rd, 7'b0110111}; end endfunction
    function [31:0] i_addi; input [4:0] rd; input [4:0] rs1; input [11:0] imm;
        begin i_addi = {imm, rs1, 3'b000, rd, 7'b0010011}; end endfunction
    function [31:0] i_sw;   input [4:0] rs1; input [4:0] rs2; input [11:0] imm;
        begin i_sw = {imm[11:5], rs2, rs1, 3'b010, imm[4:0], 7'b0100011};
        end endfunction
    function [31:0] i_lw;   input [4:0] rd; input [4:0] rs1; input [11:0] imm;
        begin i_lw = {imm, rs1, 3'b010, rd, 7'b0000011}; end endfunction
    function [31:0] i_bne;  input [4:0] rs1; input [4:0] rs2;
                            input signed [12:0] off;
        begin i_bne = {off[12], off[10:5], rs2, rs1, 3'b001,
                       off[4:1], off[11], 7'b1100011}; end endfunction

    reg [255:0] gran;

    // The seven mover-config words as a spad descriptor mv_exec walks: a count,
    // then {offset, lo, hi} per write. Both walkers carry a MESH in the header's
    // top half, and a local move names its OWN id -- leaving it 0 on a non-zero
    // node is a silent cross-node push, not a local write.
    task stage_descriptor(input integer n,
                          input [31:0] slo, input [31:0] shi,
                          input [31:0] dlo, input [31:0] dhi);
        begin
            gran = 256'd0;
            gran[31:0]    = 32'd7;
            gran[63:32]   = 32'h10;
            gran[95:64]   = slo;
            gran[127:96]  = shi;
            gran[159:128] = 32'h18;
            gran[191:160] = 32'h0200_0080;
            gran[223:192] = 32'h0000_0000;
            gran[255:224] = 32'h20;
            stage_granule(n, 8'd0, 16'd8, gran);

            gran = 256'd0;
            gran[95:64]   = 32'h10;
            gran[127:96]  = dlo;
            gran[159:128] = dhi;
            gran[191:160] = 32'h18;
            gran[223:192] = 32'h0200_0081;
            stage_granule(n, 8'd0, 16'd9, gran);

            gran = 256'd0;
            gran[31:0]    = 32'h20;
            gran[127:96]  = 32'h00;
            gran[159:128] = 32'h0001_0808;
            stage_granule(n, 8'd0, 16'd10, gran);
        end
    endtask

    task chk(input cond, input [8*60-1:0] what, input integer where);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                if (errors < 20) begin
                    $display("  FAIL %0s [%0d]", what, where);
                end
            end
        end
    endtask

    wire peA = pbusy[0];
    wire peB = pbusy[1];

    // Every beat node 1's DRAM actually receives, so a bad word is attributed
    // to the address it was written at rather than inferred from the result.
    // -d PAIR_TRACE: thousands of lines, and it is what located the one X.
`ifdef PAIR_TRACE
    always @(posedge db) if (rstn) begin
        if (dm_awvalid[1] && dm_awready[1]) begin
            $display("  %0t B-DRAM AW addr=%h len=%0d", $time,
                     dm_awaddr[1], dm_awlen[1]);
        end
        if (dm_wvalid[1] && dm_wready[1]) begin
            $display("  %0t B-DRAM  W data=%h last=%b strb=%h", $time,
                     dm_wdata[1][63:0], dm_wlast[1], dm_wstrb[1][7:0]);
        end
        if (dm_arvalid[1] && dm_arready[1]) begin
            $display("  %0t B-DRAM AR addr=%h len=%0d", $time,
                     dm_araddr[1], dm_arlen[1]);
        end
        if (dm_rvalid[1] && dm_rready[1]) begin
            $display("  %0t B-DRAM-R data=%h last=%b", $time,
                     dm_rdata[1][63:0], dm_rlast[1]);
        end
    end

    // What the mover's staging FIFO is actually fed, and by which beat: the
    // wire address and length are correct, so the X arrives between DRAM and
    // here -- with the processor's own read outstanding on the same path.
    always @(posedge nb) if (rstn && nd[1].u.u_pe.u_mover.f_wr)
        $display("  %0t B-MV-FIFO wr=%h", $time,
                 nd[1].u.u_pe.u_mover.m_rdata[63:0]);
`endif

    // What B's mover saw when it started, and what A had actually delivered by
    // then: a stale read shows the poison, a never-written one shows X.
    reg mvB_d;
    always @(posedge clk_ctrl) begin
        mvB_d <= nbusy[1];
        if (nbusy[1] && !mvB_d) begin
            $display("  %0t PROBE B's mover STARTED: DST[0]=%h DST[7]=%h FINAL[0]=%h",
                     $time, nd[1].ram.mem[DST_OFF >> 5][63:0],
                     nd[1].ram.mem[(DST_OFF >> 5) + NW - 1][63:0],
                     nd[1].ram.mem[FINAL_OFF >> 5][63:0]);
        end
        if (!nbusy[1] && mvB_d) begin
            $display("  %0t PROBE B's mover FINISHED: FINAL[0]=%h FINAL[7]=%h",
                     $time, nd[1].ram.mem[FINAL_OFF >> 5][63:0],
                     nd[1].ram.mem[(FINAL_OFF >> 5) + NW - 1][63:0]);
        end
    end

    initial begin
        for (i = 0; i < NW; i = i + 1) begin
            nd[0].ram.mem[(SRC_OFF >> 5) + i] = {8{32'hAC00_0000 | i[31:0]}};
            nd[1].ram.mem[(DST_OFF >> 5) + i]   = {8{32'h5A5A_5A5A}};
            nd[1].ram.mem[(FINAL_OFF >> 5) + i] = {8{32'hA5A5_A5A5}};
        end

        repeat (40) @(negedge clk_xdma);
        rstn = 1'b1;
        repeat (400) @(negedge clk_xdma);

        // ---- B first: it must be polling before A sends -------------------
        $display("--- load B: poll the last word, invalidating L1, then move ---");
        nslot = 0;
        // B's move is DST -> FINAL, both local to node 1, so both headers carry
        // mesh 1 (hi32 0x0000_1100).
        stage_descriptor(1, 32'h0200_0000, 32'h0000_1100,
                            32'h0300_0001, 32'h0000_1100);

        gran = 256'd0;
        gran[31:0]   = i_lui (5'd1, 20'h20000);          // control window
        gran[63:32]  = i_lui (5'd2, 20'h80200);          // DRAM | DST_OFF
        gran[95:64]  = i_addi(5'd2, 5'd2, 12'h0E0);      // + (NW-1)*32
        gran[127:96] = i_lui (5'd4, 20'hAC000);
        gran[159:128]= i_addi(5'd4, 5'd4, 12'd7);        // the sentinel
        gran[191:160]= i_sw  (5'd1, 5'd0, 12'd8);        // C_INVAL, blocking
        gran[223:192]= i_lw  (5'd3, 5'd2, 12'd0);
        gran[255:224]= i_bne (5'd3, 5'd4, -13'sd8);      // back to C_INVAL
        stage_granule(1, 8'd1, 16'd0, gran);

        gran = 256'd0;
        gran[31:0]   = i_lui (5'd5, 20'hF0000);
        gran[63:32]  = i_addi(5'd6, 5'd0, 12'd64);
        gran[95:64]  = i_sw  (5'd5, 5'd6, 12'd0);        // mv.go
        gran[127:96] = 32'h0000_0073;                    // ECALL
        stage_granule(1, 8'd1, 16'd1, gran);

        stage_flit(1, nslot, mkflit(4'h5, 8'd7, 1'b1,
                   {8'd1, 32'd0, 32'd0, 184'd0}));
        nslot = nslot + 1;
        dispatch(1, 8'h00, 16'd0, nslot[15:0]);

        spin = 0;
        while (!peB && spin < 60000) begin @(negedge clk_ctrl); spin = spin + 1; end
        chk(spin < 60000, "B's processor started and is polling", spin);

        // It must NOT have moved anything yet: the doorbell has not arrived.
        chk(nd[1].ram.mem[FINAL_OFF >> 5] === {8{32'hA5A5_A5A5}},
            "B is still waiting -- nothing moved before the doorbell", 0);

        // ---- A: push its result into B over the interlink ------------------
        $display("--- load A: move SRC into B's DRAM over the interlink ---");
        nslot = 0;
        // A reads its OWN SRC (mesh 0) and writes node 1's DST (mesh 1).
        stage_descriptor(0, 32'h0100_0000, 32'h0000_1000,
                            32'h0200_0001, 32'h0000_1100);
        gran = 256'd0;
        gran[31:0]   = i_lui (5'd1, 20'hF0000);
        gran[63:32]  = i_addi(5'd2, 5'd0, 12'd64);
        gran[95:64]  = i_sw  (5'd1, 5'd2, 12'd0);
        gran[127:96] = 32'h0000_0073;
        stage_granule(0, 8'd1, 16'd0, gran);
        stage_flit(0, nslot, mkflit(4'h5, 8'd7, 1'b1,
                   {8'd1, 32'd0, 32'd0, 184'd0}));
        nslot = nslot + 1;
        dispatch(0, 8'h00, 16'd0, nslot[15:0]);

        spin = 0;
        while (!peA && spin < 60000) begin @(negedge clk_ctrl); spin = spin + 1; end
        chk(spin < 60000, "A's processor started", spin);

        // ---- B should now see the doorbell and produce FINAL ---------------
        $display("--- waiting for B to observe the arrival and act ---");
        spin = 0;
        while (nd[1].ram.mem[(FINAL_OFF >> 5) + NW - 1]
               !== {8{32'hAC00_0000 | (NW - 1)}} && spin < 400000) begin
            @(negedge clk_ctrl); spin = spin + 1;
        end
        chk(spin < 400000, "B WOKE ON THE DOORBELL AND RAN ITS OWN MOVE", spin);

        // The last word matching only says the move REACHED the tail. Reading
        // the rest while the mover is still on the bus samples a location
        // mid-write, which reads X and looks like a data fault.
        spin = 0;
        while (nbusy[1] && spin < 100000) begin
            @(negedge clk_ctrl); spin = spin + 1;
        end
        chk(spin < 100000, "B's mover went idle", spin);
        repeat (200) @(negedge clk_ctrl);

        nbad = 0;
        for (i = 0; i < NW; i = i + 1) begin
            if (nd[1].ram.mem[(DST_OFF >> 5) + i]
                !== {8{32'hAC00_0000 | i[31:0]}}) nbad = nbad + 1;
        end
        $display("    A -> B over the interlink: %0d of %0d words wrong", nbad, NW);
        chk(nbad == 0, "A's data reached B", nbad);

        nbad = 0;
        for (i = 0; i < NW; i = i + 1) begin
            if (nd[1].ram.mem[(FINAL_OFF >> 5) + i]
                !== {8{32'hAC00_0000 | i[31:0]}}) begin
                nbad = nbad + 1;
                // 5A5A is DST's poison: B read that word before A's write to it
                // landed, which says the doorbell did not order the payload.
                $display("      FINAL[%0d] = %h (DST now %h, want %h)", i,
                         nd[1].ram.mem[(FINAL_OFF >> 5) + i][63:0],
                         nd[1].ram.mem[(DST_OFF >> 5) + i][63:0],
                         {2{32'hAC00_0000 | i[31:0]}});
            end
        end
        $display("    B's own result: %0d of %0d words wrong", nbad, NW);
        chk(nbad == 0, "B consumed A's output and produced the result", nbad);

        chk(stat_decerr == 32'd0, "no decode errors anywhere", stat_decerr);

        if (errors == 0) begin
            $display("PASS ctrlpe_pair_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL ctrlpe_pair_tb: %0d errors, %0d checks",
                     errors, checks);
        end
        $finish;
    end

    initial begin
        #12000000;
        $display("FAIL ctrlpe_pair_tb: watchdog  peA=%b peB=%b", peA, peB);
        $display("    B FINAL[last]=%h",
                 nd[1].ram.mem[(FINAL_OFF >> 5) + NW - 1][63:0]);
        $finish;
    end
endmodule

`default_nettype wire

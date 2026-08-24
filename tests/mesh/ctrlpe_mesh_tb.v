// TARGET 2: one mesh with its control processor, driven ONLY through the bus.
//
//     .   cpu   .          1 system node, 1 router, 1 matmul, 1 vector core,
//    mag  mat  vec         and the node's control processor on the north edge
//     .   nul   .
//
// host -> sb_nmu -> flit -> sb_nsu -> axi_up32to64 -> S_AXI_CTRL -> orchestrator
//      -> staged flits -> NoC -> the processor's imem -> the processor runs
//      -> mv.go -> the real mover -> DRAM
//
// NO AXI MASTER TOUCHES A MESH PORT and nothing is poked hierarchically: the
// program reaches the processor the same way a compute unit's program does, as
// CU_DATA the orchestrator dispatches.

`timescale 1ns / 1ps
`default_nettype none

module ctrlpe_mesh_tb;
    localparam integer AW    = 43;
    localparam integer FW    = 256;
    localparam integer NQ    = 2;
    localparam integer PORTW = 1;
    localparam integer NM    = 3;
    localparam integer NS    = 4 * NQ;
    localparam integer MAXW  = 512;
    localparam integer MAXID = 4;
    localparam integer DSTW  = 2;

    localparam integer MESH_DW = 256;
    localparam integer MESH_AW = 40;
    localparam integer DRAM_W  = 512;
    localparam integer IDW     = 4;
    localparam integer RAMW    = 33792;
    localparam integer MFW     = 288;          // mesh flit width

    localparam integer NW = 8;                 // 32-byte words the program moves
    localparam [MESH_AW-1:0] SRC_OFF = 40'h10_0000;
    localparam [MESH_AW-1:0] DST_OFF = 40'h20_0000;

    // Phase 2: a CONVERTING move, mode 5, programmed by the same processor.
    // 2 entries of 8 source words each -> 2 entries of 4 destination words.
    // IN RANGE: the bench's DRAM model is RAMW 512-bit words, so it ends just
    // past 0x21_0000. Above that both walks alias and the move overwrites its
    // own source, which reads as a transform fault rather than a bench bug.
    localparam integer NENT = 2, NXW = 16;
    localparam [MESH_AW-1:0] XSRC_OFF = 40'h04_0000;
    localparam [MESH_AW-1:0] XDST_OFF = 40'h08_0000;

    // noc_orchestrator's map.
    localparam [15:0] A_PROG_DST = 16'h0040, A_PROG_LEN = 16'h0048;
    localparam [15:0] A_PROG_KICK = 16'h0050, A_PROG_STAT = 16'h0058;
    localparam [15:0] A_PROG_CRED = 16'h0060, A_PROG_BASE = 16'h0068;
    localparam [15:0] A_STAGE = 16'h2000;

    integer errors = 0, checks = 0, spin, i, slot;

    // ---------------------------------------------------------------- clocks
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

    reg noc = 0, mat = 0, vec = 0, mag = 0, dram = 0;
    always begin
        #2.109 noc  = ~noc;
    end
    always begin
        #1.451 mat  = ~mat;
    end
    always begin
        #1.889 vec  = ~vec;
    end
    always begin
        #2.237 mag  = ~mag;
    end
    always begin
        #1.913 dram = ~dram;
    end

    reg rstn = 0;
    wire bus_rst = !rstn;

    // ------------------------------------------------------------ the v6 map
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

    // ------------------------------------------------------- manager plumbing
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
    wire [3:0] sclk = {clk_s3, clk_s2, clk_s2, mag};

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
        .clk_ddr0(dram), .aresetn_ddr0(rstn),
        .clk_ddr1(dram), .aresetn_ddr1(rstn),
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

    // --------------------------------------------------------------- the mesh
    wire [IDW-1:0]      dm_awid, dm_arid, dm_bid, dm_rid;
    wire [MESH_AW-1:0]  dm_awaddr, dm_araddr;
    wire [7:0]          dm_awlen, dm_arlen;
    wire [2:0]          dm_awsize, dm_arsize;
    wire [1:0]          dm_awburst, dm_arburst, dm_bresp, dm_rresp;
    wire                dm_awvalid, dm_awready, dm_arvalid, dm_arready;
    wire [DRAM_W-1:0]   dm_wdata, dm_rdata;
    wire [DRAM_W/8-1:0] dm_wstrb;
    wire                dm_wlast, dm_wvalid, dm_wready;
    wire                dm_bvalid, dm_bready, dm_rlast, dm_rvalid, dm_rready;

    wire [IDW-1:0] cs_awid, cs_arid, cs_bid, cs_rid;
    wire [31:0]    cs_awaddr, cs_araddr;
    wire [7:0]     cs_awlen, cs_arlen;
    wire [63:0]    cs_wdata, cs_rdata;
    wire [7:0]     cs_wstrb;
    wire [1:0]     cs_bresp, cs_rresp;
    wire           cs_awvalid, cs_awready, cs_wvalid, cs_wready;
    wire           cs_bvalid, cs_arvalid, cs_arready, cs_rvalid, cs_rlast;
    wire           cs_wlast;

    localparam integer QM = 0;      // station 0 port 0: the memory window
    localparam integer QC = 1;      // station 0 port 1: the control window, 32b

    axi_up32to64 #(.AW(32), .IDW(IDW)) u_up (
        .clk(mag), .resetn(rstn),
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
        .m_awid(cs_awid), .m_awaddr(cs_awaddr), .m_awlen(cs_awlen),
        .m_awsize(), .m_awburst(),
        .m_awvalid(cs_awvalid), .m_awready(cs_awready),
        .m_wdata(cs_wdata), .m_wstrb(cs_wstrb),
        .m_wlast(cs_wlast), .m_wvalid(cs_wvalid), .m_wready(cs_wready),
        .m_bid(cs_bid), .m_bresp(cs_bresp), .m_bvalid(cs_bvalid), .m_bready(),
        .m_arid(cs_arid), .m_araddr(cs_araddr), .m_arlen(cs_arlen),
        .m_arsize(), .m_arburst(),
        .m_arvalid(cs_arvalid), .m_arready(cs_arready),
        .m_rid(cs_rid), .m_rdata(cs_rdata), .m_rresp(cs_rresp),
        .m_rlast(cs_rlast), .m_rvalid(cs_rvalid), .m_rready()
    );

    ktpu_ctrlpe_1x1 #(.MESH_ID(0), .MODEL(1), .MW(DRAM_W),
                      .MAG_CDC(1), .UNIT_CDC(1)) u (
        .axi_aclk(mag), .axi_aresetn(rstn),
        .noc_clk(noc), .mat_clk(mat), .vec_clk(vec),
        .dram_aclk(dram), .dram_aresetn(rstn),

        .S_AXI_MEM_awid   (sp_awid  [QM*MAXID +: IDW]),
        .S_AXI_MEM_awaddr (sp_awaddr[QM*AW    +: MESH_AW]),
        .S_AXI_MEM_awlen  (sp_awlen [QM*8     +: 8]),
        .S_AXI_MEM_awvalid(sp_awvalid[QM]), .S_AXI_MEM_awready(sp_awready[QM]),
        .S_AXI_MEM_wdata  (sp_wdata [QM*MAXW  +: MESH_DW]),
        .S_AXI_MEM_wstrb  (sp_wstrb [QM*(MAXW/8) +: MESH_DW/8]),
        .S_AXI_MEM_wlast  (sp_wlast [QM]), .S_AXI_MEM_wvalid (sp_wvalid[QM]),
        .S_AXI_MEM_wready (sp_wready[QM]),
        .S_AXI_MEM_bid    (sp_bid   [QM*MAXID +: IDW]),
        .S_AXI_MEM_bresp  (sp_bresp [QM*2     +: 2]),
        .S_AXI_MEM_bvalid (sp_bvalid[QM]), .S_AXI_MEM_bready (sp_bready[QM]),
        .S_AXI_MEM_arid   (sp_arid  [QM*MAXID +: IDW]),
        .S_AXI_MEM_araddr (sp_araddr[QM*AW    +: MESH_AW]),
        .S_AXI_MEM_arlen  (sp_arlen [QM*8     +: 8]),
        .S_AXI_MEM_arvalid(sp_arvalid[QM]), .S_AXI_MEM_arready(sp_arready[QM]),
        .S_AXI_MEM_rid    (sp_rid   [QM*MAXID +: IDW]),
        .S_AXI_MEM_rdata  (sp_rdata [QM*MAXW  +: MESH_DW]),
        .S_AXI_MEM_rresp  (sp_rresp [QM*2     +: 2]),
        .S_AXI_MEM_rlast  (sp_rlast [QM]), .S_AXI_MEM_rvalid (sp_rvalid[QM]),
        .S_AXI_MEM_rready (sp_rready[QM]),

        .S_AXI_CTRL_awid(cs_awid), .S_AXI_CTRL_awaddr(cs_awaddr),
        .S_AXI_CTRL_awlen(cs_awlen),
        .S_AXI_CTRL_awvalid(cs_awvalid), .S_AXI_CTRL_awready(cs_awready),
        .S_AXI_CTRL_wdata(cs_wdata), .S_AXI_CTRL_wstrb(cs_wstrb),
        .S_AXI_CTRL_wlast(cs_wlast), .S_AXI_CTRL_wvalid(cs_wvalid),
        .S_AXI_CTRL_wready(cs_wready),
        .S_AXI_CTRL_bid(cs_bid), .S_AXI_CTRL_bresp(cs_bresp),
        .S_AXI_CTRL_bvalid(cs_bvalid), .S_AXI_CTRL_bready(1'b1),
        .S_AXI_CTRL_arid(cs_arid), .S_AXI_CTRL_araddr(cs_araddr),
        .S_AXI_CTRL_arlen(cs_arlen),
        .S_AXI_CTRL_arvalid(cs_arvalid), .S_AXI_CTRL_arready(cs_arready),
        .S_AXI_CTRL_rid(cs_rid), .S_AXI_CTRL_rdata(cs_rdata),
        .S_AXI_CTRL_rresp(cs_rresp), .S_AXI_CTRL_rlast(cs_rlast),
        .S_AXI_CTRL_rvalid(cs_rvalid), .S_AXI_CTRL_rready(1'b1),

        .M_AXI_DRAM_awid(dm_awid), .M_AXI_DRAM_awaddr(dm_awaddr),
        .M_AXI_DRAM_awlen(dm_awlen), .M_AXI_DRAM_awsize(dm_awsize),
        .M_AXI_DRAM_awburst(dm_awburst),
        .M_AXI_DRAM_awvalid(dm_awvalid), .M_AXI_DRAM_awready(dm_awready),
        .M_AXI_DRAM_wdata(dm_wdata), .M_AXI_DRAM_wstrb(dm_wstrb),
        .M_AXI_DRAM_wlast(dm_wlast), .M_AXI_DRAM_wvalid(dm_wvalid),
        .M_AXI_DRAM_wready(dm_wready),
        .M_AXI_DRAM_bid(dm_bid), .M_AXI_DRAM_bresp(dm_bresp),
        .M_AXI_DRAM_bvalid(dm_bvalid), .M_AXI_DRAM_bready(dm_bready),
        .M_AXI_DRAM_arid(dm_arid), .M_AXI_DRAM_araddr(dm_araddr),
        .M_AXI_DRAM_arlen(dm_arlen), .M_AXI_DRAM_arsize(dm_arsize),
        .M_AXI_DRAM_arburst(dm_arburst),
        .M_AXI_DRAM_arvalid(dm_arvalid), .M_AXI_DRAM_arready(dm_arready),
        .M_AXI_DRAM_rid(dm_rid), .M_AXI_DRAM_rdata(dm_rdata),
        .M_AXI_DRAM_rresp(dm_rresp), .M_AXI_DRAM_rlast(dm_rlast),
        .M_AXI_DRAM_rvalid(dm_rvalid), .M_AXI_DRAM_rready(dm_rready),

        .M_AXIS_LINK0_tdata(), .M_AXIS_LINK0_tuser(), .M_AXIS_LINK0_tlast(),
        .M_AXIS_LINK0_tvalid(), .M_AXIS_LINK0_tready(1'b1),
        .S_AXIS_LINK0_tdata(288'd0), .S_AXIS_LINK0_tuser(96'd0),
        .S_AXIS_LINK0_tlast(1'b0), .S_AXIS_LINK0_tvalid(1'b0),
        .S_AXIS_LINK0_tready(),
        .M_AXIS_LINK1_tdata(), .M_AXIS_LINK1_tuser(), .M_AXIS_LINK1_tlast(),
        .M_AXIS_LINK1_tvalid(), .M_AXIS_LINK1_tready(1'b1),
        .S_AXIS_LINK1_tdata(288'd0), .S_AXIS_LINK1_tuser(96'd0),
        .S_AXIS_LINK1_tlast(1'b0), .S_AXIS_LINK1_tvalid(1'b0),
        .S_AXIS_LINK1_tready()
    );

    axi_ram #(.DATA_W(DRAM_W), .ADDR_W(MESH_AW), .ID_W(IDW),
              .WORDS(RAMW), .PORTS(1)) ram (
        .clk(dram), .resetn(rstn),
        .s_awid(dm_awid), .s_awaddr(dm_awaddr), .s_awlen(dm_awlen),
        .s_awsize(dm_awsize), .s_awburst(dm_awburst),
        .s_awvalid(dm_awvalid), .s_awready(dm_awready),
        .s_wdata(dm_wdata), .s_wstrb(dm_wstrb), .s_wlast(dm_wlast),
        .s_wvalid(dm_wvalid), .s_wready(dm_wready),
        .s_bid(dm_bid), .s_bresp(dm_bresp), .s_bvalid(dm_bvalid),
        .s_bready(dm_bready),
        .s_arid(dm_arid), .s_araddr(dm_araddr), .s_arlen(dm_arlen),
        .s_arsize(dm_arsize), .s_arburst(dm_arburst),
        .s_arvalid(dm_arvalid), .s_arready(dm_arready),
        .s_rid(dm_rid), .s_rdata(dm_rdata), .s_rresp(dm_rresp),
        .s_rlast(dm_rlast), .s_rvalid(dm_rvalid), .s_rready(dm_rready),
        .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({DRAM_W{1'b0}}), .bd_rdata()
    );

    // ---- the other station ports get RAMs so no NSU dangles ----------------
    genvar q;
    generate for (q = 2; q < NS; q = q + 1) begin : ep
        localparam integer DW = (q % NQ == 0) ? FW : 32;
        axi_ram #(.DATA_W(DW), .ADDR_W(AW), .ID_W(MAXID),
                  .WORDS(512), .PORTS(1)) r (
            .clk(sclk[q / NQ]), .resetn(rstn),
            .s_awid(sp_awid[q*MAXID +: MAXID]),
            .s_awaddr(sp_awaddr[q*AW +: AW]),
            .s_awlen(sp_awlen[q*8 +: 8]), .s_awsize(sp_awsize[q*3 +: 3]),
            .s_awburst(sp_awburst[q*2 +: 2]),
            .s_awvalid(sp_awvalid[q]), .s_awready(sp_awready[q]),
            .s_wdata(sp_wdata[q*MAXW +: DW]),
            .s_wstrb(sp_wstrb[q*(MAXW/8) +: DW/8]),
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
            .s_rdata(sp_rdata[q*MAXW +: DW]),
            .s_rresp(sp_rresp[q*2 +: 2]), .s_rlast(sp_rlast[q]),
            .s_rvalid(sp_rvalid[q]), .s_rready(sp_rready[q]),
            .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({DW{1'b0}}), .bd_rdata()
        );
    end endgenerate

    // ------------------------------------------------------------- the driver
    task ntick; begin @(negedge clk_ctrl); end endtask

    // Data REPLICATED across every lane with all strobes set: the station chain
    // delivers one host write64 as four beats with one strobed, and which lane
    // that is depends on the chain, not on us.
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

    task cwr(input [15:0] off, input [63:0] d);
        begin wr64(ctrl_win(0) | {{(AW-16){1'b0}}, off}, d); end
    endtask

    // ---- staging and dispatch ---------------------------------------------
    // The dispatcher rewrites the top 16 bits (dst and src) of every staged
    // flit, so a staged header carries zeros there and only the class, txn and
    // last matter.
    function [MFW-1:0] mkflit;
        input [3:0]   ty;
        input [7:0]   txn;
        input         lst;
        input [255:0] pay;
        begin mkflit = {16'd0, ty, txn, lst, 3'b000, pay}; end
    endfunction

    task stage_flit(input integer s, input [MFW-1:0] f);
        begin
            cwr(A_STAGE + (s*5+0)*8, f[63:0]);
            cwr(A_STAGE + (s*5+1)*8, f[127:64]);
            cwr(A_STAGE + (s*5+2)*8, f[191:128]);
            cwr(A_STAGE + (s*5+3)*8, f[255:192]);
            cwr(A_STAGE + (s*5+4)*8, {32'd0, f[MFW-1:256]});
        end
    endtask

    // BASE, LEN, DST, CRED, KICK -- the driver's order.
    task dispatch(input [7:0] dst, input [15:0] base, input [15:0] len);
        begin
            cwr(A_PROG_BASE, {48'd0, base});
            cwr(A_PROG_LEN,  {48'd0, len});
            cwr(A_PROG_DST,  {56'd0, dst});
            cwr(A_PROG_CRED, {48'd0, 16'd64});
            cwr(A_PROG_KICK, 64'd1);
        end
    endtask

    // ---- the program the processor runs ------------------------------------
    function [31:0] i_lui;  input [4:0] rd; input [19:0] imm;
        begin i_lui = {imm, rd, 7'b0110111}; end endfunction
    function [31:0] i_addi; input [4:0] rd; input [4:0] rs1; input [11:0] imm;
        begin i_addi = {imm, rs1, 3'b000, rd, 7'b0010011}; end endfunction
    function [31:0] i_sw;   input [4:0] rs1; input [4:0] rs2; input [11:0] imm;
        begin i_sw = {imm[11:5], rs2, rs1, 3'b010, imm[4:0], 7'b0100011};
        end endfunction

    reg [255:0] gran;
    integer     nslot;

    // Two flits per granule: the CU_DATA descriptor and its payload.
    task stage_granule(input [7:0] buf_id, input [15:0] off,
                       input [255:0] payload);
        begin
            stage_flit(nslot, mkflit(4'h8, 8'd0, 1'b0,
                       {buf_id, off, 8'd0, 8'd0, 4'd0, 4'd0, 208'd0}));
            nslot = nslot + 1;
            stage_flit(nslot, mkflit(4'h8, 8'd0, 1'b1, payload));
            nslot = nslot + 1;
        end
    endtask

    task chk(input cond, input [8*44-1:0] what, input integer where);
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

    function [255:0] dram_word;
        input integer w;
        begin dram_word = ram.mem[w >> 1][(w & 1) * 256 +: 256]; end
    endfunction

    task set_dram_word(input integer w, input [255:0] v);
        begin ram.mem[w >> 1][(w & 1) * 256 +: 256] = v; end
    endtask

    // ---- the reference occupant, fed the same beats ------------------------
    // The bench compares against mx_quant directly rather than restating its
    // arithmetic: what is under test here is that the PROCESSOR's descriptor
    // put the right bytes through the slot and the results in the right place.
    reg           g_start = 0, g_bv = 0;
    reg  [255:0]  g_beat = 0;
    wire          g_done;
    wire [255:0]  g_w0, g_w1, g_w2, g_w3;
    mx_quant u_ref (
        .clk(clk_ctrl), .rst(!rstn),
        .start(g_start), .b_layout(1'b0),
        .beat(g_beat), .beat_valid(g_bv),
        .need_beat(), .done(g_done),
        .word0(g_w0), .word1(g_w1), .word2(g_w2), .word3(g_w3)
    );

    reg [255:0] ref_w [0:NENT*4-1];
    integer     e, b;

    task reference(input integer ent);
        begin
            @(negedge clk_ctrl); g_start = 1'b1;
            @(negedge clk_ctrl); g_start = 1'b0;
            for (b = 0; b < 8; b = b + 1) begin
                g_beat = dram_word((XSRC_OFF >> 5) + ent*8 + b);
                g_bv   = 1'b1;
                @(negedge clk_ctrl);
                g_bv   = 1'b0;
            end
            spin = 0;
            while (!g_done && spin < 400) begin
                @(negedge clk_ctrl); spin = spin + 1;
            end
            chk(spin < 400, "the reference occupant finished an entry", ent);
            ref_w[ent*4+0] = g_w0; ref_w[ent*4+1] = g_w1;
            ref_w[ent*4+2] = g_w2; ref_w[ent*4+3] = g_w3;
        end
    endtask

    wire pe_busy_w = u.pe_busy;
    wire mv_busy_w = u.u_mag.u_mag.u_mag.mv_busy;

    reg [63:0] st;
    // SIZED: an integer inside a replication contributes 32 bits, so {16{...}}
    // of an unsized expression builds 8 copies of a 32-bit word, not 16.
    reg [15:0] sv;
    initial begin
        for (i = 0; i < NW; i = i + 1) begin
            ram.mem[((SRC_OFF >> 5) + i) >> 1]
                   [((((SRC_OFF >> 5) + i) & 1)) * 256 +: 256]
                = {8{32'hAC00_0000 | i[31:0]}};
            ram.mem[((DST_OFF >> 5) + i) >> 1]
                   [((((DST_OFF >> 5) + i) & 1)) * 256 +: 256]
                = {8{32'hA5A5_A5A5}};
        end

        // Phase 2's FP16 source, and a poison destination so an alias shows.
        for (e = 0; e < NENT; e = e + 1) begin
            for (b = 0; b < 8; b = b + 1) begin
                sv = 16'h3C00 + e[15:0] * 16'd8 + b[15:0];
                set_dram_word((XSRC_OFF >> 5) + e*8 + b, {16{sv}});
            end
        end
        for (i = 0; i < NENT*4; i = i + 1) begin
            set_dram_word((XDST_OFF >> 5) + i, {8{32'hA5A5_A5A5}});
        end

        repeat (40) @(negedge clk_xdma);
        rstn = 1'b1;
        repeat (400) @(negedge clk_xdma);

        chk(dram_word(SRC_OFF >> 5) === {8{32'hAC00_0000}},
            "source preloaded", 0);
        chk(pe_busy_w === 1'b0, "processor idle at rest", 0);

        // ---- 1. the mover descriptor, as scratchpad granules ---------------
        $display("--- stage the program: 3 data granules and the code ---");
        nslot = 0;

        gran = 256'd0;
        gran[31:0]    = 32'd7;
        gran[63:32]   = 32'h10;
        gran[95:64]   = 32'h0100_0000;
        gran[127:96]  = 32'h0000_1000;
        gran[159:128] = 32'h18;
        gran[191:160] = 32'h0200_0080;
        gran[223:192] = 32'h0000_0000;
        gran[255:224] = 32'h20;
        stage_granule(8'd0, 16'd8, gran);

        gran = 256'd0;
        gran[95:64]   = 32'h10;
        gran[127:96]  = 32'h0200_0001;
        gran[159:128] = 32'h0000_1000;
        gran[191:160] = 32'h18;
        gran[223:192] = 32'h0200_0081;
        stage_granule(8'd0, 16'd9, gran);

        gran = 256'd0;
        gran[31:0]    = 32'h20;
        gran[127:96]  = 32'h00;
        gran[159:128] = 32'h0001_0808;
        stage_granule(8'd0, 16'd10, gran);

        // ---- 2. the code ---------------------------------------------------
        gran = 256'd0;
        gran[31:0]   = i_lui (5'd1, 20'hF0000);
        gran[63:32]  = i_addi(5'd2, 5'd0, 12'd64);
        gran[95:64]  = i_sw  (5'd1, 5'd2, 12'd0);
        gran[127:96] = 32'h0000_0073;
        stage_granule(8'd1, 16'd0, gran);

        // ---- 3. the kick ---------------------------------------------------
        stage_flit(nslot, mkflit(4'h5, 8'd7, 1'b1,
                   {8'd1, 32'd0, 32'd0, 184'd0}));
        nslot = nslot + 1;

        $display("--- dispatch %0d flits to the processor at (1,0) ---", nslot);
        dispatch(8'h01, 16'd0, nslot[15:0]);

        spin = 0;
        while (u.u_mag.u_mag.u_mag.u_agent.prog_run && spin < 40000) begin
            @(negedge clk_ctrl); spin = spin + 1;
        end
        chk(spin < 40000, "the dispatcher drained its program", spin);

        spin = 0;
        while (!pe_busy_w && spin < 40000) begin
            @(negedge clk_ctrl); spin = spin + 1;
        end
        chk(spin < 40000, "THE PROCESSOR STARTED, from a host-staged image", spin);

        spin = 0;
        while (!mv_busy_w && spin < 80000) begin
            @(negedge clk_ctrl); spin = spin + 1;
        end
        chk(spin < 80000, "the processor started the real mover", spin);

        spin = 0;
        while (mv_busy_w && spin < 200000) begin
            @(negedge clk_ctrl); spin = spin + 1;
        end
        chk(spin < 200000, "the mover went idle", spin);

        // A unit retires on its last write SENT, so poll the LAST word.
        spin = 0;
        while (dram_word((DST_OFF >> 5) + NW - 1)
               !== {8{32'hAC00_0000 | (NW - 1)}} && spin < 40000) begin
            @(negedge clk_ctrl); spin = spin + 1;
        end

        $display("--- what landed in DRAM ---");
        for (i = 0; i < NW; i = i + 1) begin
            chk(dram_word((DST_OFF >> 5) + i) === {8{32'hAC00_0000 | i[31:0]}},
                "destination word", i);
        end

        chk(stat_decerr == 32'd0, "no decode errors anywhere", stat_decerr);

        // ================= 2. A CONVERTING MOVE, PROGRAMMED IN ASM ==========
        // Same processor, same mv.go, same descriptor mechanism -- only the
        // mode and the transform id on the source header differ from the copy
        // above. Nothing here reaches the mover except through the program.
        $display("--- phase 2: the processor programs a mode-5 move ---");

        spin = 0;
        while (pe_busy_w && spin < 40000) begin
            @(negedge clk_ctrl); spin = spin + 1;
        end
        chk(spin < 40000, "the first program retired", spin);

        for (e = 0; e < NENT; e = e + 1) begin
            reference(e);
        end

        nslot = 0;

        // The descriptor at scratchpad word 96: 7 register writes.
        //   0x10 src {base, ndim 1, XFORM_ID 1}   0x18 src {16 words, +32}
        //   0x10 dst {base, ndim 1}               0x18 dst {2 entries, +128}
        //   0x00 {mode 5, ewidth 1, GO}
        gran = 256'd0;
        gran[31:0]    = 32'd7;
        gran[63:32]   = 32'h10;
        gran[95:64]   = 32'h0040_0000;
        gran[127:96]  = 32'h0000_9000;
        gran[159:128] = 32'h18;
        gran[191:160] = 32'h0200_0100;
        gran[223:192] = 32'h0000_0000;
        gran[255:224] = 32'h20;
        stage_granule(8'd0, 16'd12, gran);

        gran = 256'd0;
        gran[95:64]   = 32'h10;
        gran[127:96]  = 32'h0080_0001;
        gran[159:128] = 32'h0000_1000;
        gran[191:160] = 32'h18;
        gran[223:192] = 32'h0800_0021;
        stage_granule(8'd0, 16'd13, gran);

        gran = 256'd0;
        gran[31:0]    = 32'h20;
        gran[127:96]  = 32'h00;
        gran[159:128] = 32'h0001_000D;
        stage_granule(8'd0, 16'd14, gran);

        gran = 256'd0;
        gran[31:0]   = i_lui (5'd1, 20'hF0000);
        gran[63:32]  = i_addi(5'd2, 5'd0, 12'd96);
        gran[95:64]  = i_sw  (5'd1, 5'd2, 12'd0);
        gran[127:96] = 32'h0000_0073;
        stage_granule(8'd1, 16'd2, gran);

        stage_flit(nslot, mkflit(4'h5, 8'd7, 1'b1,
                   {8'd1, 32'd64, 32'd0, 184'd0}));
        nslot = nslot + 1;

        dispatch(8'h01, 16'd0, nslot[15:0]);

        spin = 0;
        while (u.u_mag.u_mag.u_mag.u_agent.prog_run && spin < 40000) begin
            @(negedge clk_ctrl); spin = spin + 1;
        end
        spin = 0;
        while (!mv_busy_w && spin < 80000) begin
            @(negedge clk_ctrl); spin = spin + 1;
        end
        chk(spin < 80000, "the processor started a CONVERTING move", spin);

        spin = 0;
        while (mv_busy_w && spin < 200000) begin
            @(negedge clk_ctrl); spin = spin + 1;
        end
        chk(spin < 200000, "the converting move went idle", spin);
        chk(u.u_mag.u_mag.u_mag.mv_fault === 4'd0, "no mover fault", 0);

        spin = 0;
        while (dram_word((XDST_OFF >> 5) + NENT*4 - 1) === {8{32'hA5A5_A5A5}}
               && spin < 40000) begin
            @(negedge clk_ctrl); spin = spin + 1;
        end

        $display("--- what the slot produced, against the reference ---");
        for (i = 0; i < NENT*4; i = i + 1) begin
            chk(dram_word((XDST_OFF >> 5) + i) === ref_w[i],
                "converted destination word", i);
        end
        chk(dram_word(XSRC_OFF >> 5) === {16{16'h3C00}},
            "the source is untouched", 0);

        if (errors == 0) begin
            $display("PASS ctrlpe_mesh_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL ctrlpe_mesh_tb: %0d errors, %0d checks",
                     errors, checks);
        end
        $finish;
    end

    initial begin
        #14000000;
        $display("FAIL ctrlpe_mesh_tb: watchdog  pe_busy=%b mv_busy=%b",
                 pe_busy_w, mv_busy_w);
        $finish;
    end
endmodule

`default_nettype wire

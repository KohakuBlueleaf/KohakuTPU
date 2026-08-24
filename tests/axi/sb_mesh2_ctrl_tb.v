// THE TOP-LEVEL BENCH: two meshes, both AXI windows live, over the station bus.
//
//     .   vec   .          2 matmul, 2 vector, 1 MAG, 1 NoC per mesh
//    mag  mat  mat         MAG on ILINK, link1 <-> link0 (gen header s15)
//     .   vec   .
//
// mesh0 sits on station 0 and mesh1 on station 1; the managers are on station 1,
// so mesh0 is reached over a station LINK and mesh1 locally -- two independent
// cross-boundary paths, the mesh interlink being the other.
//
// NO AXI MASTER TOUCHES A MESH PORT. Control goes host -> sb_nmu -> flit ->
// sb_nsu -> axi_up32to64 -> S_AXI_CTRL, which is the ship's chain: the station
// port is 32 bits (sb_line4.v:413) and 40_bus.tcl upsizes it to MAG's 64. That
// chain turns one host write64 into four 64-bit beats with ONE strobed, which is
// the shape every earlier bench had to model by hand.

`timescale 1ns / 1ps
`default_nettype none

module sb_mesh2_ctrl_tb;
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
    localparam integer RAMW    = 33792;    // must cover 0x200000 + the copy

    localparam integer NW = 64;            // 32-byte words moved
    localparam [MESH_AW-1:0] SRC_OFF = 40'h10_0000;
    localparam [MESH_AW-1:0] DST_OFF = 40'h20_0000;

    // mover.py: the aux window inside the mesh's 64 KB control window.
    localparam [15:0] A_MV_CFG  = 16'h0800;
    localparam [15:0] A_AUX_STAT = 16'h0078;

    integer errors = 0, checks = 0, spin, i, m;

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

    reg noc0 = 0, noc1 = 0;
    always begin
        #2.109 noc0 = ~noc0;
    end
    always begin
        #1.667 noc1 = ~noc1;
    end
    reg mat0 = 0, vec0 = 0, mag0 = 0, dram0 = 0;
    reg mat1 = 0, vec1 = 0, mag1 = 0, dram1 = 0;
    always begin
        #1.451 mat0  = ~mat0;
    end
    always begin
        #1.889 vec0  = ~vec0;
    end
    always begin
        #2.237 mag0  = ~mag0;
    end
    always begin
        #1.913 dram0 = ~dram0;
    end
    always begin
        #1.559 mat1  = ~mat1;
    end
    always begin
        #2.017 vec1  = ~vec1;
    end
    always begin
        #1.783 mag1  = ~mag1;
    end
    always begin
        #2.111 dram1 = ~dram1;
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
    // A 64 KB window per station, XLT 0, so the endpoint sees waddr[15:0] --
    // exactly what noc_orchestrator decodes.
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
    wire [3:0] sclk = {clk_s3, clk_s2, mag1, mag0};

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
        .clk_ddr0(dram0), .aresetn_ddr0(rstn),
        .clk_ddr1(dram1), .aresetn_ddr1(rstn),
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

    // ------------------------------------------------------------- the meshes
    wire [IDW-1:0]      dm_awid [0:1], dm_arid[0:1], dm_bid[0:1], dm_rid[0:1];
    wire [MESH_AW-1:0]  dm_awaddr[0:1], dm_araddr[0:1];
    wire [7:0]          dm_awlen[0:1], dm_arlen[0:1];
    wire [2:0]          dm_awsize[0:1], dm_arsize[0:1];
    wire [1:0]          dm_awburst[0:1], dm_arburst[0:1];
    wire [1:0]          dm_bresp[0:1], dm_rresp[0:1];
    wire [1:0]          dm_awvalid, dm_awready, dm_arvalid, dm_arready;
    wire [DRAM_W-1:0]   dm_wdata[0:1], dm_rdata[0:1];
    wire [DRAM_W/8-1:0] dm_wstrb[0:1];
    wire [1:0]          dm_wlast, dm_wvalid, dm_wready;
    wire [1:0]          dm_bvalid, dm_bready, dm_rlast, dm_rvalid, dm_rready;

    // ONE SET PER MESH. Shared nets here mean both meshes drive the same wire
    // and every remote beat resolves to X.
    wire [287:0] lk_d [0:1][0:1];      // [mesh][port]
    wire [95:0]  lk_u [0:1][0:1];
    wire         lk_l [0:1][0:1];
    wire         lk_v [0:1][0:1];

    // The control leg of each station port, between the 32-bit NSU and the mesh.
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
    generate for (g = 0; g < 2; g = g + 1) begin : mesh
        localparam integer QM = g * NQ;          // this station's port 0 (MEM)
        localparam integer QC = g * NQ + 1;      // port 1 (CTRL, 32-bit)
        wire aclk = (g == 0) ? noc0  : noc1;
        wire mclk = (g == 0) ? mag0  : mag1;
        wire tclk = (g == 0) ? mat0  : mat1;
        wire vclk = (g == 0) ? vec0  : vec1;
        wire dclk = (g == 0) ? dram0 : dram1;

        // The ship's control hop. Slave side is the station's 32-bit port,
        // master side MAG's 64-bit S_AXI_CTRL; both on this mesh's MAG clock.
        axi_up32to64 #(.AW(32), .IDW(IDW)) u_up (
            .clk(mclk), .resetn(rstn),
            .s_awid(sp_awid[QC*MAXID +: IDW]),
            .s_awaddr(sp_awaddr[QC*AW +: 32]),
            .s_awlen(sp_awlen[QC*8 +: 8]),
            .s_awsize(sp_awsize[QC*3 +: 3]),
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
            .s_arlen(sp_arlen[QC*8 +: 8]),
            .s_arsize(sp_arsize[QC*3 +: 3]),
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

        ktpu_ship_1x1_2c2v_1m #(.MESH_ID(g), .MODEL(1), .MW(DRAM_W),
                                .MAG_CDC(1), .UNIT_CDC(1)) u (
            .axi_aclk(mclk), .axi_aresetn(rstn),
            .noc_clk(aclk), .mat_clk(tclk), .vec_clk(vclk),
            .dram_aclk(dclk), .dram_aresetn(rstn),

            .S_AXI_MEM_awid   (sp_awid  [QM*MAXID +: IDW]),
            .S_AXI_MEM_awaddr (sp_awaddr[QM*AW    +: MESH_AW]),
            .S_AXI_MEM_awlen  (sp_awlen [QM*8     +: 8]),
            .S_AXI_MEM_awvalid(sp_awvalid[QM]),
            .S_AXI_MEM_awready(sp_awready[QM]),
            .S_AXI_MEM_wdata  (sp_wdata [QM*MAXW  +: MESH_DW]),
            .S_AXI_MEM_wstrb  (sp_wstrb [QM*(MAXW/8) +: MESH_DW/8]),
            .S_AXI_MEM_wlast  (sp_wlast [QM]),
            .S_AXI_MEM_wvalid (sp_wvalid[QM]),
            .S_AXI_MEM_wready (sp_wready[QM]),
            .S_AXI_MEM_bid    (sp_bid   [QM*MAXID +: IDW]),
            .S_AXI_MEM_bresp  (sp_bresp [QM*2     +: 2]),
            .S_AXI_MEM_bvalid (sp_bvalid[QM]),
            .S_AXI_MEM_bready (sp_bready[QM]),
            .S_AXI_MEM_arid   (sp_arid  [QM*MAXID +: IDW]),
            .S_AXI_MEM_araddr (sp_araddr[QM*AW    +: MESH_AW]),
            .S_AXI_MEM_arlen  (sp_arlen [QM*8     +: 8]),
            .S_AXI_MEM_arvalid(sp_arvalid[QM]),
            .S_AXI_MEM_arready(sp_arready[QM]),
            .S_AXI_MEM_rid    (sp_rid   [QM*MAXID +: IDW]),
            .S_AXI_MEM_rdata  (sp_rdata [QM*MAXW  +: MESH_DW]),
            .S_AXI_MEM_rresp  (sp_rresp [QM*2     +: 2]),
            .S_AXI_MEM_rlast  (sp_rlast [QM]),
            .S_AXI_MEM_rvalid (sp_rvalid[QM]),
            .S_AXI_MEM_rready (sp_rready[QM]),

            // LIVE, which is the point: every earlier bench tied this off.
            .S_AXI_CTRL_awid(cs_awid[g]), .S_AXI_CTRL_awaddr(cs_awaddr[g]),
            .S_AXI_CTRL_awlen(cs_awlen[g]),
            .S_AXI_CTRL_awvalid(cs_awvalid[g]),
            .S_AXI_CTRL_awready(cs_awready[g]),
            .S_AXI_CTRL_wdata(cs_wdata[g]), .S_AXI_CTRL_wstrb(cs_wstrb[g]),
            .S_AXI_CTRL_wlast(cs_wlast[g]), .S_AXI_CTRL_wvalid(cs_wvalid[g]),
            .S_AXI_CTRL_wready(cs_wready[g]),
            .S_AXI_CTRL_bid(cs_bid[g]), .S_AXI_CTRL_bresp(cs_bresp[g]),
            .S_AXI_CTRL_bvalid(cs_bvalid[g]), .S_AXI_CTRL_bready(1'b1),
            .S_AXI_CTRL_arid(cs_arid[g]), .S_AXI_CTRL_araddr(cs_araddr[g]),
            .S_AXI_CTRL_arlen(cs_arlen[g]),
            .S_AXI_CTRL_arvalid(cs_arvalid[g]),
            .S_AXI_CTRL_arready(cs_arready[g]),
            .S_AXI_CTRL_rid(cs_rid[g]), .S_AXI_CTRL_rdata(cs_rdata[g]),
            .S_AXI_CTRL_rresp(cs_rresp[g]), .S_AXI_CTRL_rlast(cs_rlast[g]),
            .S_AXI_CTRL_rvalid(cs_rvalid[g]), .S_AXI_CTRL_rready(1'b1),

            .M_AXI_DRAM_awid(dm_awid[g]), .M_AXI_DRAM_awaddr(dm_awaddr[g]),
            .M_AXI_DRAM_awlen(dm_awlen[g]), .M_AXI_DRAM_awsize(dm_awsize[g]),
            .M_AXI_DRAM_awburst(dm_awburst[g]),
            .M_AXI_DRAM_awvalid(dm_awvalid[g]),
            .M_AXI_DRAM_awready(dm_awready[g]),
            .M_AXI_DRAM_wdata(dm_wdata[g]), .M_AXI_DRAM_wstrb(dm_wstrb[g]),
            .M_AXI_DRAM_wlast(dm_wlast[g]), .M_AXI_DRAM_wvalid(dm_wvalid[g]),
            .M_AXI_DRAM_wready(dm_wready[g]),
            .M_AXI_DRAM_bid(dm_bid[g]), .M_AXI_DRAM_bresp(dm_bresp[g]),
            .M_AXI_DRAM_bvalid(dm_bvalid[g]), .M_AXI_DRAM_bready(dm_bready[g]),
            .M_AXI_DRAM_arid(dm_arid[g]), .M_AXI_DRAM_araddr(dm_araddr[g]),
            .M_AXI_DRAM_arlen(dm_arlen[g]), .M_AXI_DRAM_arsize(dm_arsize[g]),
            .M_AXI_DRAM_arburst(dm_arburst[g]),
            .M_AXI_DRAM_arvalid(dm_arvalid[g]),
            .M_AXI_DRAM_arready(dm_arready[g]),
            .M_AXI_DRAM_rid(dm_rid[g]), .M_AXI_DRAM_rdata(dm_rdata[g]),
            .M_AXI_DRAM_rresp(dm_rresp[g]), .M_AXI_DRAM_rlast(dm_rlast[g]),
            .M_AXI_DRAM_rvalid(dm_rvalid[g]), .M_AXI_DRAM_rready(dm_rready[g]),

            // mesh0.link1 <-> mesh1.link0, per the generated top's CH_SEQ note.
            // The far side of each pair ties tready high, which is the rule.
            .M_AXIS_LINK0_tdata (lk_d[g][0]), .M_AXIS_LINK0_tuser(lk_u[g][0]),
            .M_AXIS_LINK0_tlast (lk_l[g][0]), .M_AXIS_LINK0_tvalid(lk_v[g][0]),
            .M_AXIS_LINK0_tready(1'b1),
            .S_AXIS_LINK0_tdata ((g == 1) ? lk_d[0][1] : 288'd0),
            .S_AXIS_LINK0_tuser ((g == 1) ? lk_u[0][1] : 96'd0),
            .S_AXIS_LINK0_tlast ((g == 1) ? lk_l[0][1] : 1'b0),
            .S_AXIS_LINK0_tvalid((g == 1) ? lk_v[0][1] : 1'b0),
            .S_AXIS_LINK0_tready(),
            .M_AXIS_LINK1_tdata (lk_d[g][1]), .M_AXIS_LINK1_tuser(lk_u[g][1]),
            .M_AXIS_LINK1_tlast (lk_l[g][1]), .M_AXIS_LINK1_tvalid(lk_v[g][1]),
            .M_AXIS_LINK1_tready(1'b1),
            .S_AXIS_LINK1_tdata ((g == 0) ? lk_d[1][0] : 288'd0),
            .S_AXIS_LINK1_tuser ((g == 0) ? lk_u[1][0] : 96'd0),
            .S_AXIS_LINK1_tlast ((g == 0) ? lk_l[1][0] : 1'b0),
            .S_AXIS_LINK1_tvalid((g == 0) ? lk_v[1][0] : 1'b0),
            .S_AXIS_LINK1_tready()
        );

        axi_ram #(.DATA_W(DRAM_W), .ADDR_W(MESH_AW), .ID_W(IDW),
                  .WORDS(RAMW), .PORTS(1)) ram (
            .clk(dclk), .resetn(rstn),
            .s_awid(dm_awid[g]), .s_awaddr(dm_awaddr[g]),
            .s_awlen(dm_awlen[g]), .s_awsize(dm_awsize[g]),
            .s_awburst(dm_awburst[g]),
            .s_awvalid(dm_awvalid[g]), .s_awready(dm_awready[g]),
            .s_wdata(dm_wdata[g]), .s_wstrb(dm_wstrb[g]),
            .s_wlast(dm_wlast[g]), .s_wvalid(dm_wvalid[g]),
            .s_wready(dm_wready[g]),
            .s_bid(dm_bid[g]), .s_bresp(dm_bresp[g]),
            .s_bvalid(dm_bvalid[g]), .s_bready(dm_bready[g]),
            .s_arid(dm_arid[g]), .s_araddr(dm_araddr[g]),
            .s_arlen(dm_arlen[g]), .s_arsize(dm_arsize[g]),
            .s_arburst(dm_arburst[g]),
            .s_arvalid(dm_arvalid[g]), .s_arready(dm_arready[g]),
            .s_rid(dm_rid[g]), .s_rdata(dm_rdata[g]), .s_rresp(dm_rresp[g]),
            .s_rlast(dm_rlast[g]), .s_rvalid(dm_rvalid[g]),
            .s_rready(dm_rready[g]),
            .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({DRAM_W{1'b0}}),
            .bd_rdata()
        );
    end endgenerate

    // ---- stations 2 and 3 get plain RAMs so no NSU dangles -----------------
    genvar q;
    generate for (q = 4; q < NS; q = q + 1) begin : ep
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
    // Manager 0 is the 32-bit control port, which is what JTAG is: a 64-bit
    // register write leaves it as TWO 32-bit beats and the chain repacks it.
    task ntick; begin @(negedge clk_ctrl); end endtask

    // ONE 64-bit transaction -- the driver's write64. Manager 0 is a 64-bit
    // port, so a narrow size here would need lane-placed data AND lane-limited
    // strobes; replicating with all strobes set is an AXI violation that writes
    // both halves.
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

    task cfgwr(input integer msh, input [15:0] off, input [63:0] d);
        begin
            wr64(ctrl_win(msh) | {{(AW-16){1'b0}}, A_MV_CFG}
                               | {{(AW-16){1'b0}}, off}, d);
        end
    endtask

    // mover.py Walker.program: sel | base<<4 | ndim<<44. `base` is a GLOBAL
    // 40-bit address with the mesh at [37:36] -- omit it and the descriptor is
    // a cross-mesh push that completes with no fault and no local bytes moved.
    // sel | base<<4 | ndim<<44, so the field widths must sum to exactly 64 --
    // an over-wide concat truncates the TOP and silently shifts every field.
    function [63:0] hdr_of;
        input [1:0]         dmesh;
        input [MESH_AW-1:0] off;
        input               sel;
        reg [MESH_AW-1:0]   base;
        begin
            base   = off | ({{(MESH_AW-2){1'b0}}, dmesh} << 36);
            hdr_of = {17'd0, 3'd1, base, 3'd0, sel};
        end
    endfunction

    // The driver's seven words for copy(Walker(SRC,[(64,32)]), Walker(DST,...)).
    // `dmesh` is where the DESTINATION lives, so it need not be `msh`.
    task issue_copy(input integer msh, input integer dmesh);
        begin
            // The SOURCE is always local -- mag_ilink splits only the write
            // channel -- and a DRAM model indexes mem[addr>>6] without
            // stripping the mesh field, so mesh bits here read out of range.
            cfgwr(msh, 16'h10, hdr_of(2'd0,       SRC_OFF, 1'b0));
            cfgwr(msh, 16'h18, 64'h0000_0000_0200_0400);
            cfgwr(msh, 16'h20, 64'd0);
            cfgwr(msh, 16'h10, hdr_of(dmesh[1:0], DST_OFF, 1'b1));
            cfgwr(msh, 16'h18, 64'h0000_0000_0200_0401);
            cfgwr(msh, 16'h20, 64'd0);
            cfgwr(msh, 16'h00, 64'h0000_0000_0001_0808);
        end
    endtask

    // ---- where does a descriptor write actually stop? ----------------------
    integer n_ctrl_aw [0:1];
    integer n_ctrl_w  [0:1];
    integer n_pulse   [0:1];
    initial begin n_ctrl_aw[0]=0; n_ctrl_aw[1]=0; n_ctrl_w[0]=0; n_ctrl_w[1]=0;
                  n_pulse[0]=0; n_pulse[1]=0; end

    wire mv0_en = mesh[0].u.u_mag.u_pe.cfg_en_i;
    wire mv1_en = mesh[1].u.u_mag.u_pe.cfg_en_i;
    wire [7:0]  mv0_a = mesh[0].u.u_mag.u_pe.cfg_addr_i;
    wire [7:0]  mv1_a = mesh[1].u.u_mag.u_pe.cfg_addr_i;
    wire [63:0] mv0_d = mesh[0].u.u_mag.u_pe.cfg_data_i;
    wire [63:0] mv1_d = mesh[1].u.u_mag.u_pe.cfg_data_i;
    reg trace = 0;

    always @(posedge mag0) if (rstn) begin
        if (cs_awvalid[0] && cs_awready[0]) begin
            n_ctrl_aw[0] = n_ctrl_aw[0] + 1;
        end
        if (cs_wvalid[0]  && cs_wready[0]) begin
            n_ctrl_w[0]  = n_ctrl_w[0] + 1;
        end
        if (mv0_en) begin
            n_pulse[0] = n_pulse[0] + 1;
            if (trace) begin
                $display("    m0 cfg %0d: addr %02h data %h",
                         n_pulse[0], mv0_a, mv0_d);
            end
        end
    end
    always @(posedge mag1) if (rstn) begin
        if (cs_awvalid[1] && cs_awready[1]) begin
            n_ctrl_aw[1] = n_ctrl_aw[1] + 1;
            if (trace) begin
                $display("      m1 AW addr %h len %0d",
                         cs_awaddr[1], cs_awlen[1]);
            end
        end
        if (cs_wvalid[1]  && cs_wready[1])  begin
            n_ctrl_w[1]  = n_ctrl_w[1] + 1;
            if (trace) begin
                $display("      m1  W strb %02h data %h",
                         cs_wstrb[1], cs_wdata[1]);
            end
        end
        if (mv1_en) begin
            n_pulse[1] = n_pulse[1] + 1;
            if (trace) begin
                $display("    m1 cfg %0d: addr %02h data %h",
                         n_pulse[1], mv1_a, mv1_d);
            end
        end
    end

    wire mv0_busy = mesh[0].u.u_mag.u_pe.u_mover.stat_busy;
    wire mv1_busy = mesh[1].u.u_mag.u_pe.u_mover.stat_busy;
    wire [3:0] mv0_flt = mesh[0].u.u_mag.u_pe.u_mover.stat_fault;
    wire [3:0] mv1_flt = mesh[1].u.u_mag.u_pe.u_mover.stat_fault;

    // Waiting on `busy` instead of a fixed delay: a move that never STARTED and
    // one still running look identical from a timer.
    task wait_mover(input integer msh);
        reg started;
        begin
            started = 1'b0;
            spin = 0;
            while (!started && spin < 60000) begin
                @(negedge clk_ctrl); spin = spin + 1;
                if (msh == 0 ? mv0_busy : mv1_busy) begin
                    started = 1'b1;
                end
            end
            chk(started, "the mover started", msh);
            spin = 0;
            while ((msh == 0 ? mv0_busy : mv1_busy) && spin < 60000) begin
                @(negedge clk_ctrl); spin = spin + 1;
            end
            chk(spin < 60000, "the mover went idle", msh);
            chk((msh == 0 ? mv0_flt : mv1_flt) === 4'd0, "no fault",
                msh == 0 ? mv0_flt : mv1_flt);
            repeat (400) @(negedge clk_ctrl);
        end
    endtask

    task hops(input integer msh);
        begin
            $display("    mesh%0d hops: ctrl AW=%0d W=%0d | mover cfg pulses=%0d",
                     msh, n_ctrl_aw[msh], n_ctrl_w[msh], n_pulse[msh]);
        end
    endtask

    task chk(input cond, input [8*40-1:0] what, input integer where);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                if (errors < 16) begin
                    $display("  FAIL %0s [%0d]", what, where);
                end
            end
        end
    endtask

    // Backdoor into a mesh's DRAM. axi_ram is DRAM_W wide, so a 32-byte mover
    // word is half a RAM row.
    function [255:0] dram_word;
        input integer msh;
        input integer w;
        begin
            dram_word = (msh == 0)
                ? mesh[0].ram.mem[w >> 1][(w & 1) * 256 +: 256]
                : mesh[1].ram.mem[w >> 1][(w & 1) * 256 +: 256];
        end
    endfunction

    task preload(input integer msh);
        integer k;
        begin
            for (k = 0; k < NW; k = k + 1) begin
                if (msh == 0) begin
                    mesh[0].ram.mem[((SRC_OFF >> 5) + k) >> 1]
                        [((((SRC_OFF >> 5) + k) & 1)) * 256 +: 256]
                        = {8{32'hAC00_0000 | k[31:0]}};
                end
                else begin
                    mesh[1].ram.mem[((SRC_OFF >> 5) + k) >> 1]
                        [((((SRC_OFF >> 5) + k) & 1)) * 256 +: 256]
                        = {8{32'hAC00_0000 | k[31:0]}};
                end
                if (msh == 0) begin
                    mesh[0].ram.mem[((DST_OFF >> 5) + k) >> 1]
                        [((((DST_OFF >> 5) + k) & 1)) * 256 +: 256]
                        = {8{32'hA5A5_A5A5}};
                end
                else begin
                    mesh[1].ram.mem[((DST_OFF >> 5) + k) >> 1]
                        [((((DST_OFF >> 5) + k) & 1)) * 256 +: 256]
                        = {8{32'hA5A5_A5A5}};
                end
            end
        end
    endtask

    integer nbad;
    reg [255:0] gotw;
    task check_copy(input integer msh, input [8*40-1:0] tag);
        integer k;
        begin
            // A unit retires on its last write SENT, not landed, so a fixed
            // settle races the tail. Poll the LAST word -- the one an inner-loop
            // -only walk would never reach either.
            spin = 0;
            while (dram_word(msh, (DST_OFF >> 5) + NW - 1)
                   !== {8{32'hAC00_0000 | (NW - 1)}} && spin < 20000) begin
                @(negedge clk_ctrl); spin = spin + 1;
            end
            nbad = 0;
            for (k = 0; k < NW; k = k + 1) begin
                gotw = dram_word(msh, (DST_OFF >> 5) + k);
                if (gotw !== {8{32'hAC00_0000 | k[31:0]}}) begin
                    nbad = nbad + 1;
                    if (nbad < 6) begin
                        $display("      word %0d: %h want %h", k, gotw[63:0],
                                 {2{32'hAC00_0000 | k[31:0]}});
                    end
                end
            end
            $display("    %0s: %0d of %0d destination words wrong", tag, nbad, NW);
            chk(nbad == 0, tag, nbad);
        end
    endtask

    initial begin
        repeat (40) @(negedge clk_xdma);
        rstn = 1'b1;
        preload(0);
        preload(1);
        repeat (400) @(negedge clk_xdma);

        // X in the destination means the SOURCE was X, so separate "the preload
        // did not land" from "the mover moved nothing" before blaming either.
        for (m = 0; m < 2; m = m + 1) begin
            gotw = dram_word(m, SRC_OFF >> 5);
            chk(gotw === {8{32'hAC00_0000}}, "src word 0 preloaded", m);
            gotw = dram_word(m, (SRC_OFF >> 5) + 1);
            chk(gotw === {8{32'hAC00_0001}}, "src word 1 preloaded", m);
            gotw = dram_word(m, DST_OFF >> 5);
            chk(gotw === {8{32'hA5A5_A5A5}}, "dst word 0 poisoned", m);
        end

        $display("--- 1. mesh1 (host's own station): mover copy through the bus ---");
        trace = 1;
        issue_copy(1, 1);
        trace = 0;
        wait_mover(1);
        hops(1);
        check_copy(1, "mesh1 local copy");

        $display("--- 2. mesh0 (one station link away): the same ---");
        issue_copy(0, 0);
        wait_mover(0);
        hops(0);
        check_copy(0, "mesh0 copy over the link");

        // 3. THE INTERLINK, deliberately: mesh0's mover reads mesh0 DRAM and
        // writes mesh1's, so the words cross MAG -> link -> far MAG -> far DRAM.
        $display("--- 3. cross-mesh: mesh0's mover writes into mesh1 ---");
        for (i = 0; i < NW; i = i + 1) begin
            mesh[1].ram.mem[((DST_OFF >> 5) + i) >> 1]
                [((((DST_OFF >> 5) + i) & 1)) * 256 +: 256] = {8{32'h5A5A_5A5A}};
        end
        issue_copy(0, 1);
        wait_mover(0);
        repeat (8000) @(negedge clk_ctrl);
        check_copy(1, "cross-mesh push into mesh1");

        // 4. mesh1 AGAIN. If the first run's two X words were the first packed
        // DRAM beat after reset, a repeat is clean and the cause is warm-up,
        // not the descriptor.
        $display("--- 4. mesh1 local copy, REPEATED ---");
        for (i = 0; i < NW; i = i + 1) begin
            mesh[1].ram.mem[((DST_OFF >> 5) + i) >> 1]
                [((((DST_OFF >> 5) + i) & 1)) * 256 +: 256] = {8{32'hA5A5_A5A5}};
        end
        issue_copy(1, 1);
        wait_mover(1);
        check_copy(1, "mesh1 local copy, second time");

        chk(stat_decerr == 32'd0, "no decode errors anywhere", stat_decerr);

        if (errors == 0) begin
            $display("PASS sb_mesh2_ctrl_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL sb_mesh2_ctrl_tb: %0d errors, %0d checks", errors, checks);
        end
        $finish;
    end

    // 4 ms, as mover_cfg32 uses: two whole meshes make a longer bound cost
    // hours of wall time rather than catching anything extra.
    initial begin
        #4000000;
        $display("FAIL sb_mesh2_ctrl_tb: watchdog");
        $display("    m0 busy=%b fault=%0d | m1 busy=%b fault=%0d",
                 mv0_busy, mv0_flt, mv1_busy, mv1_flt);
        hops(0); hops(1);
        $finish;
    end
endmodule

`default_nettype wire

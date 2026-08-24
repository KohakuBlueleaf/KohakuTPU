// TARGET 3: two meshes, each with its control processor, over the station bus.
//
// mesh0's processor runs a program whose mover descriptor names mesh 1 as the
// destination, so the words cross MAG -> interlink -> far MAG -> far DRAM with
// no host in the loop after the kick. mesh1's processor runs its own local copy
// at the same time, so both are live.
//
// The destination mesh rides address bit 36, which lands at bit 40 of the
// descriptor's header word -- hi32 0x0000_1100 against 0x0000_1000 for a local
// move. A wrong bit aliases into local DRAM and the move "succeeds".

`timescale 1ns / 1ps
`default_nettype none

module ctrlpe_mesh2_tb;
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
    localparam integer MFW     = 288;

    localparam integer NW = 8;
    localparam [MESH_AW-1:0] SRC_OFF = 40'h10_0000;
    localparam [MESH_AW-1:0] DST_OFF = 40'h20_0000;

    // Phase 4: a CONVERTING move whose destination is the FAR mesh, so the
    // slot's output leaves over the interlink. Both bases are kept inside the
    // bench's DRAM model, which ends just past 0x21_0000.
    localparam integer NENT = 2;
    localparam [MESH_AW-1:0] XSRC_OFF = 40'h04_0000;
    localparam [MESH_AW-1:0] XDST_OFF = 40'h08_0000;

    localparam [15:0] A_PROG_DST = 16'h0040, A_PROG_LEN = 16'h0048;
    localparam [15:0] A_PROG_KICK = 16'h0050, A_PROG_CRED = 16'h0060;
    localparam [15:0] A_PROG_BASE = 16'h0068, A_STAGE = 16'h2000;

    integer errors = 0, checks = 0, spin, i, m, nbad;

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

    // ONE SET PER MESH: a shared net means both drive it and every remote beat
    // resolves to X.
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
    generate for (g = 0; g < 2; g = g + 1) begin : mesh
        localparam integer QM = g * NQ;
        localparam integer QC = g * NQ + 1;
        wire aclk = (g == 0) ? noc0  : noc1;
        wire mclk = (g == 0) ? mag0  : mag1;
        wire tclk = (g == 0) ? mat0  : mat1;
        wire vclk = (g == 0) ? vec0  : vec1;
        wire dclk = (g == 0) ? dram0 : dram1;

        axi_up32to64 #(.AW(32), .IDW(IDW)) u_up (
            .clk(mclk), .resetn(rstn),
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

        ktpu_ctrlpe_1x1 #(.MESH_ID(g), .MODEL(1), .MW(DRAM_W),
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

            // mesh0.link1 <-> mesh1.link0, and the far side ties tready high.
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

    task cwr(input integer msh, input [15:0] off, input [63:0] d);
        begin wr64(ctrl_win(msh) | {{(AW-16){1'b0}}, off}, d); end
    endtask

    function [MFW-1:0] mkflit;
        input [3:0]   ty;
        input [7:0]   txn;
        input         lst;
        input [255:0] pay;
        begin mkflit = {16'd0, ty, txn, lst, 3'b000, pay}; end
    endfunction

    task stage_flit(input integer msh, input integer s, input [MFW-1:0] f);
        begin
            cwr(msh, A_STAGE + (s*5+0)*8, f[63:0]);
            cwr(msh, A_STAGE + (s*5+1)*8, f[127:64]);
            cwr(msh, A_STAGE + (s*5+2)*8, f[191:128]);
            cwr(msh, A_STAGE + (s*5+3)*8, f[255:192]);
            cwr(msh, A_STAGE + (s*5+4)*8, {32'd0, f[MFW-1:256]});
        end
    endtask

    task dispatch(input integer msh, input [7:0] dst,
                  input [15:0] base, input [15:0] len);
        begin
            cwr(msh, A_PROG_BASE, {48'd0, base});
            cwr(msh, A_PROG_LEN,  {48'd0, len});
            cwr(msh, A_PROG_DST,  {56'd0, dst});
            cwr(msh, A_PROG_CRED, {48'd0, 16'd64});
            cwr(msh, A_PROG_KICK, 64'd1);
        end
    endtask

    function [31:0] i_lui;  input [4:0] rd; input [19:0] imm;
        begin i_lui = {imm, rd, 7'b0110111}; end endfunction
    function [31:0] i_addi; input [4:0] rd; input [4:0] rs1; input [11:0] imm;
        begin i_addi = {imm, rs1, 3'b000, rd, 7'b0010011}; end endfunction
    function [31:0] i_sw;   input [4:0] rs1; input [4:0] rs2; input [11:0] imm;
        begin i_sw = {imm[11:5], rs2, rs1, 3'b010, imm[4:0], 7'b0100011};
        end endfunction

    reg [255:0] gran;
    integer     nslot;

    task stage_granule(input integer msh, input [7:0] buf_id,
                       input [15:0] off, input [255:0] payload);
        begin
            stage_flit(msh, nslot, mkflit(4'h8, 8'd0, 1'b0,
                       {buf_id, off, 8'd0, 8'd0, 4'd0, 4'd0, 208'd0}));
            nslot = nslot + 1;
            stage_flit(msh, nslot, mkflit(4'h8, 8'd0, 1'b1, payload));
            nslot = nslot + 1;
        end
    endtask

    // The whole image for one processor: the mover descriptor as scratchpad
    // granules, the code, and the kick. `dmesh` is where the copy LANDS.
    task load_and_kick(input integer msh, input [31:0] dst_hi);
        begin
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
            stage_granule(msh, 8'd0, 16'd8, gran);

            gran = 256'd0;
            gran[95:64]   = 32'h10;
            gran[127:96]  = 32'h0200_0001;
            gran[159:128] = dst_hi;          // mesh id rides bit 40 here
            gran[191:160] = 32'h18;
            gran[223:192] = 32'h0200_0081;
            stage_granule(msh, 8'd0, 16'd9, gran);

            gran = 256'd0;
            gran[31:0]    = 32'h20;
            gran[127:96]  = 32'h00;
            gran[159:128] = 32'h0001_0808;
            stage_granule(msh, 8'd0, 16'd10, gran);

            gran = 256'd0;
            gran[31:0]   = i_lui (5'd1, 20'hF0000);
            gran[63:32]  = i_addi(5'd2, 5'd0, 12'd64);
            gran[95:64]  = i_sw  (5'd1, 5'd2, 12'd0);
            gran[127:96] = 32'h0000_0073;
            stage_granule(msh, 8'd1, 16'd0, gran);

            stage_flit(msh, nslot, mkflit(4'h5, 8'd7, 1'b1,
                       {8'd1, 32'd0, 32'd0, 184'd0}));
            nslot = nslot + 1;

            dispatch(msh, 8'h01, 16'd0, nslot[15:0]);
        end
    endtask

    // The same image, but mode 5 with the occupant named on the SOURCE header.
    // `dst_hi` still carries the destination mesh at bit 40, so one field
    // decides whether the converted words stay home or cross the interlink.
    task load_and_kick_xform(input integer msh, input [31:0] dst_hi);
        begin
            nslot = 0;

            gran = 256'd0;
            gran[31:0]    = 32'd7;
            gran[63:32]   = 32'h10;
            gran[95:64]   = 32'h0040_0000;
            gran[127:96]  = 32'h0000_9000;      // ndim 1, XFORM_ID 1
            gran[159:128] = 32'h18;
            gran[191:160] = 32'h0200_0100;      // 16 source words, +32
            gran[223:192] = 32'h0000_0000;
            gran[255:224] = 32'h20;
            stage_granule(msh, 8'd0, 16'd12, gran);

            gran = 256'd0;
            gran[95:64]   = 32'h10;
            gran[127:96]  = 32'h0080_0001;
            gran[159:128] = dst_hi;
            gran[191:160] = 32'h18;
            gran[223:192] = 32'h0800_0021;      // 2 entries, +128
            stage_granule(msh, 8'd0, 16'd13, gran);

            gran = 256'd0;
            gran[31:0]    = 32'h20;
            gran[127:96]  = 32'h00;
            gran[159:128] = 32'h0001_000D;      // mode 5, ewidth 1, GO
            stage_granule(msh, 8'd0, 16'd14, gran);

            gran = 256'd0;
            gran[31:0]   = i_lui (5'd1, 20'hF0000);
            gran[63:32]  = i_addi(5'd2, 5'd0, 12'd96);
            gran[95:64]  = i_sw  (5'd1, 5'd2, 12'd0);
            gran[127:96] = 32'h0000_0073;
            stage_granule(msh, 8'd1, 16'd2, gran);

            stage_flit(msh, nslot, mkflit(4'h5, 8'd7, 1'b1,
                       {8'd1, 32'd64, 32'd0, 184'd0}));
            nslot = nslot + 1;

            dispatch(msh, 8'h01, 16'd0, nslot[15:0]);
        end
    endtask

    task chk(input cond, input [8*48-1:0] what, input integer where);
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
        input integer msh;
        input integer w;
        begin
            dram_word = (msh == 0)
                ? mesh[0].ram.mem[w >> 1][(w & 1) * 256 +: 256]
                : mesh[1].ram.mem[w >> 1][(w & 1) * 256 +: 256];
        end
    endfunction

    task set_word(input integer msh, input integer w, input [255:0] v);
        begin
            if (msh == 0) begin
                mesh[0].ram.mem[w >> 1][(w & 1) * 256 +: 256] = v;
            end
            else begin
                mesh[1].ram.mem[w >> 1][(w & 1) * 256 +: 256] = v;
            end
        end
    endtask

    // The reference occupant, fed the same beats: mx_quant's arithmetic is its
    // own bench's business, and restating it here would prove nothing.
    reg          g_start = 0, g_bv = 0;
    reg  [255:0] g_beat = 0;
    wire         g_done;
    wire [255:0] g_w0, g_w1, g_w2, g_w3;
    mx_quant u_ref (
        .clk(clk_ctrl), .rst(!rstn),
        .start(g_start), .b_layout(1'b0),
        .beat(g_beat), .beat_valid(g_bv),
        .need_beat(), .done(g_done),
        .word0(g_w0), .word1(g_w1), .word2(g_w2), .word3(g_w3)
    );

    reg [255:0] ref_w [0:NENT*4-1];
    integer     e, b;
    reg [15:0]  sv;

    task reference(input integer ent);
        begin
            @(negedge clk_ctrl); g_start = 1'b1;
            @(negedge clk_ctrl); g_start = 1'b0;
            for (b = 0; b < 8; b = b + 1) begin
                g_beat = dram_word(0, (XSRC_OFF >> 5) + ent*8 + b);
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

    wire pe0 = mesh[0].u.pe_busy;
    wire pe1 = mesh[1].u.pe_busy;
    wire mv0 = mesh[0].u.u_mag.u_mag.u_mag.mv_busy;
    wire mv1 = mesh[1].u.u_mag.u_mag.u_mag.mv_busy;

    task wait_idle(input integer msh);
        begin
            spin = 0;
            while (!(msh == 0 ? pe0 : pe1) && spin < 60000) begin
                @(negedge clk_ctrl); spin = spin + 1;
            end
            chk(spin < 60000, "the processor started", msh);
            spin = 0;
            while (!(msh == 0 ? mv0 : mv1) && spin < 100000) begin
                @(negedge clk_ctrl); spin = spin + 1;
            end
            chk(spin < 100000, "the processor started its mover", msh);
            spin = 0;
            while ((msh == 0 ? mv0 : mv1) && spin < 300000) begin
                @(negedge clk_ctrl); spin = spin + 1;
            end
            chk(spin < 300000, "the mover went idle", msh);
        end
    endtask

    task check_copy(input integer msh, input [8*48-1:0] tag);
        integer k;
        begin
            // A unit retires on its last write SENT, so poll the LAST word.
            spin = 0;
            while (dram_word(msh, (DST_OFF >> 5) + NW - 1)
                   !== {8{32'hAC00_0000 | (NW - 1)}} && spin < 60000) begin
                @(negedge clk_ctrl); spin = spin + 1;
            end
            nbad = 0;
            for (k = 0; k < NW; k = k + 1) begin
                if (dram_word(msh, (DST_OFF >> 5) + k)
                    !== {8{32'hAC00_0000 | k[31:0]}}) nbad = nbad + 1;
            end
            $display("    %0s: %0d of %0d words wrong", tag, nbad, NW);
            chk(nbad == 0, tag, nbad);
        end
    endtask

    initial begin
        for (m = 0; m < 2; m = m + 1) begin
            for (i = 0; i < NW; i = i + 1) begin
                if (m == 0) begin
                    mesh[0].ram.mem[((SRC_OFF >> 5) + i) >> 1]
                        [((((SRC_OFF >> 5) + i) & 1)) * 256 +: 256]
                        = {8{32'hAC00_0000 | i[31:0]}};
                    mesh[0].ram.mem[((DST_OFF >> 5) + i) >> 1]
                        [((((DST_OFF >> 5) + i) & 1)) * 256 +: 256]
                        = {8{32'hA5A5_A5A5}};
                end else begin
                    mesh[1].ram.mem[((SRC_OFF >> 5) + i) >> 1]
                        [((((SRC_OFF >> 5) + i) & 1)) * 256 +: 256]
                        = {8{32'hAC00_0000 | i[31:0]}};
                    mesh[1].ram.mem[((DST_OFF >> 5) + i) >> 1]
                        [((((DST_OFF >> 5) + i) & 1)) * 256 +: 256]
                        = {8{32'h5A5A_5A5A}};
                end
            end
        end

        repeat (40) @(negedge clk_xdma);
        rstn = 1'b1;
        repeat (400) @(negedge clk_xdma);

        chk(pe0 === 1'b0 && pe1 === 1'b0, "both processors idle at rest", 0);

        // A LOCAL move NAMES ITS OWN MESH ID. mag_ilink compares the
        // descriptor's destination mesh against this node's own; a local copy
        // that leaves the field at 0 is a cross-mesh push to mesh 0, which
        // completes with no fault and no local bytes moved.
        //   mesh 0 -> hi32 0x0000_1000, mesh 1 -> hi32 0x0000_1100

        // ---- 1. mesh0's processor: a LOCAL copy, one station link away -----
        $display("--- 1. mesh0's processor runs a local copy ---");
        load_and_kick(0, 32'h0000_1000);
        wait_idle(0);
        check_copy(0, "mesh0 local copy, by its own processor");

        // ---- 2. mesh1's processor: a LOCAL copy, on the host's station -----
        $display("--- 2. mesh1's processor runs a local copy ---");
        load_and_kick(1, 32'h0000_1100);
        wait_idle(1);
        check_copy(1, "mesh1 local copy, by its own processor");

        // ---- 3. mesh0's processor: a CROSS-MESH push into mesh1 ------------
        $display("--- 3. mesh0's processor pushes into mesh1 over the interlink ---");
        for (i = 0; i < NW; i = i + 1) begin
            mesh[1].ram.mem[((DST_OFF >> 5) + i) >> 1]
                [((((DST_OFF >> 5) + i) & 1)) * 256 +: 256] = {8{32'h5A5A_5A5A}};
            mesh[0].ram.mem[((DST_OFF >> 5) + i) >> 1]
                [((((DST_OFF >> 5) + i) & 1)) * 256 +: 256] = {8{32'hA5A5_A5A5}};
        end
        load_and_kick(0, 32'h0000_1100);      // mesh 1 at header bit 40
        wait_idle(0);
        repeat (12000) @(negedge clk_ctrl);
        check_copy(1, "cross-mesh push into mesh1, by mesh0's processor");

        // mesh0's own destination must be untouched: the push went elsewhere.
        chk(dram_word(0, DST_OFF >> 5) === {8{32'hA5A5_A5A5}},
            "mesh0's own destination untouched by its cross-mesh push", 0);

        // ---- 4. a CONVERTING move that lands in the FAR mesh ---------------
        $display("--- 4. mesh0 converts and pushes the result into mesh1 ---");
        for (e = 0; e < NENT; e = e + 1) begin
            for (b = 0; b < 8; b = b + 1) begin
                sv = 16'h3C00 + e[15:0] * 16'd8 + b[15:0];
                set_word(0, (XSRC_OFF >> 5) + e*8 + b, {16{sv}});
            end
        end
        for (i = 0; i < NENT*4; i = i + 1) begin
            set_word(1, (XDST_OFF >> 5) + i, {8{32'h5A5A_5A5A}});
            set_word(0, (XDST_OFF >> 5) + i, {8{32'hA5A5_A5A5}});
        end

        for (e = 0; e < NENT; e = e + 1) begin
            reference(e);
        end

        load_and_kick_xform(0, 32'h0000_1100);   // mesh 1 at header bit 40
        wait_idle(0);
        repeat (12000) @(negedge clk_ctrl);

        chk(mesh[0].u.u_mag.u_mag.u_mag.mv_fault === 4'd0,
            "no fault on the cross-mesh converting move", 0);
        nbad = 0;
        for (i = 0; i < NENT*4; i = i + 1) begin
            if (dram_word(1, (XDST_OFF >> 5) + i) !== ref_w[i]) begin
                nbad = nbad + 1;
            end
        end
        $display("    converted into mesh1: %0d of %0d words wrong",
                 nbad, NENT*4);
        chk(nbad == 0, "the far mesh holds what the occupant produced", nbad);

        // The converted words went to mesh1, so mesh0's own destination and the
        // source it read are both untouched.
        chk(dram_word(0, XDST_OFF >> 5) === {8{32'hA5A5_A5A5}},
            "mesh0's own destination untouched by the converting push", 0);
        chk(dram_word(0, XSRC_OFF >> 5) === {16{16'h3C00}},
            "the source is untouched", 0);

        chk(stat_decerr == 32'd0, "no decode errors anywhere", stat_decerr);

        if (errors == 0) begin
            $display("PASS ctrlpe_mesh2_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL ctrlpe_mesh2_tb: %0d errors, %0d checks",
                     errors, checks);
        end
        $finish;
    end

    initial begin
        #10000000;
        $display("FAIL ctrlpe_mesh2_tb: watchdog  pe0=%b pe1=%b mv0=%b mv1=%b",
                 pe0, pe1, mv0, mv1);
        $finish;
    end
endmodule

`default_nettype wire

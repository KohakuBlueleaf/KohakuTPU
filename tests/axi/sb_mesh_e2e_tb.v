// END TO END: a custom AXI master -> the station bus -> a REAL mesh -> DRAM.

// Every bus bench before this drove block RAM, so the bus had never met a mesh.
// Two ktpu_min_1m: 1 MAG, 1 NoC, 1 matmul, 1 vector each, on two stations.

// FIFTEEN CLOCKS, none sharing an edge with anything it talks to: three manager
// domains, four station fabrics, five per mesh. The crossings are the point.

// v6's map, NOT sb_line4's uniform fixture: mesh MEM at (id+1)<<40 with XLT 0,
// so the mesh id rides twice -- in the window, and at [37:36] of what arrives.

`timescale 1ns / 1ps
`default_nettype none

module sb_mesh_e2e_tb;
    localparam integer AW    = 43;
    localparam integer FW    = 256;
    localparam integer NQ    = 2;
    localparam integer PORTW = 1;
    localparam integer NM    = 3;
    localparam integer NS    = 4 * NQ;
    localparam integer MAXW  = 512;
    localparam integer MAXID = 4;
    localparam integer DSTW  = 2;

    localparam integer MESH_DW = 256;      // mesh S_AXI_MEM
    localparam integer MESH_AW = 40;
    localparam integer DRAM_W  = 512;      // mesh M_AXI_DRAM
    localparam integer IDW     = 4;

    integer errors = 0, checks = 0;

    // ---------------------------------------------------------------- clocks
    reg bclk0 = 0, bclk1 = 0, bclk2 = 0, bclk3 = 0;
    always #1.250 bclk0 = ~bclk0;
    always #1.373 bclk1 = ~bclk1;
    always #1.611 bclk2 = ~bclk2;
    always #1.187 bclk3 = ~bclk3;

    reg clk_ctrl = 0, clk_xdma = 0;
    always #5.000 clk_ctrl = ~clk_ctrl;
    always #2.000 clk_xdma = ~clk_xdma;

    // Station port clocks. Ports 0 and 1 of station s both sit here at NQ=2,
    // so a mesh on port 0 must take clk_s<s> as its axi_aclk.
    reg clk_s0 = 0, clk_s1 = 0, clk_s2 = 0, clk_s3 = 0;
    always #2.109 clk_s0 = ~clk_s0;
    always #1.667 clk_s1 = ~clk_s1;
    always #2.773 clk_s2 = ~clk_s2;
    always #2.373 clk_s3 = ~clk_s3;

    reg clk_ddr0 = 0, clk_ddr1 = 0, clk_ddr2 = 0, clk_ddr3 = 0;
    always #1.667 clk_ddr0 = ~clk_ddr0;
    always #1.712 clk_ddr1 = ~clk_ddr1;
    always #1.623 clk_ddr2 = ~clk_ddr2;
    always #1.749 clk_ddr3 = ~clk_ddr3;

    // axi_aclk IS the MAG clock and noc_clk the fabric, so the station port
    // serving S_AXI_MEM takes axi_aclk. See B11.
    reg noc0 = 0, noc1 = 0;
    always #2.109 noc0 = ~noc0;
    always #1.667 noc1 = ~noc1;
    reg mat0 = 0, vec0 = 0, mag0 = 0, dram0 = 0;
    reg mat1 = 0, vec1 = 0, mag1 = 0, dram1 = 0;
    always #1.451 mat0  = ~mat0;
    always #1.889 vec0  = ~vec0;
    always #2.237 mag0  = ~mag0;
    always #1.913 dram0 = ~dram0;
    always #1.559 mat1  = ~mat1;
    always #2.017 vec1  = ~vec1;
    always #1.783 mag1  = ~mag1;
    always #2.111 dram1 = ~dram1;

    reg rstn = 0;
    wire bus_rst = !rstn;

    // ------------------------------------------------------------ the v6 map
    localparam [AW-1:0] FULL = {AW{1'b1}};
    localparam [AW-1:0] MESH_MASK = {3'b111, {40{1'b0}}};
    localparam [AW-1:0] RAM_MASK  = FULL ^ {{(AW-16){1'b0}}, 16'hFFFF};

    // Built by concatenation, not `(id+1) << 40`: that shift is done in 32-bit
    // integer arithmetic and every window comes out zero.
    function [AW-1:0] mesh_win;
        input integer id;
        reg [2:0] w;
        begin w = id[2:0] + 3'd1; mesh_win = {w, {40{1'b0}}}; end
    endfunction
    function [AW-1:0] ram_win;
        input integer stn;
        reg [2:0] w;
        begin w = stn[2:0] + 3'd1; ram_win = {{(AW-19){1'b0}}, w, 16'd0}; end
    endfunction

    // Segment k = station*NQ + port. Port 0 has XLT 0, so the top three bits are
    // consumed and the mesh sees 40; port 1 is a 64K window passing its base on.
    localparam [NS*AW-1:0] SEG_BASE =
        { ram_win(3), mesh_win(3), ram_win(2), mesh_win(2),
          ram_win(1), mesh_win(1), ram_win(0), mesh_win(0) };
    localparam [NS*AW-1:0] SEG_MASK =
        { RAM_MASK, MESH_MASK, RAM_MASK, MESH_MASK,
          RAM_MASK, MESH_MASK, RAM_MASK, MESH_MASK };
    // XLT 0 everywhere: the window is consumed and each endpoint sees an offset
    // from its own base, so a 512-word RAM is not handed word 16,400.
    localparam [NS*AW-1:0] SEG_XLT = {NS*AW{1'b0}};
    localparam [NS*DSTW-1:0] SEG_DST =
        { 2'd3, 2'd3, 2'd2, 2'd2, 2'd1, 2'd1, 2'd0, 2'd0 };
    localparam [NS*DSTW-1:0] SEG_DPORT =
        { 2'd1, 2'd0, 2'd1, 2'd0, 2'd1, 2'd0, 2'd1, 2'd0 };

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
        .clk_ddr0(clk_ddr0), .aresetn_ddr0(rstn),
        .clk_ddr1(clk_ddr1), .aresetn_ddr1(rstn),
        .clk_ddr2(clk_ddr2), .aresetn_ddr2(rstn),
        .clk_ddr3(clk_ddr3), .aresetn_ddr3(rstn),
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
    wire [IDW-1:0]    dm_awid  [0:1], dm_arid [0:1], dm_bid [0:1], dm_rid [0:1];
    wire [MESH_AW-1:0] dm_awaddr[0:1], dm_araddr[0:1];
    wire [7:0]        dm_awlen [0:1], dm_arlen [0:1];
    wire [2:0]        dm_awsize[0:1], dm_arsize[0:1];
    wire [1:0]        dm_awburst[0:1], dm_arburst[0:1], dm_bresp[0:1], dm_rresp[0:1];
    wire [1:0]        dm_awvalid, dm_awready, dm_arvalid, dm_arready;
    wire [DRAM_W-1:0] dm_wdata [0:1], dm_rdata[0:1];
    wire [DRAM_W/8-1:0] dm_wstrb[0:1];
    wire [1:0]        dm_wlast, dm_wvalid, dm_wready;
    wire [1:0]        dm_bvalid, dm_bready, dm_rlast, dm_rvalid, dm_rready;

    genvar m;
    generate for (m = 0; m < 2; m = m + 1) begin : mesh
        localparam integer Q  = (m == 0) ? 0 : 2;
        wire aclk = (m == 0) ? noc0 : noc1;
        wire mclk = (m == 0) ? mag0   : mag1;
        wire tclk = (m == 0) ? mat0   : mat1;
        wire vclk = (m == 0) ? vec0   : vec1;
        wire dclk = (m == 0) ? dram0  : dram1;

        ktpu_min_1m #(.MESH_ID(m), .MODEL(1), .MW(DRAM_W),
                      .MAG_CDC(1), .UNIT_CDC(1)) u (
            .axi_aclk(mclk), .axi_aresetn(rstn),
            .noc_clk(aclk), .mat_clk(tclk), .vec_clk(vclk),
            .dram_aclk(dclk), .dram_aresetn(rstn),

            .S_AXI_MEM_awid   (sp_awid  [Q*MAXID +: IDW]),
            .S_AXI_MEM_awaddr (sp_awaddr[Q*AW    +: MESH_AW]),
            .S_AXI_MEM_awlen  (sp_awlen [Q*8     +: 8]),
            .S_AXI_MEM_awvalid(sp_awvalid[Q]),
            .S_AXI_MEM_awready(sp_awready[Q]),
            .S_AXI_MEM_wdata  (sp_wdata [Q*MAXW  +: MESH_DW]),
            .S_AXI_MEM_wstrb  (sp_wstrb [Q*(MAXW/8) +: MESH_DW/8]),
            .S_AXI_MEM_wlast  (sp_wlast [Q]),
            .S_AXI_MEM_wvalid (sp_wvalid[Q]),
            .S_AXI_MEM_wready (sp_wready[Q]),
            .S_AXI_MEM_bid    (sp_bid   [Q*MAXID +: IDW]),
            .S_AXI_MEM_bresp  (sp_bresp [Q*2     +: 2]),
            .S_AXI_MEM_bvalid (sp_bvalid[Q]),
            .S_AXI_MEM_bready (sp_bready[Q]),
            .S_AXI_MEM_arid   (sp_arid  [Q*MAXID +: IDW]),
            .S_AXI_MEM_araddr (sp_araddr[Q*AW    +: MESH_AW]),
            .S_AXI_MEM_arlen  (sp_arlen [Q*8     +: 8]),
            .S_AXI_MEM_arvalid(sp_arvalid[Q]),
            .S_AXI_MEM_arready(sp_arready[Q]),
            .S_AXI_MEM_rid    (sp_rid   [Q*MAXID +: IDW]),
            .S_AXI_MEM_rdata  (sp_rdata [Q*MAXW  +: MESH_DW]),
            .S_AXI_MEM_rresp  (sp_rresp [Q*2     +: 2]),
            .S_AXI_MEM_rlast  (sp_rlast [Q]),
            .S_AXI_MEM_rvalid (sp_rvalid[Q]),
            .S_AXI_MEM_rready (sp_rready[Q]),

            .S_AXI_CTRL_awid({IDW{1'b0}}), .S_AXI_CTRL_awaddr(32'd0),
            .S_AXI_CTRL_awlen(8'd0), .S_AXI_CTRL_awvalid(1'b0),
            .S_AXI_CTRL_awready(),
            .S_AXI_CTRL_wdata(64'd0), .S_AXI_CTRL_wstrb(8'd0),
            .S_AXI_CTRL_wlast(1'b1), .S_AXI_CTRL_wvalid(1'b0),
            .S_AXI_CTRL_wready(),
            .S_AXI_CTRL_bid(), .S_AXI_CTRL_bresp(), .S_AXI_CTRL_bvalid(),
            .S_AXI_CTRL_bready(1'b1),
            .S_AXI_CTRL_arid({IDW{1'b0}}), .S_AXI_CTRL_araddr(32'd0),
            .S_AXI_CTRL_arlen(8'd0), .S_AXI_CTRL_arvalid(1'b0),
            .S_AXI_CTRL_arready(),
            .S_AXI_CTRL_rid(), .S_AXI_CTRL_rdata(), .S_AXI_CTRL_rresp(),
            .S_AXI_CTRL_rlast(), .S_AXI_CTRL_rvalid(), .S_AXI_CTRL_rready(1'b1),

            .M_AXI_DRAM_awid(dm_awid[m]), .M_AXI_DRAM_awaddr(dm_awaddr[m]),
            .M_AXI_DRAM_awlen(dm_awlen[m]), .M_AXI_DRAM_awsize(dm_awsize[m]),
            .M_AXI_DRAM_awburst(dm_awburst[m]),
            .M_AXI_DRAM_awvalid(dm_awvalid[m]), .M_AXI_DRAM_awready(dm_awready[m]),
            .M_AXI_DRAM_wdata(dm_wdata[m]), .M_AXI_DRAM_wstrb(dm_wstrb[m]),
            .M_AXI_DRAM_wlast(dm_wlast[m]), .M_AXI_DRAM_wvalid(dm_wvalid[m]),
            .M_AXI_DRAM_wready(dm_wready[m]),
            .M_AXI_DRAM_bid(dm_bid[m]), .M_AXI_DRAM_bresp(dm_bresp[m]),
            .M_AXI_DRAM_bvalid(dm_bvalid[m]), .M_AXI_DRAM_bready(dm_bready[m]),
            .M_AXI_DRAM_arid(dm_arid[m]), .M_AXI_DRAM_araddr(dm_araddr[m]),
            .M_AXI_DRAM_arlen(dm_arlen[m]), .M_AXI_DRAM_arsize(dm_arsize[m]),
            .M_AXI_DRAM_arburst(dm_arburst[m]),
            .M_AXI_DRAM_arvalid(dm_arvalid[m]), .M_AXI_DRAM_arready(dm_arready[m]),
            .M_AXI_DRAM_rid(dm_rid[m]), .M_AXI_DRAM_rdata(dm_rdata[m]),
            .M_AXI_DRAM_rresp(dm_rresp[m]), .M_AXI_DRAM_rlast(dm_rlast[m]),
            .M_AXI_DRAM_rvalid(dm_rvalid[m]), .M_AXI_DRAM_rready(dm_rready[m]),

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
                  .WORDS(2048), .PORTS(1)) ram (
            .clk(dclk), .resetn(rstn),
            .s_awid(dm_awid[m]), .s_awaddr(dm_awaddr[m]), .s_awlen(dm_awlen[m]),
            .s_awsize(dm_awsize[m]), .s_awburst(dm_awburst[m]),
            .s_awvalid(dm_awvalid[m]), .s_awready(dm_awready[m]),
            .s_wdata(dm_wdata[m]), .s_wstrb(dm_wstrb[m]), .s_wlast(dm_wlast[m]),
            .s_wvalid(dm_wvalid[m]), .s_wready(dm_wready[m]),
            .s_bid(dm_bid[m]), .s_bresp(dm_bresp[m]), .s_bvalid(dm_bvalid[m]),
            .s_bready(dm_bready[m]),
            .s_arid(dm_arid[m]), .s_araddr(dm_araddr[m]), .s_arlen(dm_arlen[m]),
            .s_arsize(dm_arsize[m]), .s_arburst(dm_arburst[m]),
            .s_arvalid(dm_arvalid[m]), .s_arready(dm_arready[m]),
            .s_rid(dm_rid[m]), .s_rdata(dm_rdata[m]), .s_rresp(dm_rresp[m]),
            .s_rlast(dm_rlast[m]), .s_rvalid(dm_rvalid[m]),
            .s_rready(dm_rready[m]),
            .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({DRAM_W{1'b0}}), .bd_rdata()
        );
    end endgenerate

    // ---- every other station port gets a plain RAM, so no NSU dangles -------
    genvar q;
    generate for (q = 0; q < NS; q = q + 1) begin : ep
        if (q != 0 && q != 2) begin : g_ram
            localparam integer DW = (q % NQ == 0) ? FW : 32;
            axi_ram #(.DATA_W(DW), .ADDR_W(AW), .ID_W(MAXID),
                      .WORDS(512), .PORTS(1)) r (
                .clk(sclk[q / NQ]), .resetn(rstn),
                .s_awid(sp_awid[q*MAXID +: MAXID]),
                .s_awaddr(sp_awaddr[q*AW +: AW]),
                .s_awlen(sp_awlen[q*8 +: 8]),
                .s_awsize(sp_awsize[q*3 +: 3]),
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
                .s_arlen(sp_arlen[q*8 +: 8]),
                .s_arsize(sp_arsize[q*3 +: 3]),
                .s_arburst(sp_arburst[q*2 +: 2]),
                .s_arvalid(sp_arvalid[q]), .s_arready(sp_arready[q]),
                .s_rid(sp_rid[q*MAXID +: MAXID]),
                .s_rdata(sp_rdata[q*MAXW +: DW]),
                .s_rresp(sp_rresp[q*2 +: 2]), .s_rlast(sp_rlast[q]),
                .s_rvalid(sp_rvalid[q]), .s_rready(sp_rready[q]),
                .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({DW{1'b0}}),
                .bd_rdata()
            );
        end
    end endgenerate

    // ------------------------------------------------ the custom AXI masters
    // Manager 1 is 512-bit, so a beat splits 2:1 onto the 256-bit fabric.
    // Drive and sample at NEGEDGE: at the posedge these read pre-NBA values.
    integer spin;

    // Manager 0 is the 100 MHz jtag port and 1/2 are the 250 MHz XDMA pair, so a
    // task that assumed one clock handshook against the wrong edge for manager 0.
    task ntick(input integer mg);
        begin if (mg == 0) @(negedge clk_ctrl); else @(negedge clk_xdma); end
    endtask
    task ptick(input integer mg);
        begin if (mg == 0) @(posedge clk_ctrl); else @(posedge clk_xdma); end
    endtask

    task mwrite(input integer mg, input [AW-1:0] a, input [7:0] len,
                input [2:0] sz, input [MAXW-1:0] d0, input [MAXW-1:0] d1);
        integer beat;
        begin
            ntick(mg);
            mp_awaddr[mg*AW +: AW] = a;
            mp_awlen [mg*8  +: 8]  = len;
            mp_awsize[mg*3  +: 3]  = sz;
            mp_awvalid[mg] = 1'b1;
            spin = 0;
            while (spin < 20000) begin
                ptick(mg);
                if (mp_awready[mg]) spin = 30000; else spin = spin + 1;
            end
            ntick(mg); mp_awvalid[mg] = 1'b0;

            for (beat = 0; beat <= len; beat = beat + 1) begin
                ntick(mg);
                mp_wdata[mg*MAXW +: MAXW] = (beat == 0) ? d0 : d1;
                mp_wstrb[mg*(MAXW/8) +: MAXW/8] = {(MAXW/8){1'b1}};
                mp_wlast[mg]  = (beat == len);
                mp_wvalid[mg] = 1'b1;
                spin = 0;
                while (spin < 20000) begin
                    ptick(mg);
                    if (mp_wready[mg]) spin = 30000; else spin = spin + 1;
                end
                ntick(mg); mp_wvalid[mg] = 1'b0;
            end
            spin = 0;
            while (spin < 20000 && !mp_bvalid[mg]) begin
                ntick(mg); spin = spin + 1;
            end
        end
    endtask

    reg [MAXW-1:0] rd_beat [0:15];
    integer        rd_n;

    task mread(input integer mg, input [AW-1:0] a, input [7:0] len,
               input [2:0] sz);
        begin
            rd_n = 0;
            ntick(mg);
            mp_araddr[mg*AW +: AW] = a;
            mp_arlen [mg*8  +: 8]  = len;
            mp_arsize[mg*3  +: 3]  = sz;
            mp_arvalid[mg] = 1'b1;
            spin = 0;
            while (spin < 20000) begin
                ptick(mg);
                if (mp_arready[mg]) spin = 30000; else spin = spin + 1;
            end
            ntick(mg); mp_arvalid[mg] = 1'b0;
            spin = 0;
            while (spin < 40000 && rd_n <= len) begin
                ntick(mg);
                if (mp_rvalid[mg]) begin
                    rd_beat[rd_n] = mp_rdata[mg*MAXW +: MAXW];
                    rd_n = rd_n + 1;
                end
                spin = spin + 1;
            end
        end
    endtask

    // Handshake counters at each hop, so a break says WHERE rather than that the
    // data did not arrive: manager -> station port -> mesh DRAM master.
    integer n_m1_aw = 0, n_m1_w = 0, n_m1_b = 0;
    always @(posedge clk_xdma) if (rstn) begin
        if (mp_awvalid[1] && mp_awready[1]) n_m1_aw = n_m1_aw + 1;
        if (mp_wvalid[1]  && mp_wready[1])  n_m1_w  = n_m1_w  + 1;
        if (mp_bvalid[1])                   n_m1_b  = n_m1_b  + 1;
    end
    integer n_s0_aw = 0, n_s0_w = 0, v_s0_aw = 0, r_s0_aw = 0;
    reg [MESH_AW-1:0] seen_awaddr = 0;
    reg [7:0]         seen_awlen  = 8'hFF;
    always @(posedge sclk[0]) if (rstn) begin
        if (sp_awvalid[0]) begin
            v_s0_aw = v_s0_aw + 1;
            seen_awaddr = sp_awaddr[0*AW +: MESH_AW];
            seen_awlen  = sp_awlen[0*8 +: 8];
        end
        if (sp_awready[0]) r_s0_aw = r_s0_aw + 1;
        if (sp_awvalid[0] && sp_awready[0]) n_s0_aw = n_s0_aw + 1;
        if (sp_wvalid[0]  && sp_wready[0])  n_s0_w  = n_s0_w  + 1;
    end
    integer n_d0_aw = 0, n_d0_w = 0;
    always @(posedge dram0) if (rstn) begin
        if (dm_awvalid[0] && dm_awready[0]) n_d0_aw = n_d0_aw + 1;
        if (dm_wvalid[0]  && dm_wready[0])  n_d0_w  = n_d0_w  + 1;
    end

    task hops;
        begin
            $display("    hops: mgr1 aw=%0d w=%0d b=%0d | stn0p0 awv=%0d awr=%0d aw=%0d w=%0d | dram0 aw=%0d w=%0d",
                     n_m1_aw, n_m1_w, n_m1_b, v_s0_aw, r_s0_aw, n_s0_aw, n_s0_w,
                     n_d0_aw, n_d0_w);
            $display("    stn0p0 saw awaddr=%h awlen=%0d", seen_awaddr, seen_awlen);
        end
    endtask

    task chk(input cond, input [255:0] what, input integer where);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                if (errors < 12) $display("  FAIL %0s [%0d]", what, where);
            end
        end
    endtask

    // ----------------------------------------------------------------- checks
    localparam [39:0] OFF = 40'h2000;
    localparam [MAXW-1:0] PAT0 = {8{64'hE2E0_0000_1111_2222}};
    localparam [MAXW-1:0] PAT1 = {8{64'hE2E1_3333_4444_5555}};

    integer i;
    initial begin
        for (i = 0; i < 2048; i = i + 1) begin
            mesh[0].ram.mem[i] = {DRAM_W{1'b0}};
            mesh[1].ram.mem[i] = {DRAM_W{1'b0}};
        end
        repeat (40) @(negedge clk_xdma);
        rstn = 1'b1;
        repeat (200) @(negedge clk_xdma);

        // Mesh 0 is on station 0, one link hop from the managers on station 1.
        $display("--- 1. master -> bus -> mesh0 S_AXI_MEM -> MAG -> DRAM ---");
        mwrite(1, mesh_win(0) | {3'd0, 2'd0, 2'd0, 36'd0} | {3'd0, OFF},
               8'd1, 3'd6, PAT0, PAT1);
        repeat (4000) @(negedge clk_xdma);
        hops;
        chk(mesh[0].ram.mem[OFF >> 6] === PAT0, "mesh0 DRAM took beat 0", 0);

        $display("--- 2. same path into mesh1, on its own station and clocks ---");
        mwrite(1, mesh_win(1) | {3'd0, 2'd0, 2'd1, 36'd0} | {3'd0, OFF},
               8'd1, 3'd6, PAT0, PAT1);
        repeat (4000) @(negedge clk_xdma);
        hops;
        chk(mesh[1].ram.mem[OFF >> 6] === PAT0, "mesh1 DRAM took beat 0", 0);

        $display("--- 3. read mesh0 back through the bus ---");
        mread(1, mesh_win(0) | {3'd0, OFF}, 8'd0, 3'd6);
        chk(rd_n > 0, "mesh0 read returned a beat", 0);
        if (rd_n > 0) chk(rd_beat[0] === PAT0, "mesh0 read data matches", 0);

        $display("--- 4. the narrow endpoints, 32-bit manager 0 ---");
        mwrite(0, ram_win(0) | 43'h40, 8'd0, 3'd2,
               {(MAXW/32){32'hCAFE_0001}}, {MAXW{1'b0}});
        repeat (600) @(negedge clk_xdma);
        mread(0, ram_win(0) | 43'h40, 8'd0, 3'd2);
        chk(rd_n > 0, "narrow endpoint returned a beat", 0);
        if (rd_n > 0) begin
            $display("    narrow read = %h  (want CAFE0001), beats=%0d",
                     rd_beat[0][31:0], rd_n);
            chk(rd_beat[0][31:0] === 32'hCAFE_0001, "narrow data matches", 0);
        end

        chk(stat_decerr == 32'd0, "no decode errors anywhere", 0);

        if (errors == 0)
            $display("  PASS sb_mesh_e2e_tb: %0d checks", checks);
        else
            $display("  FAIL sb_mesh_e2e_tb: %0d errors, %0d checks",
                     errors, checks);
        $finish;
    end

    initial begin
        #8000000;
        $display("  FAIL sb_mesh_e2e_tb: watchdog");
        $finish;
    end
endmodule

`default_nettype wire

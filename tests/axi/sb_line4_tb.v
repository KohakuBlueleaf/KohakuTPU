// sb_line4: four stations on a line, managers on station 1. Proves multi-hop
// routing -- station 1 to station 3 is forwarded THROUGH station 2.

// Drive and sample at NEGEDGE, transfer at the posedge between. Sampling right
// after a posedge reads pre-NBA values and reports X for every burst.

`timescale 1ns / 1ps
`default_nettype none

module sb_line4_tb;
`ifdef SB_AW32
    localparam integer AW = 32;
`else
    localparam integer AW = 40;
`endif
    localparam integer MAXW  = 512;
    localparam integer MAXID = 4;
    localparam integer NM    = 3;
    // -d SB_NQ4 is the v6 shape: mesh MEM, mesh CTRL, DDR ctrl, clk_wiz, which
    // sb_line4's port_dom() spreads over three clock domains per station.
`ifdef SB_NQ4
    localparam integer NQ    = 4;
    localparam integer PORTW = 2;
`else
    localparam integer NQ    = 2;
    localparam integer PORTW = 1;
`endif
    localparam integer NS    = 4 * NQ;
    localparam real    TS    = 0.001;

    integer errors = 0;
    integer checks = 0;

    // Phase 13. The wide manager is 512-bit regardless of FW (awsize 3'd6).
    localparam integer P_MW = 512;
    reg [31:0] xcyc = 0;
    reg [31:0] bw_t0, bw_t1;
    integer    bw_i;
    integer    f4sp;
    reg [1:0] exp_b = 2'b00;

    reg bus_clk = 0, clk_ctrl = 0, clk_xdma = 0;
    reg clk_s0 = 0, clk_s1 = 0, clk_s2 = 0, clk_s3 = 0;
    // FOUR, deliberately unequal: each SLR's MIG has its own ui_clk, so one
    // shared clock here would leave three of the four crossings untested.
    reg clk_ddr0 = 0, clk_ddr1 = 0, clk_ddr2 = 0, clk_ddr3 = 0;
    always begin  // 299.9 MHz
        #1.667 clk_ddr0 = ~clk_ddr0;
    end
    always begin  // 292.1 MHz
        #1.712 clk_ddr1 = ~clk_ddr1;
    end
    always begin  // 308.1 MHz
        #1.623 clk_ddr2 = ~clk_ddr2;
    end
    always begin  // 285.9 MHz
        #1.749 clk_ddr3 = ~clk_ddr3;
    end
    wire [3:0] tb_dclk = {clk_ddr3, clk_ddr2, clk_ddr1, clk_ddr0};
    always begin  // 400.0 MHz
        #1.250 bus_clk  = ~bus_clk;
    end
    always begin  // 100.0 MHz
        #5.000 clk_ctrl = ~clk_ctrl;
    end
    always begin  // 250.0 MHz
        #2.000 clk_xdma = ~clk_xdma;
    end
    always begin  // 237.1 MHz
        #2.109 clk_s0   = ~clk_s0;
    end
    always begin  // 299.9 MHz
        #1.667 clk_s1   = ~clk_s1;
    end
    always begin  // 180.3 MHz
        #2.773 clk_s2   = ~clk_s2;
    end
    always begin  // 210.7 MHz
        #2.373 clk_s3   = ~clk_s3;
    end

    reg rstn = 0;
    wire bus_rst = !rstn;

    // -d SB_LINKCDC gives each station its own fabric clock, deliberately
    // unrelated, so the link's crossing is exercised rather than assumed.
`ifdef SB_LINKCDC
    localparam integer P_CDC = 1;
    reg bclk1 = 0, bclk2 = 0, bclk3 = 0;
    always begin  // 364.2 MHz
        #1.373 bclk1 = ~bclk1;
    end
    always begin  // 310.4 MHz
        #1.611 bclk2 = ~bclk2;
    end
    always begin  // 421.2 MHz
        #1.187 bclk3 = ~bclk3;
    end
    wire [3:0] bclk = {bclk3, bclk2, bclk1, bus_clk};
`else
    localparam integer P_CDC = 0;
    wire [3:0] bclk = {4{bus_clk}};
`endif
    wire [3:0] brst = {4{bus_rst}};

    // A line: station s owns sp[s*NQ .. s*NQ+NQ-1]. No special case for 1.
    function [AW-1:0] adr;
        input integer stn, ep;
        begin adr = (stn << (AW - 4)) | (ep << 16); end
    endfunction

    reg  [MAXID-1:0]  awid   [0:NM-1];
    reg  [AW-1:0]     awaddr [0:NM-1];
    reg  [7:0]        awlen  [0:NM-1];
    reg  [2:0]        awsize [0:NM-1];
    reg               awvld  [0:NM-1];
    reg  [MAXW-1:0]   wdata  [0:NM-1];
    reg  [MAXW/8-1:0] wstrb  [0:NM-1];
    reg               wlast  [0:NM-1];
    reg               wvld   [0:NM-1];
    reg               brdy   [0:NM-1];
    reg  [MAXID-1:0]  arid   [0:NM-1];
    reg  [AW-1:0]     araddr [0:NM-1];
    reg  [7:0]        arlen  [0:NM-1];
    reg  [2:0]        arsize [0:NM-1];
    reg               arvld  [0:NM-1];
    reg               rrdy   [0:NM-1];

    wire [NM-1:0]      awrdy, wrdy, bvld, arrdy, rvld, rlast;
    wire [NM*2-1:0]    bresp_v, rresp_v;
    wire [NM*MAXW-1:0] rdata_v;

`ifdef SB_CRED1
    localparam integer P_CRED = 1;
`else
    localparam integer P_CRED = 16;
`endif
`ifdef SB_FW256
    localparam integer P_FW = 256;
`else
    localparam integer P_FW = 512;
`endif

    // Manager 0 is the jtag path at its own 64 bits, straight into the DUT:
    // the NMU's packer is what has to turn those beats into whole flits.
    localparam integer JW = 64;
    wire             j_awrdy, j_wrdy, j_bvld, j_arrdy, j_rvld, j_rlast;
    wire [1:0]       j_bresp, j_rresp;
    wire [JW-1:0]    j_rdata;
    wire [MAXID-1:0] j_bid, j_rid;

    assign j_awrdy = awrdy[0];
    assign j_wrdy  = wrdy[0];
    assign j_bvld  = bvld[0];
    assign j_bresp = bresp_v[1:0];
    assign j_arrdy = arrdy[0];
    assign j_rvld  = rvld[0];
    assign j_rlast = rlast[0];
    assign j_rdata = rdata_v[JW-1:0];
    assign j_rresp = rresp_v[1:0];
    assign j_bid   = {MAXID{1'b0}};
    assign j_rid   = {MAXID{1'b0}};

    wire [NM*MAXID-1:0]  mp_awid   = {awid[2], awid[1], awid[0]};
    wire [NM*AW-1:0]     mp_awaddr = {awaddr[2], awaddr[1], awaddr[0]};
    wire [NM*8-1:0]      mp_awlen  = {awlen[2], awlen[1], awlen[0]};
    wire [NM*3-1:0]      mp_awsize = {awsize[2], awsize[1], awsize[0]};
    wire [NM-1:0]        mp_awvalid = {awvld[2], awvld[1], awvld[0]};
    wire [NM*MAXW-1:0]   mp_wdata  = {wdata[2], wdata[1], wdata[0]};
    wire [NM*MAXW/8-1:0] mp_wstrb  = {wstrb[2], wstrb[1], wstrb[0]};
    wire [NM-1:0]        mp_wlast  = {wlast[2], wlast[1], wlast[0]};
    wire [NM-1:0]        mp_wvalid = {wvld[2], wvld[1], wvld[0]};
    wire [NM-1:0]        mp_bready = {brdy[2], brdy[1], brdy[0]};
    wire [NM*MAXID-1:0]  mp_arid   = {arid[2], arid[1], arid[0]};
    wire [NM*AW-1:0]     mp_araddr = {araddr[2], araddr[1], araddr[0]};
    wire [NM*8-1:0]      mp_arlen  = {arlen[2], arlen[1], arlen[0]};
    wire [NM*3-1:0]      mp_arsize = {arsize[2], arsize[1], arsize[0]};
    wire [NM-1:0]        mp_arvalid = {arvld[2], arvld[1], arvld[0]};
    wire [NM-1:0]        mp_rready = {rrdy[2], rrdy[1], rrdy[0]};

    wire [NS*MAXID-1:0]  sp_awid, sp_arid, sp_bid, sp_rid;
    wire [NS*AW-1:0]     sp_awaddr, sp_araddr;
    wire [NS*8-1:0]      sp_awlen, sp_arlen;
    wire [NS*3-1:0]      sp_awsize, sp_arsize;
    wire [NS*2-1:0]      sp_awburst, sp_arburst, sp_bresp, sp_rresp;
    wire [NS-1:0]        sp_awvalid, sp_awready, sp_wvalid, sp_wready;
    wire [NS-1:0]        sp_wlast, sp_bvalid, sp_bready;
    wire [NS-1:0]        sp_arvalid, sp_arready, sp_rvalid, sp_rready, sp_rlast;
    wire [NS*MAXW-1:0]   sp_wdata, sp_rdata;
    wire [NS*MAXW/8-1:0] sp_wstrb;
    wire [31:0]          stat_decerr;

    // -d SB_WIDE512 keeps port 0 a 512-bit slave whatever the flit width is:
    // the wide-slave-behind-narrow-fabric case sb_nsu's SDW<=FW rule forbids.
`ifdef SB_WIDE512
    localparam integer P_WIDE = 512;
`else
    localparam integer P_WIDE = P_FW;
`endif

`ifdef SB_HALFLINK
    localparam integer P_FULL = 0;
`else
    localparam integer P_FULL = 1;
`endif

    // MIN_AREA is the speed-for-size corner: one outstanding and no
    // packet-complete token, so the hub holds its grant through an underrun.
`ifdef SB_MINAREA
    localparam integer P_OST = 1, P_SF = 0;
`else
    localparam integer P_OST = 4, P_SF = 1;
`endif
`ifdef SB_TIMEOUT
    localparam integer P_TO = 4000;
`else
    localparam integer P_TO = 0;
`endif

    // -d SB_MIXPRESET gives all four stations DIFFERENT shim settings at once,
    // so a heterogeneous line is tested rather than asserted to work.
`ifdef SB_MIXPRESET
    localparam integer M0 = 1, M1 = 4, M2 = 8, M3 = 2;   // outstanding
    localparam integer F0 = 0, F1 = 1, F2 = 1, F3 = 0;   // store-and-forward
    localparam integer B0 = 0, B1 = 820, B2 = 0, B3 = 820;   // BRAM trade
    localparam integer T0 = 0, T1 = 0, T2 = 4000, T3 = 0;    // timeout
`else
    localparam integer M0 = P_OST, M1 = P_OST, M2 = P_OST, M3 = P_OST;
    localparam integer F0 = P_SF, F1 = P_SF, F2 = P_SF, F3 = P_SF;
    localparam integer B0 = 0, B1 = 0, B2 = 0, B3 = 0;
    localparam integer T0 = P_TO, T1 = P_TO, T2 = P_TO, T3 = P_TO;
`endif

`ifdef SB_ISKID
    localparam integer P_ISKID = 1;
`else
    localparam integer P_ISKID = 0;
`endif
    // -d SB_SHRINK exercises the per-manager NMU shrink: JTAG OUTST=4 (serial,
    // keeps pack+256-burst), xdma OUTST=8, ctrl OUTST=2 + FORCE_PLACE. Manager 2
    // FORCE_PLACE is valid only against WORD subs -- the bench proves it.
`ifdef SB_SHRINK
    localparam integer MG0 = 4, MG1 = 8, MG2 = 2;
    localparam integer MP0 = 0, MP1 = 0, MP2 = 1;
`else
    localparam integer MG0 = 0, MG1 = 0, MG2 = 0;
    localparam integer MP0 = 0, MP1 = 0, MP2 = 0;
`endif
    // -d SB_MREQ1 / SB_MRSP1 / SB_LPB1: the xdma manager's queue depths and
    // station 1's own threshold, the S2 station tier. Defaults are the line's.
`ifndef SB_MREQ1
    `define SB_MREQ1 256
`endif
`ifndef SB_MRSP1
    `define SB_MRSP1 256
`endif
`ifndef SB_LPB1
    `define SB_LPB1 B1
`endif
`ifdef SB_NSB
    localparam integer P_NSB = 1;   // 32-bit subs as single-beat config ports
`else
    localparam integer P_NSB = 0;
`endif
`ifdef SB_KTS
    localparam integer P_KTS = 1;   // one surface per direction, not four links
`else
    localparam integer P_KTS = 0;
`endif
    // -d SB_MGR0BUS: the jtag manager on station 1's bus clock (MGR0_DOM 1,
    // synchronous NMU queues), so the jtag tasks drive on that clock.
`ifdef SB_MGR0BUS
    localparam integer P_M0 = 1;
    wire jclk = bclk[1];
`else
    localparam integer P_M0 = 0;
    wire jclk = clk_ctrl;
`endif
    sb_line4 #(.AW(AW), .FW(P_FW), .NQ(NQ), .PORTW(PORTW), .CRED(P_CRED),
               .LINK_CDC(P_CDC), .LINK_FULL(P_FULL), .LINK_KTS(P_KTS),
               .OST(P_OST),
               .STORE_FWD(P_SF), .TIMEOUT(P_TO), .WIDE_DW(P_WIDE), .ISKID(P_ISKID),
               .NSB(P_NSB), .MGR0_DOM(P_M0),
               .MOST0(MG0), .MOST1(MG1), .MOST2(MG2),
               .MPLC0(MP0), .MPLC1(MP1), .MPLC2(MP2),
               .MREQ1(`SB_MREQ1), .MRSP1(`SB_MRSP1),
               .OST0(M0), .OST1(M1), .OST2(M2), .OST3(M3),
               .SFW0(F0), .SFW1(F1), .SFW2(F2), .SFW3(F3),
               .LPB0(B0), .LPB1(`SB_LPB1), .LPB2(B2), .LPB3(B3),
               .TMO0(T0), .TMO1(T1), .TMO2(T2), .TMO3(T3)) u_dut (
        .bus_clk0(bclk[0]), .bus_rst0(brst[0]),
        .bus_clk1(bclk[1]), .bus_rst1(brst[1]),
        .bus_clk2(bclk[2]), .bus_rst2(brst[2]),
        .bus_clk3(bclk[3]), .bus_rst3(brst[3]),
        .clk_ctrl(clk_ctrl), .aresetn_ctrl(rstn),
        .clk_xdma(clk_xdma), .aresetn_xdma(rstn),
        .clk_s0(clk_s0), .aresetn_s0(rstn),
        .clk_s1(clk_s1), .aresetn_s1(rstn),
        .clk_s2(clk_s2), .aresetn_s2(rstn),
        .clk_s3(clk_s3), .aresetn_s3(rstn),
        .clk_ddr0(clk_ddr0), .aresetn_ddr0(rstn),
        .clk_ddr1(clk_ddr1), .aresetn_ddr1(rstn),
        .clk_ddr2(clk_ddr2), .aresetn_ddr2(rstn),
        .clk_ddr3(clk_ddr3), .aresetn_ddr3(rstn),
        .mp_awid(mp_awid), .mp_awaddr(mp_awaddr), .mp_awlen(mp_awlen),
        .mp_awsize(mp_awsize), .mp_awburst({NM{2'b01}}),
        .mp_awvalid(mp_awvalid), .mp_awready(awrdy),
        .mp_wdata(mp_wdata), .mp_wstrb(mp_wstrb), .mp_wlast(mp_wlast),
        .mp_wvalid(mp_wvalid), .mp_wready(wrdy),
        .mp_bid(), .mp_bresp(bresp_v), .mp_bvalid(bvld), .mp_bready(mp_bready),
        .mp_arid(mp_arid), .mp_araddr(mp_araddr), .mp_arlen(mp_arlen),
        .mp_arsize(mp_arsize), .mp_arburst({NM{2'b01}}),
        .mp_arvalid(mp_arvalid), .mp_arready(arrdy),
        .mp_rid(), .mp_rdata(rdata_v), .mp_rresp(rresp_v), .mp_rlast(rlast),
        .mp_rvalid(rvld), .mp_rready(mp_rready),
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

    // Must mirror sb_line4's port_dom(): ports 0,1 mesh, 2 ddr, rest ctrl.
    wire [3:0]    tb_mclk = {clk_s3, clk_s2, clk_s1, clk_s0};
    wire [NS-1:0] sclk;
    genvar q;
    generate
    for (q = 0; q < NS; q = q + 1) begin : g_sclk
        localparam integer QS = q / NQ;
        localparam integer QP = q % NQ;
        assign sclk[q] = (QP < 2)  ? tb_mclk[QS]
                       : (QP == 2) ? tb_dclk[QS] : clk_ctrl;
    end
    endgenerate

    wire [NM-1:0]    mclk = {clk_xdma, clk_xdma, jclk};
    wire [32*NM-1:0] mchk_err, mchk_txn;
    wire [32*NS-1:0] schk_err, schk_txn;

    genvar c;
    generate
    for (c = 0; c < NM; c = c + 1) begin : g_chk_m
        sb_axi_check #(.DW(MAXW), .AWD(AW), .NAME("mgr")) u_chk (
            .clk(mclk[c]), .resetn(rstn), .nerr(mchk_err[c*32 +: 32]),
            .ntxn(mchk_txn[c*32 +: 32]),
            .awaddr(mp_awaddr[c*AW +: AW]), .awlen(mp_awlen[c*8 +: 8]),
            .awvalid(mp_awvalid[c]), .awready(awrdy[c]),
            .wdata(mp_wdata[c*MAXW +: MAXW]), .wlast(mp_wlast[c]),
            .wvalid(mp_wvalid[c]), .wready(wrdy[c]),
            .bvalid(bvld[c]), .bready(mp_bready[c]),
            .araddr(mp_araddr[c*AW +: AW]), .arlen(mp_arlen[c*8 +: 8]),
            .arvalid(mp_arvalid[c]), .arready(arrdy[c]),
            .rlast(rlast[c]), .rvalid(rvld[c]), .rready(mp_rready[c]));
    end
    for (c = 0; c < NS; c = c + 1) begin : g_chk_s
        sb_axi_check #(.DW(MAXW), .AWD(AW), .NAME("sub")) u_chk (
            .clk(sclk[c]), .resetn(rstn), .nerr(schk_err[c*32 +: 32]),
            .ntxn(schk_txn[c*32 +: 32]),
            .awaddr(sp_awaddr[c*AW +: AW]), .awlen(sp_awlen[c*8 +: 8]),
            .awvalid(sp_awvalid[c]), .awready(sp_awready[c]),
            .wdata(sp_wdata[c*MAXW +: MAXW]), .wlast(sp_wlast[c]),
            .wvalid(sp_wvalid[c]), .wready(sp_wready[c]),
            .bvalid(sp_bvalid[c]), .bready(sp_bready[c]),
            .araddr(sp_araddr[c*AW +: AW]), .arlen(sp_arlen[c*8 +: 8]),
            .arvalid(sp_arvalid[c]), .arready(sp_arready[c]),
            .rlast(sp_rlast[c]), .rvalid(sp_rvalid[c]),
            .rready(sp_rready[c]));
    end
    endgenerate

    // Gate BOTH directions: masking only READY leaves the RAM seeing VALID and
    // completing behind the fabric's back, so nothing is actually stalled.
    reg  [NS-1:0] stall = {NS{1'b0}};
    wire [NS-1:0] ram_awready, ram_wready, ram_arready;
    wire [NS-1:0] ram_awvalid = sp_awvalid & ~stall;
    wire [NS-1:0] ram_wvalid  = sp_wvalid  & ~stall;
    wire [NS-1:0] ram_arvalid = sp_arvalid & ~stall;
    assign sp_awready = ram_awready & ~stall;
    assign sp_wready  = ram_wready  & ~stall;
    assign sp_arready = ram_arready & ~stall;

    // A free-running LFSR so the stall pattern is not aligned to any clock.
    always @(posedge clk_xdma) begin
        xcyc <= xcyc + 32'd1;
    end

    reg [15:0] lfsr = 16'hACE1;
    reg        stall_en = 1'b0;
    always @(posedge bus_clk) begin
        lfsr <= {lfsr[14:0], lfsr[15] ^ lfsr[13] ^ lfsr[12] ^ lfsr[10]};
        stall <= stall_en ? {NS{1'b0}} | lfsr[NS-1:0] : {NS{1'b0}};
    end

    genvar i;
    generate
    for (i = 0; i < NS; i = i + 1) begin : g_ram
        localparam integer DW = (i % NQ == 0) ? P_WIDE : 32;
        if (DW < MAXW) begin : g_pad
            assign sp_rdata[i*MAXW + DW +: MAXW-DW] = {(MAXW-DW){1'b0}};
        end
        axi4_ram #(.DATA_WIDTH(DW), .ADDR_WIDTH(AW), .ID_WIDTH(MAXID),
                   .DEPTH(512)) u_ram (
            .clk(sclk[i]), .resetn(rstn),
            .s_axi_awid(sp_awid[i*MAXID +: MAXID]),
            .s_axi_awaddr(sp_awaddr[i*AW +: AW]),
            .s_axi_awlen(sp_awlen[i*8 +: 8]),
            .s_axi_awsize(sp_awsize[i*3 +: 3]),
            .s_axi_awburst(sp_awburst[i*2 +: 2]),
            .s_axi_awvalid(ram_awvalid[i]), .s_axi_awready(ram_awready[i]),
            .s_axi_wdata(sp_wdata[i*MAXW +: DW]),
            .s_axi_wstrb(sp_wstrb[i*(MAXW/8) +: DW/8]),
            .s_axi_wlast(sp_wlast[i]), .s_axi_wvalid(ram_wvalid[i]),
            .s_axi_wready(ram_wready[i]),
            .s_axi_bid(sp_bid[i*MAXID +: MAXID]),
            .s_axi_bresp(sp_bresp[i*2 +: 2]),
            .s_axi_bvalid(sp_bvalid[i]), .s_axi_bready(sp_bready[i]),
            .s_axi_arid(sp_arid[i*MAXID +: MAXID]),
            .s_axi_araddr(sp_araddr[i*AW +: AW]),
            .s_axi_arlen(sp_arlen[i*8 +: 8]),
            .s_axi_arsize(sp_arsize[i*3 +: 3]),
            .s_axi_arburst(sp_arburst[i*2 +: 2]),
            .s_axi_arvalid(ram_arvalid[i]), .s_axi_arready(ram_arready[i]),
            .s_axi_rid(sp_rid[i*MAXID +: MAXID]),
            .s_axi_rdata(sp_rdata[i*MAXW +: DW]),
            .s_axi_rresp(sp_rresp[i*2 +: 2]), .s_axi_rlast(sp_rlast[i]),
            .s_axi_rvalid(sp_rvalid[i]), .s_axi_rready(sp_rready[i])
        );
    end
    endgenerate

    function [31:0] pat32;
        input [AW-1:0] a;
        begin pat32 = {a[15:0], ~a[15:0]} ^ 32'h5A5A_F0F0; end
    endfunction

    function [MAXW-1:0] pat512;
        input [AW-1:0] a;
        input integer  b;
        integer k;
        begin
            for (k = 0; k < 16; k = k + 1) begin
                pat512[k*32 +: 32] = pat32(a) ^ (b * 32'h0001_0001)
                                              ^ (k * 32'h1000_0100);
            end
        end
    endfunction

    task chk;
        input [MAXW-1:0] got, exp;
        input [AW-1:0]   a;
        begin
            checks = checks + 1;
            if (got !== exp) begin
                errors = errors + 1;
                $display("%0t FAIL @%h got %h exp %h", $time, a, got, exp);
            end
        end
    endtask

    // A 32-bit value as the 64-bit jtag master places it: at its address lane.
    task j_write;
        input [AW-1:0] a;
        begin
            @(negedge jclk); #TS;
            awid[0] = 4'd1; awaddr[0] = a; awlen[0] = 8'd0;
            awsize[0] = 3'd2; awvld[0] = 1'b1;
            wdata[0] = a[2] ? {416'd0, pat32(a), 32'd0} : {448'd0, pat32(a)};
            wstrb[0] = {56'd0, a[2] ? 8'hF0 : 8'h0F};
            wlast[0] = 1'b1; wvld[0] = 1'b1;
            brdy[0]  = 1'b1;
            #TS;
            while (!j_awrdy) begin @(negedge jclk); #TS; end
            @(negedge jclk); #TS; awvld[0] = 1'b0;
            while (!j_wrdy) begin @(negedge jclk); #TS; end
            @(negedge jclk); #TS; wvld[0] = 1'b0;
            while (!j_bvld) begin @(negedge jclk); #TS; end
            if (j_bresp !== exp_b) begin
                errors = errors + 1;
                $display("%0t FAIL jtag write @%h bresp %b exp %b", $time, a,
                         j_bresp, exp_b);
            end
            @(negedge jclk); #TS; brdy[0] = 1'b1;
        end
    endtask

    task j_read;
        input [AW-1:0] a;
        input [1:0]    exp_resp;
        begin
            @(negedge jclk); #TS;
            arid[0] = 4'd2; araddr[0] = a; arlen[0] = 8'd0;
            arsize[0] = 3'd2; arvld[0] = 1'b1; rrdy[0] = 1'b1;
            #TS;
            while (!j_arrdy) begin @(negedge jclk); #TS; end
            @(negedge jclk); #TS; arvld[0] = 1'b0;
            while (!j_rvld) begin @(negedge jclk); #TS; end
            if (j_rresp !== exp_resp) begin
                errors = errors + 1;
                $display("%0t FAIL jtag read @%h rresp %b exp %b", $time, a,
                         j_rresp, exp_resp);
            end else if (exp_resp == 2'b00) begin
                chk({480'd0, a[2] ? j_rdata[63:32] : j_rdata[31:0]},
                    {480'd0, pat32(a)}, a);
            end
            @(negedge jclk); #TS; rrdy[0] = 1'b1;
        end
    endtask

    function [63:0] pat64;
        input [AW-1:0] a;
        begin pat64 = {pat32(a + 40'd4), pat32(a)}; end
    endfunction

    task jb_write;
        input [AW-1:0] a;
        input [7:0]    len;
        integer b;
        begin
            @(negedge jclk); #TS;
            awid[0] = 4'd5; awaddr[0] = a; awlen[0] = len;
            awsize[0] = 3'd3; awvld[0] = 1'b1; brdy[0] = 1'b1;
            #TS;
            while (!j_awrdy) begin @(negedge jclk); #TS; end
            @(negedge jclk); #TS; awvld[0] = 1'b0;
            for (b = 0; b <= len; b = b + 1) begin
                wdata[0] = {448'd0, pat64(a + b*8)};
                wstrb[0] = {56'd0, 8'hFF};
                wlast[0] = (b == len); wvld[0] = 1'b1;
                #TS;
                while (!j_wrdy) begin @(negedge jclk); #TS; end
                @(negedge jclk); #TS; wvld[0] = 1'b0;
            end
            while (!j_bvld) begin @(negedge jclk); #TS; end
            if (j_bresp !== 2'b00) begin
                errors = errors + 1;
                $display("%0t FAIL jtag burst write @%h bresp %b", $time, a,
                         j_bresp);
            end
            @(negedge jclk); #TS;
        end
    endtask

    task jb_read;
        input [AW-1:0] a;
        input [7:0]    len;
        integer b, spin;
        begin
            @(negedge jclk); #TS;
            arid[0] = 4'd6; araddr[0] = a; arlen[0] = len;
            arsize[0] = 3'd3; arvld[0] = 1'b1; rrdy[0] = 1'b1;
            #TS;
            while (!j_arrdy) begin @(negedge jclk); #TS; end
            @(negedge jclk); #TS; arvld[0] = 1'b0;
            b = 0; spin = 0;
            while (b <= len && spin < 20000) begin
                if (j_rvld) begin
                    chk({448'd0, j_rdata}, {448'd0, pat64(a + b*8)}, a + b*8);
                    if ((b == len) !== j_rlast) begin
                        errors = errors + 1;
                        $display("%0t FAIL jtag burst rlast beat %0d of %0d",
                                 $time, b, len);
                    end
                    b = b + 1;
                end
                spin = spin + 1;
                @(negedge jclk); #TS;
            end
            if (b <= len) begin
                errors = errors + 1;
                $display("%0t FAIL jtag burst read @%h stalled at beat %0d of %0d",
                         $time, a, b, len);
            end
        end
    endtask

    task x_write;
        input [AW-1:0] a;
        input [7:0]    len;
        integer        b;
        begin
            @(negedge clk_xdma); #TS;
            awid[1] = 4'd3; awaddr[1] = a; awlen[1] = len;
            // The manager is 512-bit whatever the flit is; at FW=256 that is
            // the 2:1 split path, which is the point of testing FW=256.
            awsize[1] = 3'd6;
            awvld[1] = 1'b1; brdy[1] = 1'b1;
            #TS;
            while (!awrdy[1]) begin @(negedge clk_xdma); #TS; end
            @(negedge clk_xdma); #TS; awvld[1] = 1'b0;
            for (b = 0; b <= len; b = b + 1) begin
                wdata[1] = pat512(a, b);
                wstrb[1] = {64{1'b1}};
                wlast[1] = (b == len);
                wvld[1]  = 1'b1;
                #TS;
                while (!wrdy[1]) begin @(negedge clk_xdma); #TS; end
                @(negedge clk_xdma); #TS;
            end
            wvld[1] = 1'b0; wlast[1] = 1'b0;
            while (!bvld[1]) begin @(negedge clk_xdma); #TS; end
            if (bresp_v[3:2] !== 2'b00) begin
                errors = errors + 1;
                $display("%0t FAIL xdma write @%h bresp %b", $time, a,
                         bresp_v[3:2]);
            end
            @(negedge clk_xdma); #TS; brdy[1] = 1'b1;
        end
    endtask

    task x_read;
        input [AW-1:0] a;
        input [7:0]    len;
        integer        b;
        begin
            @(negedge clk_xdma); #TS;
            arid[1] = 4'd4; araddr[1] = a; arlen[1] = len;
            arsize[1] = 3'd6;
            arvld[1] = 1'b1; rrdy[1] = 1'b1;
            #TS;
            while (!arrdy[1]) begin @(negedge clk_xdma); #TS; end
            @(negedge clk_xdma); #TS; arvld[1] = 1'b0;
            for (b = 0; b <= len; b = b + 1) begin
                while (!rvld[1]) begin @(negedge clk_xdma); #TS; end
                chk(rdata_v[MAXW +: MAXW], pat512(a, b), a);
                if ((b == len) !== rlast[1]) begin
                    errors = errors + 1;
                    $display("%0t FAIL xdma rlast beat %0d of %0d", $time, b,
                             len);
                end
                @(negedge clk_xdma); #TS;
            end
            rrdy[1] = 1'b1;
        end
    endtask

    task l_write;
        input [AW-1:0] a;
        begin
            @(negedge clk_xdma); #TS;
            awid[2] = 4'd0; awaddr[2] = a; awlen[2] = 8'd0;
            awsize[2] = 3'd2; awvld[2] = 1'b1;
            wdata[2] = {480'd0, pat32(a)};
            wstrb[2] = {60'd0, 4'hF}; wlast[2] = 1'b1; wvld[2] = 1'b1;
            brdy[2]  = 1'b1;
            #TS;
            while (!awrdy[2]) begin @(negedge clk_xdma); #TS; end
            @(negedge clk_xdma); #TS; awvld[2] = 1'b0;
            while (!wrdy[2]) begin @(negedge clk_xdma); #TS; end
            @(negedge clk_xdma); #TS; wvld[2] = 1'b0;
            while (!bvld[2]) begin @(negedge clk_xdma); #TS; end
            @(negedge clk_xdma); #TS; brdy[2] = 1'b1;
        end
    endtask

    task l_read;
        input [AW-1:0] a;
        begin
            @(negedge clk_xdma); #TS;
            arid[2] = 4'd0; araddr[2] = a; arlen[2] = 8'd0;
            arsize[2] = 3'd2; arvld[2] = 1'b1; rrdy[2] = 1'b1;
            #TS;
            while (!arrdy[2]) begin @(negedge clk_xdma); #TS; end
            @(negedge clk_xdma); #TS; arvld[2] = 1'b0;
            while (!rvld[2]) begin @(negedge clk_xdma); #TS; end
            chk({480'd0, rdata_v[2*MAXW +: 32]}, {480'd0, pat32(a)}, a);
            @(negedge clk_xdma); #TS; rrdy[2] = 1'b1;
        end
    endtask

`ifdef SB_PROBE
    // One shot at a fixed time: why is the first write not accepted?
    initial begin
        #8000;
        $display("PROBE awvld=%b awrdy=%b addr=%h", mp_awvalid, awrdy,
                 mp_awaddr[0 +: AW]);
        $display("PROBE nmu0 aw_hit=%b aw_dst=%h aw_dpt=%h tag_avail=%b cred=%d reqf_full=%b tokf_full=%b wst=%d",
                 u_dut.g_stn[1].g_mgr.g_nmu[0].u_nmu.aw_hit,
                 u_dut.g_stn[1].g_mgr.g_nmu[0].u_nmu.aw_dst,
                 u_dut.g_stn[1].g_mgr.g_nmu[0].u_nmu.aw_dpt,
                 u_dut.g_stn[1].g_mgr.g_nmu[0].u_nmu.tag_avail,
                 u_dut.g_stn[1].g_mgr.g_nmu[0].u_nmu.rsp_credit,
                 u_dut.g_stn[1].g_mgr.g_nmu[0].u_nmu.reqf_full,
                 u_dut.g_stn[1].g_mgr.g_nmu[0].u_nmu.tokf_full,
                 u_dut.g_stn[1].g_mgr.g_nmu[0].u_nmu.wst);
        $display("PROBE stn1 nm_req_valid=%b nm_req_ready=%b ij_valid=%b ij_ready=%b",
                 u_dut.g_stn[1].u_stn.nm_req_valid,
                 u_dut.g_stn[1].u_stn.nm_req_ready,
                 u_dut.g_stn[1].u_stn.ij_valid,
                 u_dut.g_stn[1].u_stn.ij_ready);
        $display("PROBE stn1 ns_req_valid=%b ns_req_ready=%b src=%h",
                 u_dut.g_stn[1].u_stn.ns_req_valid,
                 u_dut.g_stn[1].u_stn.ns_req_ready,
                 u_dut.g_stn[1].u_stn.ns_req_src);
        $display("PROBE stn1 sub sp_awvalid=%b sp_awready=%b sp_bvalid=%b sp_bready=%b",
                 sp_awvalid[3:2], sp_awready[3:2],
                 sp_bvalid[3:2], sp_bready[3:2]);
        $display("PROBE stn1 ns_rsp_valid=%b ns_rsp_ready=%b dst=%h",
                 u_dut.g_stn[1].u_stn.ns_rsp_valid,
                 u_dut.g_stn[1].u_stn.ns_rsp_ready,
                 u_dut.g_stn[1].u_stn.ns_rsp_dst);
        $display("PROBE stn1 cl_valid=%b cl_ready=%b nm_rsp_valid=%b nm_rsp_ready=%b",
                 u_dut.g_stn[1].u_stn.cl_valid,
                 u_dut.g_stn[1].u_stn.cl_ready,
                 u_dut.g_stn[1].u_stn.nm_rsp_valid,
                 u_dut.g_stn[1].u_stn.nm_rsp_ready);
    end
`endif

    integer s, e, n;
    integer t0, seed;
    integer hop [0:3];
    integer dj_i, dx_i, dl_i;
    integer ph14_err;
    reg [AW-1:0] ra;

    initial begin
        seed = 32'h5EED_0005;
        for (n = 0; n < NM; n = n + 1) begin
            awvld[n] = 0; wvld[n] = 0; arvld[n] = 0;
            brdy[n] = 1; rrdy[n] = 1; wlast[n] = 0;
            awlen[n] = 0; arlen[n] = 0; awsize[n] = 0; arsize[n] = 0;
            awaddr[n] = 0; araddr[n] = 0; wdata[n] = 0; wstrb[n] = 0;
            awid[n] = 0; arid[n] = 0;
        end

        repeat (20) @(posedge bus_clk);
        rstn = 1;
        repeat (20) @(posedge bus_clk);

        $display("--- phase 1: every endpoint on every station");
        for (s = 0; s < 4; s = s + 1) begin
            for (e = 0; e < NQ; e = e + 1) begin
`ifdef SB_PROBE
                $display("    try stn %0d ep %0d @%h", s, e, adr(s, e) + 40'h40);
`endif
                j_write(adr(s, e) + 40'h40);
                j_read (adr(s, e) + 40'h40, 2'b00);
            end
        end

        $display("--- phase 2: wide bursts to each station");
        for (s = 0; s < 4; s = s + 1) begin
            x_write(adr(s, 0) + 40'h800, 8'd7);
            x_read (adr(s, 0) + 40'h800, 8'd7);
        end

        $display("--- phase 3: the lite manager, narrow ports only");
        for (s = 0; s < 4; s = s + 1) begin
            l_write(adr(s, 1) + 40'h100);
            l_read (adr(s, 1) + 40'h100);
        end

        $display("--- phase 4: decode error terminates at the NMU");
        n = stat_decerr;
        exp_b = 2'b11;
        j_write(adr(7, 0));
        exp_b = 2'b00;
        j_read(adr(7, 0), 2'b11);
        checks = checks + 1;
        if (stat_decerr != n + 2) begin
            errors = errors + 1;
            $display("%0t FAIL decerr %0d expected %0d", $time, stat_decerr,
                     n + 2);
        end

        $display("--- phase 5: three managers, three stations, at once");
        fork
            begin j_write(adr(0, 1) + 40'h200); j_read(adr(0, 1) + 40'h200, 2'b00); end
            begin x_write(adr(2, 0) + 40'h900, 8'd7); x_read(adr(2, 0) + 40'h900, 8'd7); end
            begin l_write(adr(3, 1) + 40'h300); l_read(adr(3, 1) + 40'h300); end
        join

        // The line's distinguishing number: managers are on station 1, so
        // station 3 is TWO hops and must be forwarded through station 2.
        $display("--- phase 6: latency by hop count");
        for (s = 0; s < 4; s = s + 1) begin
            j_write(adr(s, 1) + 40'h500);
            t0 = $time;
            j_read(adr(s, 1) + 40'h500, 2'b00);
            hop[s] = ($time - t0) / 10;
        end
        $display("    HOPS stn0 %0d, stn1 %0d, stn2 %0d, stn3 %0d ctrl cycles",
                 hop[0], hop[1], hop[2], hop[3]);

        $display("--- phase 7: randomised traffic across the line");
        for (n = 0; n < 24; n = n + 1) begin
            s  = {$random(seed)} % 4;
            e  = {$random(seed)} % NQ;
            ra = adr(s, e) + (({$random(seed)} % 32) * 4) + 40'h2000;
            if (e == 0) begin
                x_write(ra & ~40'h3F, 8'd3);
                x_read (ra & ~40'h3F, 8'd3);
            end else begin
                j_write(ra); j_read(ra, 2'b00);
            end
        end

        // The 4 KB rule caps a 512-bit port at 64 beats. Run the ceiling to
        // every station: a max packet is what head-of-line blocking costs.
        $display("--- phase 8: maximum legal burst to every station");
        for (s = 0; s < 4; s = s + 1) begin
            x_write(adr(s, 0) + 40'h4000, 8'd63);
            x_read (adr(s, 0) + 40'h4000, 8'd63);
        end

        // Every manager onto ONE station at once, which is where a shared
        // buffer or a lost grant shows up as a hang rather than a wrong value.
        $display("--- phase 9: deadlock stress, all managers on one station");
        for (s = 0; s < 4; s = s + 1) begin
            fork
                begin : ds_j
                    for (dj_i = 0; dj_i < 4; dj_i = dj_i + 1) begin
                        j_write(adr(s, 1) + 40'h900 + dj_i*4);
                        j_read (adr(s, 1) + 40'h900 + dj_i*4, 2'b00);
                    end
                end
                begin : ds_x
                    for (dx_i = 0; dx_i < 4; dx_i = dx_i + 1) begin
                        x_write(adr(s, 0) + 40'h6000 + dx_i*512, 8'd7);
                        x_read (adr(s, 0) + 40'h6000 + dx_i*512, 8'd7);
                    end
                end
                begin : ds_l
                    for (dl_i = 0; dl_i < 4; dl_i = dl_i + 1) begin
                        l_write(adr(s, NQ-1) + 40'hA00 + dl_i*4);
                        l_read (adr(s, NQ-1) + 40'hA00 + dl_i*4);
                    end
                end
            join
        end

        // Same ID to stations with very different hop counts: the near one must
        // not overtake the far one on a shared id.
        $display("--- phase 10: same-ID ordering across unequal latencies");
        for (n = 0; n < 6; n = n + 1) begin
            j_write(adr(3, 1) + 40'hB00 + n*4);
            j_write(adr(1, 1) + 40'hB00 + n*4);
            j_read (adr(3, 1) + 40'hB00 + n*4, 2'b00);
            j_read (adr(1, 1) + 40'hB00 + n*4, 2'b00);
        end

        // F4: two same-ARID reads, FAR (stn 3) then NEAR (stn 1); the near must
        // not overtake. ID_ORDER=1 holds the near AR till far retires; 0 = the bug.
        $display("--- phase 10b: F4 same-ID reorder, far-then-near shared ARID");
        x_write(adr(3, 0) + 40'hE00, 8'd0);
        x_write(adr(1, 0) + 40'h2E00, 8'd0);
        @(negedge clk_xdma); #TS;
        arid[1] = 4'd4; araddr[1] = adr(3, 0) + 40'hE00; arlen[1] = 8'd0;
        arsize[1] = 3'd6; arvld[1] = 1'b1; rrdy[1] = 1'b1;
        #TS;
        f4sp = 0;
        while (!arrdy[1] && f4sp < 8000) begin @(negedge clk_xdma); #TS; f4sp=f4sp+1; end
        @(negedge clk_xdma); #TS; arvld[1] = 1'b0;
        fork
            begin : f4_issue_near
                @(negedge clk_xdma); #TS;
                araddr[1] = adr(1, 0) + 40'h2E00; arvld[1] = 1'b1;  // same ARID
                f4sp = 0;
                while (!arrdy[1] && f4sp < 8000) begin
                    @(negedge clk_xdma); #TS; f4sp = f4sp + 1;
                end
                $display("    F4 AR2(near) accepted %0d cyc after issue", f4sp);
                @(negedge clk_xdma); #TS; arvld[1] = 1'b0;
            end
            begin : f4_collect
                while (!rvld[1]) begin @(negedge clk_xdma); #TS; end
                $display("    F4 R#1 far=%b near=%b",
                         rdata_v[MAXW +: MAXW] === pat512(adr(3,0)+40'hE00, 0),
                         rdata_v[MAXW +: MAXW] === pat512(adr(1,0)+40'h2E00, 0));
                if (rdata_v[MAXW +: MAXW] !== pat512(adr(3, 0) + 40'hE00, 0)) begin
                    errors = errors + 1;
                    $display("    FAIL F4: R#1 is not the FAR read -- near overtook");
                end
                @(negedge clk_xdma); #TS;
                while (!rvld[1]) begin @(negedge clk_xdma); #TS; end
                if (rdata_v[MAXW +: MAXW] !== pat512(adr(1, 0) + 40'h2E00, 0)) begin
                    errors = errors + 1;
                    $display("    FAIL F4: R#2 is not the NEAR read");
                end
                @(negedge clk_xdma); #TS;
            end
        join

        // Random subordinate backpressure, which is where a shared buffer or a
        // grant held across an underrun stops being a throughput question.
        $display("--- phase 11: randomised traffic under subordinate stalls");
        stall_en = 1'b1;
        for (n = 0; n < 16; n = n + 1) begin
            s  = {$random(seed)} % 4;
            e  = {$random(seed)} % NQ;
            ra = adr(s, e) + (({$random(seed)} % 16) * 4) + 40'hC00;
            if (e == 0) begin
                x_write(ra & ~40'h3F, 8'd3);
                x_read (ra & ~40'h3F, 8'd3);
            end else begin
                j_write(ra); j_read(ra, 2'b00);
            end
        end
        stall_en = 1'b0;
        repeat (100) @(posedge bus_clk);

        $display("--- phase 12: reset mid-traffic, then recover");
        rstn = 0;
        repeat (20) @(posedge bus_clk);
        rstn = 1;
        repeat (40) @(posedge bus_clk);
        for (n = 0; n < NM; n = n + 1) begin
            awvld[n] = 0; wvld[n] = 0; arvld[n] = 0; wlast[n] = 0;
        end
        repeat (20) @(posedge bus_clk);
        j_write(adr(0, 1) + 40'h600); j_read(adr(0, 1) + 40'h600, 2'b00);
        x_write(adr(3, 0) + 40'h5000, 8'd3);
        x_read (adr(3, 0) + 40'h5000, 8'd3);

        // Sustained rate, not a single burst: the count spans every AW
        // handshake and B wait, so it is what a manager actually sees.
        $display("--- phase 13: sustained write bandwidth by hop count");
        for (s = 0; s < 4; s = s + 1) begin
            bw_t0 = xcyc;
            for (bw_i = 0; bw_i < 8; bw_i = bw_i + 1) begin
                x_write(adr(s, 0) + 40'h4000, 8'd63);
            end
            bw_t1 = xcyc;
            $display("    BW stn%0d %0d cycles for %0d bytes", s,
                     bw_t1 - bw_t0, 8 * 64 * (P_MW / 8));
        end

        $display("--- phase 14: 64-bit jtag bursts through the built-in conversion");
        for (s = 0; s < 4; s = s + 1) begin
            jb_write(adr(s, 0) | 40'h3000, 8'd15);
            jb_read (adr(s, 0) | 40'h3000, 8'd15);
        end

        // Hard gate since the NSU splits sub-flit writes itself: a 64-bit op
        // to a 32-bit endpoint must land both halves.
        ph14_err = errors;
        jb_write(adr(0, 1) | 40'h80, 8'd0);
        j_read (adr(0, 1) | 40'h80, 2'b00);
        j_read ((adr(0, 1) | 40'h80) + 40'd4, 2'b00);
        checks = checks + 1;
        if (errors != ph14_err) begin
            $display("    CTRL-PATH: 64-bit op to a 32-bit endpoint LOST %0d check(s)",
                     errors - ph14_err);
        end
        else begin
            $display("    CTRL-PATH: 64-bit op to a 32-bit endpoint intact");
        end

        repeat (200) @(posedge bus_clk);

        for (n = 0; n < NM; n = n + 1) begin
            checks = checks + 1;
            errors = errors + mchk_err[n*32 +: 32];
        end
        for (n = 0; n < NS; n = n + 1) begin
            checks = checks + 1;
            errors = errors + schk_err[n*32 +: 32];
        end
        $display("    monitors saw %0d/%0d/%0d manager transactions",
                 mchk_txn[0 +: 32], mchk_txn[32 +: 32], mchk_txn[64 +: 32]);

        if (errors) begin
            $display("FAIL  %0d errors in %0d checks", errors, checks);
        end
        else begin
            $display("PASS  %0d checks", checks);
        end
        $finish;
    end

    // A full clean run ends near 100 ms; a stalled handshake otherwise runs
    // the simulator forever with no message at all.
    initial begin
        #200_000_000;
        $display("FAIL  watchdog: the bench did not finish in 200 ms");
        $finish;
    end

    // Wall-clock beacon: a livelock freezes $time between beats, an event
    // storm crawls it; either way the last line says WHERE.
    always begin
        #1_000_000 $display("  HB t=%0t", $time);
    end
endmodule

`default_nettype wire

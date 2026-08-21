// v6.7 JTAG-dead repro: the station bus in the EXACT shipped v6.7 shape, with a
// 64-bit AXI4 manager on S00 driving the silicon-observed access shapes.
//
// What this bench carries that sb_line4_tb does NOT:
//   1. AW=43 and the real SEG_* table from scripts/tcl/v6/50_addr_lit.tcl
//      (SEG_OVERRIDE=1), instead of the uniform 64 KB map at AW=40.
//   2. The AXI4-Lite truncation sb_bd_line4 applies to ports 2 and 3 of every
//      station: awlen/awsize/wlast/arlen/arsize are DROPPED at the port, rlast
//      is tied 1'b1, bid/rid are tied 0. sb_line4_tb wires all sixteen ports to
//      a full AXI4 axi4_ram that honours arlen, so this is untested there.
//   3. Only manager 0 (jtag, 64-bit) is driven, which is the silicon case.
//
// -d LITE_AS_FULL puts a full AXI4 axi4_ram back on ports 2/3 to A/B item 2.

`timescale 1ns / 1ps
`default_nettype none

module sb_v67_jtag_tb;

    // ------------------------------------------------------ v6.7 00_config.tcl
    localparam integer AW    = 43;
    localparam integer FW    = 256;
    localparam integer NQ    = 4;
    localparam integer PORTW = 2;
    localparam integer NS    = 4 * NQ;
    localparam integer NM    = 3;
    localparam integer MAXW  = 512;
    localparam integer MAXID = 4;
    localparam integer P_OST = 4;
    localparam integer P_SF  = 1;
    localparam integer P_LPB = 0;
    localparam integer P_TO  = 0;
    localparam integer P_CRED = 16;
    localparam integer P_PIPE = 4;
    localparam integer P_CDC  = 1;
    localparam integer P_FULL = 0;      // LINK_FULL

    localparam integer JW = 64;         // jtag_axi M_AXI_DATA_WIDTH

    // Bounded run. Any single access that does not finish inside TMO ctrl
    // cycles is a hang, and the state at that moment is what names the defect.
    localparam integer TMO = 4000;

    integer errors = 0;
    integer checks = 0;
    integer hangs  = 0;

    // ------------------------------------------------- the map, 50_addr_lit.tcl
    // seg k = mid*NQ + port. MASK 1 = compare; emitted = (a & ~MASK) | XLT.
    localparam [AW-1:0] FULL = {AW{1'b1}};

    function [NS*AW-1:0] mk_seg_base; input integer u; integer m; begin
        mk_seg_base = {(NS*AW){1'b0}};
        for (m = 0; m < 4; m = m + 1) begin
            mk_seg_base[(m*4+0)*AW +: AW] = (m + 1) * (43'd1 << 40);
            mk_seg_base[(m*4+1)*AW +: AW] = 43'h800000 + m * 43'h10000;
            mk_seg_base[(m*4+2)*AW +: AW] = m * 43'h100000;
            mk_seg_base[(m*4+3)*AW +: AW] = 43'h900000 + m * 43'h10000;
        end
    end endfunction

    function [NS*AW-1:0] mk_seg_mask; input integer u; integer m; begin
        mk_seg_mask = {(NS*AW){1'b0}};
        for (m = 0; m < 4; m = m + 1) begin
            mk_seg_mask[(m*4+0)*AW +: AW] = 43'd7 << 40;
            mk_seg_mask[(m*4+1)*AW +: AW] = FULL ^ 43'hFFFF;
            mk_seg_mask[(m*4+2)*AW +: AW] = FULL ^ 43'hFFFFF;
            mk_seg_mask[(m*4+3)*AW +: AW] = FULL ^ 43'hFFFF;
        end
    end endfunction

    function [NS*AW-1:0] mk_seg_xlt; input integer u; integer m; begin
        mk_seg_xlt = {(NS*AW){1'b0}};
        for (m = 0; m < 4; m = m + 1) begin
            mk_seg_xlt[(m*4+0)*AW +: AW] = {AW{1'b0}};
            mk_seg_xlt[(m*4+1)*AW +: AW] = 43'h800000 + m * 43'h10000;
            mk_seg_xlt[(m*4+2)*AW +: AW] = m * 43'h100000;
            mk_seg_xlt[(m*4+3)*AW +: AW] = 43'h900000 + m * 43'h10000;
        end
    end endfunction

    function [NS*2-1:0] mk_seg_dst; input integer u; integer m, p; begin
        mk_seg_dst = {(NS*2){1'b0}};
        for (m = 0; m < 4; m = m + 1)
            for (p = 0; p < 4; p = p + 1)
                mk_seg_dst[(m*4+p)*2 +: 2] = m[1:0];
    end endfunction

    function [NS*2-1:0] mk_seg_dprt; input integer u; integer m, p; begin
        mk_seg_dprt = {(NS*2){1'b0}};
        for (m = 0; m < 4; m = m + 1)
            for (p = 0; p < 4; p = p + 1)
                mk_seg_dprt[(m*4+p)*2 +: 2] = p[1:0];
    end endfunction

    localparam [NS*AW-1:0] SEG_BASE = mk_seg_base(0);
    localparam [NS*AW-1:0] SEG_MASK = mk_seg_mask(0);
    localparam [NS*AW-1:0] SEG_XLT  = mk_seg_xlt(0);
    localparam [NS*2-1:0]  SEG_DST  = mk_seg_dst(0);
    localparam [NS*2-1:0]  SEG_DPRT = mk_seg_dprt(0);

    // --------------------------------------------------------------- the clocks
    // BUS_MHZ 200, CTRL 100, XDMA 250, mesh clk_out4 300, DDR ui_clk 300.
    // LINK_CDC=1: four independent MMCMs, so four unrelated 200 MHz clocks.
    reg bclk0 = 0, bclk1 = 0, bclk2 = 0, bclk3 = 0;
    always #2.500 bclk0 = ~bclk0;           // 200.0 MHz
    always #2.497 bclk1 = ~bclk1;
    always #2.503 bclk2 = ~bclk2;
    always #2.499 bclk3 = ~bclk3;

    reg clk_ctrl = 0, clk_xdma = 0;
    always #5.000 clk_ctrl = ~clk_ctrl;     // 100.0 MHz
    always #2.000 clk_xdma = ~clk_xdma;     // 250.0 MHz

    reg clk_s0 = 0, clk_s1 = 0, clk_s2 = 0, clk_s3 = 0;
    always #1.667 clk_s0 = ~clk_s0;         // MAG clk_out4, 300 MHz
    always #1.667 clk_s1 = ~clk_s1;
    always #1.667 clk_s2 = ~clk_s2;
    always #1.667 clk_s3 = ~clk_s3;

    reg clk_ddr0 = 0, clk_ddr1 = 0, clk_ddr2 = 0, clk_ddr3 = 0;
    always #1.667 clk_ddr0 = ~clk_ddr0;     // MIG ui_clk, 300 MHz
    always #1.671 clk_ddr1 = ~clk_ddr1;
    always #1.663 clk_ddr2 = ~clk_ddr2;
    always #1.669 clk_ddr3 = ~clk_ddr3;

    reg rstn = 0;
    wire bus_rst = !rstn;
    wire [3:0] bclk = {bclk3, bclk2, bclk1, bclk0};
    wire [3:0] brst = {4{bus_rst}};
    wire [3:0] tb_mclk = {clk_s3, clk_s2, clk_s1, clk_s0};
    wire [3:0] tb_dclk = {clk_ddr3, clk_ddr2, clk_ddr1, clk_ddr0};

    // ------------------------------------------------------------- the managers
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

    wire [NM*MAXID-1:0]  mp_awid    = {awid[2], awid[1], awid[0]};
    wire [NM*AW-1:0]     mp_awaddr  = {awaddr[2], awaddr[1], awaddr[0]};
    wire [NM*8-1:0]      mp_awlen   = {awlen[2], awlen[1], awlen[0]};
    wire [NM*3-1:0]      mp_awsize  = {awsize[2], awsize[1], awsize[0]};
    wire [NM-1:0]        mp_awvalid = {awvld[2], awvld[1], awvld[0]};
    wire [NM*MAXW-1:0]   mp_wdata   = {wdata[2], wdata[1], wdata[0]};
    wire [NM*MAXW/8-1:0] mp_wstrb   = {wstrb[2], wstrb[1], wstrb[0]};
    wire [NM-1:0]        mp_wlast   = {wlast[2], wlast[1], wlast[0]};
    wire [NM-1:0]        mp_wvalid  = {wvld[2], wvld[1], wvld[0]};
    wire [NM-1:0]        mp_bready  = {brdy[2], brdy[1], brdy[0]};
    wire [NM*MAXID-1:0]  mp_arid    = {arid[2], arid[1], arid[0]};
    wire [NM*AW-1:0]     mp_araddr  = {araddr[2], araddr[1], araddr[0]};
    wire [NM*8-1:0]      mp_arlen   = {arlen[2], arlen[1], arlen[0]};
    wire [NM*3-1:0]      mp_arsize  = {arsize[2], arsize[1], arsize[0]};
    wire [NM-1:0]        mp_arvalid = {arvld[2], arvld[1], arvld[0]};
    wire [NM-1:0]        mp_rready  = {rrdy[2], rrdy[1], rrdy[0]};

    // ----------------------------------------------------------- the subordinates
    wire [NS*MAXID-1:0]  sp_awid, sp_arid;
    wire [NS*MAXID-1:0]  sp_bid, sp_rid;
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

    sb_line4 #(.FW(FW), .AW(AW), .MAXW(MAXW), .MAXID(MAXID), .NM(NM),
               .NQ(NQ), .PORTW(PORTW), .TAGW(4), .OST(P_OST),
               .STORE_FWD(P_SF), .LUT_PER_BRAM(P_LPB), .TIMEOUT(P_TO),
               .PIPE(P_PIPE), .CRED(P_CRED), .STNW(2), .SRCW(2),
               .LINK_CDC(P_CDC), .MGR_STN(1), .LINK_FULL(P_FULL),
               .WIDE_DW(FW), .DSTW_P(2),
               .SEG_OVERRIDE(1),
               .SEG_BASE_P(SEG_BASE), .SEG_MASK_P(SEG_MASK),
               .SEG_XLT_P(SEG_XLT), .SEG_DST_P(SEG_DST),
               .SEG_DPORT_P(SEG_DPRT), .SEG_VLD_P({NS{1'b1}})) u_dut (
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

    // Mirrors sb_line4's port_dom(): ports 0,1 mesh clock, 2 ddr, 3 ctrl.
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

    // The endpoints as the v6.7 block design wires them:
    //   port 0  mesh S_AXI_MEM     256-bit AXI4
    //   port 1  dwc_ctrl S_AXI      32-bit AXI4  (32->64 into mesh CTRL)
    //   port 2  DDR4 S_AXI_CTRL     32-bit AXI4-LITE
    //   port 3  clk_wiz s_axi_lite  32-bit AXI4-LITE
    genvar i;
    generate
    for (i = 0; i < NS; i = i + 1) begin : g_ep
        localparam integer QP = i % NQ;
        localparam integer DW = (QP == 0) ? FW : 32;
        localparam integer LITE = (QP >= 2) ? 1 : 0;

        if (DW < MAXW) begin : g_pad
            assign sp_rdata[i*MAXW + DW +: MAXW-DW] = {(MAXW-DW){1'b0}};
        end

`ifdef LITE_AS_FULL
        localparam integer IS_LITE = 0;
`else
        localparam integer IS_LITE = LITE;
`endif

        if (IS_LITE) begin : g_lite
            // EXACTLY what sb_bd_line4 does at an AXI4-Lite port: awlen,
            // awsize, wlast, arlen, arsize never leave the wrapper; rlast is
            // tied high and bid/rid are tied 0.
            assign sp_bid[i*MAXID +: MAXID] = {MAXID{1'b0}};
            assign sp_rid[i*MAXID +: MAXID] = {MAXID{1'b0}};
            assign sp_rlast[i] = 1'b1;

            axi_lite_slave #(.DW(32), .AW(AW)) u_lite (
                .clk(sclk[i]), .resetn(rstn),
                .awaddr(sp_awaddr[i*AW +: AW]),
                .awvalid(sp_awvalid[i]), .awready(sp_awready[i]),
                .wdata(sp_wdata[i*MAXW +: 32]),
                .wstrb(sp_wstrb[i*(MAXW/8) +: 4]),
                .wvalid(sp_wvalid[i]), .wready(sp_wready[i]),
                .bresp(sp_bresp[i*2 +: 2]),
                .bvalid(sp_bvalid[i]), .bready(sp_bready[i]),
                .araddr(sp_araddr[i*AW +: AW]),
                .arvalid(sp_arvalid[i]), .arready(sp_arready[i]),
                .rdata(sp_rdata[i*MAXW +: 32]),
                .rresp(sp_rresp[i*2 +: 2]),
                .rvalid(sp_rvalid[i]), .rready(sp_rready[i])
            );
        end else begin : g_full
            axi4_ram #(.DATA_WIDTH(DW), .ADDR_WIDTH(AW), .ID_WIDTH(MAXID),
                       .DEPTH(512)) u_ram (
                .clk(sclk[i]), .resetn(rstn),
                .s_axi_awid(sp_awid[i*MAXID +: MAXID]),
                .s_axi_awaddr(sp_awaddr[i*AW +: AW]),
                .s_axi_awlen(sp_awlen[i*8 +: 8]),
                .s_axi_awsize(sp_awsize[i*3 +: 3]),
                .s_axi_awburst(sp_awburst[i*2 +: 2]),
                .s_axi_awvalid(sp_awvalid[i]), .s_axi_awready(sp_awready[i]),
                .s_axi_wdata(sp_wdata[i*MAXW +: DW]),
                .s_axi_wstrb(sp_wstrb[i*(MAXW/8) +: DW/8]),
                .s_axi_wlast(sp_wlast[i]), .s_axi_wvalid(sp_wvalid[i]),
                .s_axi_wready(sp_wready[i]),
                .s_axi_bid(sp_bid[i*MAXID +: MAXID]),
                .s_axi_bresp(sp_bresp[i*2 +: 2]),
                .s_axi_bvalid(sp_bvalid[i]), .s_axi_bready(sp_bready[i]),
                .s_axi_arid(sp_arid[i*MAXID +: MAXID]),
                .s_axi_araddr(sp_araddr[i*AW +: AW]),
                .s_axi_arlen(sp_arlen[i*8 +: 8]),
                .s_axi_arsize(sp_arsize[i*3 +: 3]),
                .s_axi_arburst(sp_arburst[i*2 +: 2]),
                .s_axi_arvalid(sp_arvalid[i]), .s_axi_arready(sp_arready[i]),
                .s_axi_rid(sp_rid[i*MAXID +: MAXID]),
                .s_axi_rdata(sp_rdata[i*MAXW +: DW]),
                .s_axi_rresp(sp_rresp[i*2 +: 2]), .s_axi_rlast(sp_rlast[i]),
                .s_axi_rvalid(sp_rvalid[i]), .s_axi_rready(sp_rready[i])
            );
        end
    end
    endgenerate

    // =========================================================== the state probe
    // A stall's LOCATION names the defect, so print the pipeline rather than
    // reading source and guessing which stage is holding.

`define NMU u_dut.g_stn[1].g_mgr.g_nmu[0].u_nmu

    task dump_nmu;
        begin
            $display("  NMU0 (jtag, MW=64 -> FW=256, PACK)");
            $display("    s_aclk side : wst=%0d tag_busy=%b tag_avail=%b credit=%0d er_busy=%b",
                     `NMU.wst, `NMU.tag_busy, `NMU.tag_avail,
                     `NMU.rsp_credit, `NMU.er_busy);
            $display("    decode      : ar_hit=%b ar_dst=%0d ar_dpt=%0d ar_xadr=%h ar_beats=%0d",
                     `NMU.ar_hit, `NMU.ar_dst, `NMU.ar_dpt,
                     `NMU.ar_xadr, `NMU.ar_beats);
            $display("                : aw_hit=%b aw_dst=%0d aw_dpt=%0d aw_xadr=%h",
                     `NMU.aw_hit, `NMU.aw_dst, `NMU.aw_dpt, `NMU.aw_xadr);
            $display("    gates       : aw_ok=%b ar_ok=%b aw_go=%b ar_go=%b ar_miss=%b",
                     `NMU.aw_ok, `NMU.ar_ok, `NMU.aw_go, `NMU.ar_go,
                     `NMU.ar_miss);
            $display("    REQ fifo    : empty=%b full=%b  TOK empty=%b full=%b sending=%b",
                     `NMU.reqf_empty, `NMU.reqf_full,
                     `NMU.tokf_empty, `NMU.tokf_full, `NMU.sending);
            $display("    packer      : pk_v=%b mid=%b eat=%b ends=%b",
                     `NMU.g_pack.pk_v, `NMU.g_pack.mid,
                     `NMU.g_pack.eat, `NMU.g_pack.ends);
            $display("    inject      : req_valid=%b req_ready=%b dst=%0d dport=%0d addr=%h len=%0d size=%0d wr=%b head=%b last=%b",
                     `NMU.req_valid, `NMU.req_ready, `NMU.req_dst,
                     `NMU.req_dport, `NMU.req_addr, `NMU.req_len,
                     `NMU.req_size, `NMU.req_wr, `NMU.req_head, `NMU.req_last);
            $display("    RSP fifo    : empty=%b full=%b rsp_valid=%b rsp_pop=%b rf_tag=%0d",
                     `NMU.rspf_empty, `NMU.rspf_full, `NMU.rsp_valid,
                     `NMU.rsp_pop, `NMU.rf_tag);
            $display("    rsp out     : b_nrm=%b r_raw=%b r_nrm=%b rd_fin=%b s_rvalid=%b s_bvalid=%b",
                     `NMU.b_nrm, `NMU.r_raw, `NMU.r_nrm, `NMU.rd_fin,
                     `NMU.s_rvalid, `NMU.s_bvalid);
            $display("    tag0        : rsv=%0d left=%0d lane=%0d sz=%0d",
                     `NMU.tg_rsv[0], `NMU.tg_left[0], `NMU.tg_lane[0],
                     `NMU.tg_sz[0]);
            $display("    tag1        : rsv=%0d left=%0d lane=%0d sz=%0d",
                     `NMU.tg_rsv[1], `NMU.tg_left[1], `NMU.tg_lane[1],
                     `NMU.tg_sz[1]);
        end
    endtask

    task dump_stn;
        input integer s;
        begin
            case (s)
            0: $display("  STN0 inj v/r=%b/%b  eject v/r=%b/%b  rspcol v/r=%b/%b  rf_req v/r=%b/%b  rt_rsp v/r=%b/%b",
                    u_dut.g_stn[0].u_stn.ij_valid, u_dut.g_stn[0].u_stn.ij_ready,
                    u_dut.g_stn[0].u_stn.ns_req_valid, u_dut.g_stn[0].u_stn.ns_req_ready,
                    u_dut.g_stn[0].u_stn.cl_valid, u_dut.g_stn[0].u_stn.cl_ready,
                    u_dut.g_stn[0].u_stn.rf_req_valid, u_dut.g_stn[0].u_stn.rf_req_ready,
                    u_dut.g_stn[0].u_stn.rt_rsp_valid, u_dut.g_stn[0].u_stn.rt_rsp_ready);
            1: $display("  STN1 inj v/r=%b/%b  eject v/r=%b/%b  rspcol v/r=%b/%b  lt_req v/r=%b/%b  lf_rsp v/r=%b/%b  nm_rsp v/r=%b/%b",
                    u_dut.g_stn[1].u_stn.ij_valid, u_dut.g_stn[1].u_stn.ij_ready,
                    u_dut.g_stn[1].u_stn.ns_req_valid, u_dut.g_stn[1].u_stn.ns_req_ready,
                    u_dut.g_stn[1].u_stn.cl_valid, u_dut.g_stn[1].u_stn.cl_ready,
                    u_dut.g_stn[1].u_stn.lt_req_valid, u_dut.g_stn[1].u_stn.lt_req_ready,
                    u_dut.g_stn[1].u_stn.lf_rsp_valid, u_dut.g_stn[1].u_stn.lf_rsp_ready,
                    u_dut.g_stn[1].u_stn.nm_rsp_valid, u_dut.g_stn[1].u_stn.nm_rsp_ready);
            endcase
        end
    endtask

    task dump_link;
        begin
            $display("  LINK0 REQ(stn1->stn0) i_v=%b i_r=%b o_v=%b o_r=%b sent=%0d nocred=%0d",
                     u_dut.g_link[0].g_l.g_cdc.u_rq_bwd.i_valid,
                     u_dut.g_link[0].g_l.g_cdc.u_rq_bwd.i_ready,
                     u_dut.g_link[0].g_l.g_cdc.u_rq_bwd.o_valid,
                     u_dut.g_link[0].g_l.g_cdc.u_rq_bwd.o_ready,
                     u_dut.g_link[0].g_l.g_cdc.u_rq_bwd.n_sent,
                     u_dut.g_link[0].g_l.g_cdc.u_rq_bwd.n_nocred);
            $display("  LINK0 RSP(stn0->stn1) i_v=%b i_r=%b o_v=%b o_r=%b sent=%0d nocred=%0d",
                     u_dut.g_link[0].g_l.g_cdc.u_rs_fwd.i_valid,
                     u_dut.g_link[0].g_l.g_cdc.u_rs_fwd.i_ready,
                     u_dut.g_link[0].g_l.g_cdc.u_rs_fwd.o_valid,
                     u_dut.g_link[0].g_l.g_cdc.u_rs_fwd.o_ready,
                     u_dut.g_link[0].g_l.g_cdc.u_rs_fwd.n_sent,
                     u_dut.g_link[0].g_l.g_cdc.u_rs_fwd.n_nocred);
        end
    endtask

`define NSU0(p) u_dut.g_stn[0].g_nsu[p].u_nsu

    task dump_nsu0;
        input integer p;
        begin
            $display("  NSU stn0 port %0d", p);
            case (p)
            0: begin
               $display("    fabric : req_v=%b req_r=%b rq_empty=%b rqf_full=%b in_body=%b start_rd=%b start_wr=%b body_wr=%b",
                    `NSU0(0).req_valid, `NSU0(0).req_ready, `NSU0(0).rq_empty,
                    `NSU0(0).rqf_full, `NSU0(0).in_body, `NSU0(0).start_rd,
                    `NSU0(0).start_wr, `NSU0(0).body_wr);
               $display("    unpack : w_multi=%b wsub=%0d w_subl=%b w_body=%b w_nsub=%0d",
                    `NSU0(0).w_multi, `NSU0(0).wsub, `NSU0(0).w_subl,
                    `NSU0(0).w_body, `NSU0(0).w_nsub);
               $display("    issue  : rq_size=%0d rq_len=%0d -> wlen=%0d wal=%h  awq_e=%b arq_e=%b wq_e=%b (full aw=%b ar=%b w=%b)",
                    `NSU0(0).rq_size, `NSU0(0).rq_len, `NSU0(0).rq_wlen,
                    `NSU0(0).rq_wal, `NSU0(0).awq_empty, `NSU0(0).arq_empty,
                    `NSU0(0).wq_empty, `NSU0(0).awq_full, `NSU0(0).arq_full,
                    `NSU0(0).wq_full);
               $display("    axi    : arv=%b arr=%b arlen=%0d arsize=%0d | awv=%b awr=%b awlen=%0d | wv=%b wr=%b wlast=%b | rv=%b rr=%b rlast=%b | bv=%b br=%b",
                    `NSU0(0).m_arvalid, `NSU0(0).m_arready, `NSU0(0).m_arlen,
                    `NSU0(0).m_arsize, `NSU0(0).m_awvalid, `NSU0(0).m_awready,
                    `NSU0(0).m_awlen, `NSU0(0).m_wvalid, `NSU0(0).m_wready,
                    `NSU0(0).m_wlast, `NSU0(0).m_rvalid, `NSU0(0).m_rready,
                    `NSU0(0).m_rlast, `NSU0(0).m_bvalid, `NSU0(0).m_bready);
               $display("    rsp    : w_busy=%b r_busy=%b r_active=%b rsf_e=%b rsf_f=%b rsp_v=%b rsp_r=%b",
                    `NSU0(0).w_busy, `NSU0(0).r_busy, `NSU0(0).r_active,
                    `NSU0(0).rsf_empty, `NSU0(0).rsf_full,
                    `NSU0(0).rsp_valid, `NSU0(0).rsp_ready);
               end
            1: begin
               $display("    fabric : req_v=%b req_r=%b rq_empty=%b rqf_full=%b in_body=%b start_rd=%b start_wr=%b body_wr=%b",
                    `NSU0(1).req_valid, `NSU0(1).req_ready, `NSU0(1).rq_empty,
                    `NSU0(1).rqf_full, `NSU0(1).in_body, `NSU0(1).start_rd,
                    `NSU0(1).start_wr, `NSU0(1).body_wr);
               $display("    unpack : w_multi=%b wsub=%0d w_subl=%b w_body=%b w_nsub=%0d",
                    `NSU0(1).w_multi, `NSU0(1).wsub, `NSU0(1).w_subl,
                    `NSU0(1).w_body, `NSU0(1).w_nsub);
               $display("    issue  : rq_size=%0d rq_len=%0d -> wlen=%0d wal=%h  awq_e=%b arq_e=%b wq_e=%b (full aw=%b ar=%b w=%b)",
                    `NSU0(1).rq_size, `NSU0(1).rq_len, `NSU0(1).rq_wlen,
                    `NSU0(1).rq_wal, `NSU0(1).awq_empty, `NSU0(1).arq_empty,
                    `NSU0(1).wq_empty, `NSU0(1).awq_full, `NSU0(1).arq_full,
                    `NSU0(1).wq_full);
               $display("    axi    : arv=%b arr=%b arlen=%0d arsize=%0d | awv=%b awr=%b awlen=%0d | wv=%b wr=%b wlast=%b | rv=%b rr=%b rlast=%b | bv=%b br=%b",
                    `NSU0(1).m_arvalid, `NSU0(1).m_arready, `NSU0(1).m_arlen,
                    `NSU0(1).m_arsize, `NSU0(1).m_awvalid, `NSU0(1).m_awready,
                    `NSU0(1).m_awlen, `NSU0(1).m_wvalid, `NSU0(1).m_wready,
                    `NSU0(1).m_wlast, `NSU0(1).m_rvalid, `NSU0(1).m_rready,
                    `NSU0(1).m_rlast, `NSU0(1).m_bvalid, `NSU0(1).m_bready);
               $display("    rsp    : w_busy=%b r_busy=%b r_active=%b rsf_e=%b rsf_f=%b rsp_v=%b rsp_r=%b fl_v=%b",
                    `NSU0(1).w_busy, `NSU0(1).r_busy, `NSU0(1).r_active,
                    `NSU0(1).rsf_empty, `NSU0(1).rsf_full,
                    `NSU0(1).rsp_valid, `NSU0(1).rsp_ready,
                    `NSU0(1).g_pack.fl_v);
               end
            2: begin
               $display("    fabric : req_v=%b req_r=%b rq_empty=%b rqf_full=%b in_body=%b start_rd=%b start_wr=%b body_wr=%b",
                    `NSU0(2).req_valid, `NSU0(2).req_ready, `NSU0(2).rq_empty,
                    `NSU0(2).rqf_full, `NSU0(2).in_body, `NSU0(2).start_rd,
                    `NSU0(2).start_wr, `NSU0(2).body_wr);
               $display("    unpack : w_multi=%b wsub=%0d w_subl=%b w_body=%b w_nsub=%0d",
                    `NSU0(2).w_multi, `NSU0(2).wsub, `NSU0(2).w_subl,
                    `NSU0(2).w_body, `NSU0(2).w_nsub);
               $display("    issue  : rq_size=%0d rq_len=%0d -> wlen=%0d wal=%h  awq_e=%b arq_e=%b wq_e=%b (full aw=%b ar=%b w=%b)",
                    `NSU0(2).rq_size, `NSU0(2).rq_len, `NSU0(2).rq_wlen,
                    `NSU0(2).rq_wal, `NSU0(2).awq_empty, `NSU0(2).arq_empty,
                    `NSU0(2).wq_empty, `NSU0(2).awq_full, `NSU0(2).arq_full,
                    `NSU0(2).wq_full);
               $display("    axi    : arv=%b arr=%b arlen=%0d arsize=%0d | awv=%b awr=%b awlen=%0d | wv=%b wr=%b wlast=%b | rv=%b rr=%b rlast=%b | bv=%b br=%b",
                    `NSU0(2).m_arvalid, `NSU0(2).m_arready, `NSU0(2).m_arlen,
                    `NSU0(2).m_arsize, `NSU0(2).m_awvalid, `NSU0(2).m_awready,
                    `NSU0(2).m_awlen, `NSU0(2).m_wvalid, `NSU0(2).m_wready,
                    `NSU0(2).m_wlast, `NSU0(2).m_rvalid, `NSU0(2).m_rready,
                    `NSU0(2).m_rlast, `NSU0(2).m_bvalid, `NSU0(2).m_bready);
               $display("    rsp    : w_busy=%b r_busy=%b r_active=%b rsf_e=%b rsf_f=%b rsp_v=%b rsp_r=%b fl_v=%b",
                    `NSU0(2).w_busy, `NSU0(2).r_busy, `NSU0(2).r_active,
                    `NSU0(2).rsf_empty, `NSU0(2).rsf_full,
                    `NSU0(2).rsp_valid, `NSU0(2).rsp_ready,
                    `NSU0(2).g_pack.fl_v);
               end
            3: begin
               $display("    fabric : req_v=%b req_r=%b rq_empty=%b rqf_full=%b in_body=%b start_rd=%b start_wr=%b body_wr=%b",
                    `NSU0(3).req_valid, `NSU0(3).req_ready, `NSU0(3).rq_empty,
                    `NSU0(3).rqf_full, `NSU0(3).in_body, `NSU0(3).start_rd,
                    `NSU0(3).start_wr, `NSU0(3).body_wr);
               $display("    unpack : w_multi=%b wsub=%0d w_subl=%b w_body=%b w_nsub=%0d",
                    `NSU0(3).w_multi, `NSU0(3).wsub, `NSU0(3).w_subl,
                    `NSU0(3).w_body, `NSU0(3).w_nsub);
               $display("    issue  : rq_size=%0d rq_len=%0d -> wlen=%0d wal=%h  awq_e=%b arq_e=%b wq_e=%b (full aw=%b ar=%b w=%b)",
                    `NSU0(3).rq_size, `NSU0(3).rq_len, `NSU0(3).rq_wlen,
                    `NSU0(3).rq_wal, `NSU0(3).awq_empty, `NSU0(3).arq_empty,
                    `NSU0(3).wq_empty, `NSU0(3).awq_full, `NSU0(3).arq_full,
                    `NSU0(3).wq_full);
               $display("    axi    : arv=%b arr=%b arlen=%0d arsize=%0d | awv=%b awr=%b awlen=%0d | wv=%b wr=%b wlast=%b | rv=%b rr=%b rlast=%b | bv=%b br=%b",
                    `NSU0(3).m_arvalid, `NSU0(3).m_arready, `NSU0(3).m_arlen,
                    `NSU0(3).m_arsize, `NSU0(3).m_awvalid, `NSU0(3).m_awready,
                    `NSU0(3).m_awlen, `NSU0(3).m_wvalid, `NSU0(3).m_wready,
                    `NSU0(3).m_wlast, `NSU0(3).m_rvalid, `NSU0(3).m_rready,
                    `NSU0(3).m_rlast, `NSU0(3).m_bvalid, `NSU0(3).m_bready);
               $display("    rsp    : w_busy=%b r_busy=%b r_active=%b rsf_e=%b rsf_f=%b rsp_v=%b rsp_r=%b fl_v=%b",
                    `NSU0(3).w_busy, `NSU0(3).r_busy, `NSU0(3).r_active,
                    `NSU0(3).rsf_empty, `NSU0(3).rsf_full,
                    `NSU0(3).rsp_valid, `NSU0(3).rsp_ready,
                    `NSU0(3).g_pack.fl_v);
               end
            endcase
        end
    endtask

    task dump_all;
        input integer port;
        begin
            $display("---------------- STATE AT STALL ----------------");
            dump_nmu;
            dump_stn(1);
            dump_link;
            dump_stn(0);
            dump_nsu0(port);
            $display("  manager pins : arv=%b arr=%b rv=%b rr=%b rlast=%b | awv=%b awr=%b wv=%b wr=%b bv=%b br=%b",
                     arvld[0], arrdy[0], rvld[0], rrdy[0], rlast[0],
                     awvld[0], awrdy[0], wvld[0], wrdy[0], bvld[0], brdy[0]);
            $display("  decerr count : %0d", stat_decerr);
            $display("------------------------------------------------");
        end
    endtask

    // ================================================================ the driver
    reg [63:0] got;
    reg [1:0]  gresp;
    reg        timed_out;

    task jread;
        input [AW-1:0] a;
        input [2:0]    sz;
        input [7:0]    len;
        input integer  port;
        integer t;
        begin
            timed_out = 0;
            @(negedge clk_ctrl);
            arid[0]   = 4'd1;
            araddr[0] = a;
            arsize[0] = sz;
            arlen[0]  = len;
            arvld[0]  = 1'b1;
            rrdy[0]   = 1'b1;

            t = 0;
            while (!(arrdy[0] === 1'b1) && (t < TMO)) begin
                @(negedge clk_ctrl); t = t + 1;
            end
            if (t >= TMO) begin
                $display("%0t HANG: AR never accepted, addr=%h size=%0d len=%0d",
                         $time, a, sz, len);
                dump_all(port);
                timed_out = 1; hangs = hangs + 1;
                @(negedge clk_ctrl); arvld[0] = 1'b0;
                disable jread;
            end
            @(negedge clk_ctrl); arvld[0] = 1'b0;

            t = 0;
            while (!(rvld[0] === 1'b1) && (t < TMO)) begin
                @(negedge clk_ctrl); t = t + 1;
            end
            if (t >= TMO) begin
                $display("%0t HANG: AR accepted, R never returned, addr=%h size=%0d len=%0d",
                         $time, a, sz, len);
                dump_all(port);
                timed_out = 1; hangs = hangs + 1;
                disable jread;
            end
            got   = rdata_v[63:0];
            gresp = rresp_v[1:0];
            @(negedge clk_ctrl);
        end
    endtask

    task jwrite;
        input [AW-1:0]  a;
        input [2:0]     sz;
        input [7:0]     len;
        input [63:0]    d;
        input [7:0]     strb;
        input integer   port;
        integer t;
        reg aw_done, w_done;
        begin
            timed_out = 0;
            @(negedge clk_ctrl);
            awid[0]   = 4'd2;
            awaddr[0] = a;
            awsize[0] = sz;
            awlen[0]  = len;
            awvld[0]  = 1'b1;
            wdata[0]  = {448'd0, d};
            wstrb[0]  = {56'd0, strb};
            wlast[0]  = 1'b1;
            wvld[0]   = 1'b1;
            brdy[0]   = 1'b1;
            aw_done = 0; w_done = 0;

            t = 0;
            while (!(aw_done && w_done) && (t < TMO)) begin
                if (awvld[0] && (awrdy[0] === 1'b1)) begin
                    @(negedge clk_ctrl); awvld[0] = 1'b0; aw_done = 1;
                end else if (wvld[0] && (wrdy[0] === 1'b1)) begin
                    @(negedge clk_ctrl); wvld[0] = 1'b0; w_done = 1;
                end else begin
                    @(negedge clk_ctrl);
                end
                t = t + 1;
            end
            if (t >= TMO) begin
                $display("%0t HANG: write address/data phase, addr=%h size=%0d aw_done=%b w_done=%b",
                         $time, a, sz, aw_done, w_done);
                dump_all(port);
                timed_out = 1; hangs = hangs + 1;
                @(negedge clk_ctrl); awvld[0] = 1'b0; wvld[0] = 1'b0;
                disable jwrite;
            end

            t = 0;
            while (!(bvld[0] === 1'b1) && (t < TMO)) begin
                @(negedge clk_ctrl); t = t + 1;
            end
            if (t >= TMO) begin
                $display("%0t HANG: write accepted, B never returned, addr=%h size=%0d",
                         $time, a, sz);
                dump_all(port);
                timed_out = 1; hangs = hangs + 1;
                disable jwrite;
            end
            gresp = bresp_v[1:0];
            @(negedge clk_ctrl);
        end
    endtask

    task step_rd;
        input [8*40-1:0] name;
        input [AW-1:0]   a;
        input [2:0]      sz;
        input integer    port;
        begin
            checks = checks + 1;
            $display("");
            $display("=== READ  %0s  addr=%h size=%0d (%0d bytes) ===",
                     name, a, sz, 1 << sz);
            jread(a, sz, 8'd0, port);
            if (timed_out) begin
                errors = errors + 1;
                $display("    RESULT: TIMEOUT");
            end else begin
                $display("    RESULT: rdata=%h rresp=%b", got, gresp);
            end
        end
    endtask

    task step_wr;
        input [8*40-1:0] name;
        input [AW-1:0]   a;
        input [2:0]      sz;
        input [63:0]     d;
        input [7:0]      strb;
        input integer    port;
        begin
            checks = checks + 1;
            $display("");
            $display("=== WRITE %0s  addr=%h size=%0d data=%h strb=%h ===",
                     name, a, sz, d, strb);
            jwrite(a, sz, 8'd0, d, strb, port);
            if (timed_out) begin
                errors = errors + 1;
                $display("    RESULT: TIMEOUT");
            end else begin
                $display("    RESULT: bresp=%b", gresp);
            end
        end
    endtask

    // ------------------------------------------------------- global watchdog
    initial begin
        #2000000;
        $display("");
        $display("WATCHDOG TIMEOUT -- the bench itself did not finish");
        $display("FAIL");
        $finish;
    end

    integer m;
    initial begin
        for (m = 0; m < NM; m = m + 1) begin
            awid[m] = 0; awaddr[m] = 0; awlen[m] = 0; awsize[m] = 0;
            awvld[m] = 0; wdata[m] = 0; wstrb[m] = 0; wlast[m] = 0;
            wvld[m] = 0; brdy[m] = 1; arid[m] = 0; araddr[m] = 0;
            arlen[m] = 0; arsize[m] = 0; arvld[m] = 0; rrdy[m] = 1;
        end

        $display("=========================================================");
        $display(" v6.7 JTAG repro: FW=%0d AW=%0d NQ=%0d LINK_CDC=%0d LINK_FULL=%0d",
                 FW, AW, NQ, P_CDC, P_FULL);
`ifdef LITE_AS_FULL
        $display(" ports 2,3: FULL AXI4 axi4_ram  (sb_line4_tb's assumption)");
`else
        $display(" ports 2,3: AXI4-LITE, truncated exactly as sb_bd_line4 does");
`endif
        $display("=========================================================");

        repeat (40) @(posedge clk_ctrl);
        rstn = 1;
        repeat (200) @(posedge clk_ctrl);

        // The four silicon probes, all on mesh 0 = station 0, all 64-bit
        // single beats because Vivado 2024.2 ignores create_hw_axi_txn -size.
        step_rd("mesh0 CTRL   (stn0 port1, AXI4 32b)", 43'h800000,          3'd3, 1);
        step_rd("mesh0 WIZ    (stn0 port3, LITE 32b)", 43'h900004,          3'd3, 3);
        step_rd("mesh0 DDRCTL (stn0 port2, LITE 32b)", 43'h0,               3'd3, 2);
        step_rd("mesh0 DRAM   (stn0 port0, AXI4 256b)", 43'h100_0000_0000,  3'd3, 0);

        // Same shapes as writes.
        step_wr("mesh0 CTRL   (stn0 port1, AXI4 32b)", 43'h800000, 3'd3,
                64'hDEAD_BEEF_CAFE_F00D, 8'hFF, 1);
        step_wr("mesh0 WIZ    (stn0 port3, LITE 32b)", 43'h900000, 3'd3,
                64'h1122_3344_5566_7788, 8'hFF, 3);
        step_wr("mesh0 DDRCTL (stn0 port2, LITE 32b)", 43'h0, 3'd3,
                64'h0000_0001_0000_0002, 8'hFF, 2);
        step_wr("mesh0 DRAM   (stn0 port0, AXI4 256b)", 43'h100_0000_0000, 3'd3,
                64'hA5A5_5A5A_0F0F_F0F0, 8'hFF, 0);

        // Reference: the same targets at 32-bit size, which is what v6.5 sent.
        step_rd("REF 32b mesh0 CTRL", 43'h800000,         3'd2, 1);
        step_rd("REF 32b mesh0 WIZ",  43'h900004,         3'd2, 3);
        step_rd("REF 32b mesh0 DDRC", 43'h0,              3'd2, 2);
        step_rd("REF 32b mesh0 DRAM", 43'h100_0000_0000,  3'd2, 0);

        $display("");
        $display("=========================================================");
        $display(" accesses=%0d  timeouts=%0d", checks, hangs);
        if (errors == 0) $display("PASS -- no access hung");
        else             $display("FAIL -- %0d of %0d accesses hung", errors, checks);
        $display("=========================================================");
        $finish;
    end
endmodule


// A TRUE AXI4-Lite subordinate: one address, one data beat, one response.
// It has no AWLEN/ARLEN to honour, which is the whole point -- an AXI4 burst
// arriving here is not shortened, it is simply not understood.
module axi_lite_slave #(
    parameter integer DW = 32,
    parameter integer AW = 43
)(
    input  wire            clk,
    input  wire            resetn,
    input  wire [AW-1:0]   awaddr,
    input  wire            awvalid,
    output wire            awready,
    input  wire [DW-1:0]   wdata,
    input  wire [DW/8-1:0] wstrb,
    input  wire            wvalid,
    output wire            wready,
    output wire [1:0]      bresp,
    output reg             bvalid,
    input  wire            bready,
    input  wire [AW-1:0]   araddr,
    input  wire            arvalid,
    output wire            arready,
    output reg  [DW-1:0]   rdata,
    output wire [1:0]      rresp,
    output reg             rvalid,
    input  wire            rready
);
    localparam integer N = 256;
    reg [DW-1:0] regs [0:N-1];
    integer k;
    initial for (k = 0; k < N; k = k + 1) regs[k] = {16'hC0DE, k[15:0]};

    wire [7:0] wi = awaddr[2 +: 8];
    wire [7:0] ri = araddr[2 +: 8];

    // Lite: the write completes only when BOTH the address and its single data
    // beat are present. A second data beat with no new address waits forever.
    wire wgo = awvalid && wvalid && (!bvalid || bready);
    assign awready = wgo;
    assign wready  = wgo;
    assign bresp   = 2'b00;

    wire rgo = arvalid && (!rvalid || rready);
    assign arready = rgo;
    assign rresp   = 2'b00;

    integer b;
    always @(posedge clk) begin
        if (!resetn) begin
            bvalid <= 1'b0;
            rvalid <= 1'b0;
        end else begin
            if (bvalid && bready) bvalid <= 1'b0;
            if (wgo) begin
                for (b = 0; b < DW/8; b = b + 1)
                    if (wstrb[b]) regs[wi][b*8 +: 8] <= wdata[b*8 +: 8];
                bvalid <= 1'b1;
            end
            if (rvalid && rready) rvalid <= 1'b0;
            if (rgo) begin
                rdata  <= regs[ri];
                rvalid <= 1'b1;
            end
        end
    end
endmodule

`default_nettype wire

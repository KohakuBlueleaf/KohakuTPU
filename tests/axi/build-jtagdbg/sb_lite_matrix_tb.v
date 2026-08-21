// Lite/width correctness matrix on the SHIPPED v6.7 station (sb_line4, real
// seg table, real Lite truncation). The jtagdbg bench proved nothing hangs;
// this one proves what the data actually does. It checks:
//   - read/write DATA correctness against a backdoor view of the endpoint
//   - AWLEN!=0 or AWSIZE!=2 ever presented to an AXI4-Lite port
//   - orphan W beats left parked at a Lite port after traffic drains
//   - which registers a write actually changed (footprint check)
// Port 3 (clk_wiz position) ignores WSTRB, port 2 (ddr-ctl position) honors
// it -- both are legal AXI4-Lite slave behaviors and silicon must survive both.
//
// Verdict: with the current RTL this bench REPRODUCES the silicon defects and
// says BUG-REPRODUCED; after a fix it must print CLEAN with zero violations.

`timescale 1ns / 1ps
`default_nettype none

module sb_lite_matrix_tb;

    localparam integer AW    = 43;
    localparam integer FW    = 256;
    localparam integer NQ    = 4;
    localparam integer PORTW = 2;
    localparam integer NS    = 4 * NQ;
    localparam integer NM    = 3;
    localparam integer MAXW  = 512;
    localparam integer MAXID = 4;
    localparam integer TMO   = 4000;

    integer data_err  = 0;
    integer proto_err = 0;
    integer hangs     = 0;
    integer checks    = 0;

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

    reg bclk0 = 0, bclk1 = 0, bclk2 = 0, bclk3 = 0;
    always #2.500 bclk0 = ~bclk0;
    always #2.497 bclk1 = ~bclk1;
    always #2.503 bclk2 = ~bclk2;
    always #2.499 bclk3 = ~bclk3;

    reg clk_ctrl = 0, clk_xdma = 0;
    always #5.000 clk_ctrl = ~clk_ctrl;
    always #2.000 clk_xdma = ~clk_xdma;

    reg clk_s0 = 0, clk_s1 = 0, clk_s2 = 0, clk_s3 = 0;
    always #1.667 clk_s0 = ~clk_s0;
    always #1.667 clk_s1 = ~clk_s1;
    always #1.667 clk_s2 = ~clk_s2;
    always #1.667 clk_s3 = ~clk_s3;

    reg clk_ddr0 = 0, clk_ddr1 = 0, clk_ddr2 = 0, clk_ddr3 = 0;
    always #1.667 clk_ddr0 = ~clk_ddr0;
    always #1.671 clk_ddr1 = ~clk_ddr1;
    always #1.663 clk_ddr2 = ~clk_ddr2;
    always #1.669 clk_ddr3 = ~clk_ddr3;

    reg rstn = 0;
    wire bus_rst = !rstn;
    wire [3:0] bclk = {bclk3, bclk2, bclk1, bclk0};
    wire [3:0] brst = {4{bus_rst}};
    wire [3:0] tb_mclk = {clk_s3, clk_s2, clk_s1, clk_s0};
    wire [3:0] tb_dclk = {clk_ddr3, clk_ddr2, clk_ddr1, clk_ddr0};

    // manager clock: 0 jtag=ctrl, 1 xdma, 2 xdma-lite=xdma
    wire mclk0 = clk_ctrl;
    wire mclk2 = clk_xdma;

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
               .NQ(NQ), .PORTW(PORTW), .TAGW(4), .OST(4),
               .STORE_FWD(1), .LUT_PER_BRAM(0), .TIMEOUT(0),
               .PIPE(4), .CRED(16), .STNW(2), .SRCW(2),
               .LINK_CDC(1), .MGR_STN(1), .LINK_FULL(0),
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

    // Endpoints: port0 full 256b ram, port1 full 32b ram, port2 Lite
    // strobe-HONORING, port3 Lite strobe-IGNORING (clk_wiz-like).
    genvar i;
    generate
    for (i = 0; i < NS; i = i + 1) begin : g_ep
        localparam integer QP = i % NQ;
        localparam integer DW = (QP == 0) ? FW : 32;

        if (DW < MAXW) begin : g_pad
            assign sp_rdata[i*MAXW + DW +: MAXW-DW] = {(MAXW-DW){1'b0}};
        end

        if (QP >= 2) begin : g_lite
            // The production shape after the fix: sb_axi2lite between the
            // station's full port and the Lite endpoint, exactly as the
            // regenerated sb_bd_line4 instantiates it.
            wire [AW-1:0] l_awaddr, l_araddr;
            wire          l_awvalid, l_awready, l_wvalid, l_wready;
            wire          l_bvalid, l_bready, l_arvalid, l_arready;
            wire          l_rvalid, l_rready;
            wire [31:0]   l_wdata, l_rdata;
            wire [3:0]    l_wstrb;
            wire [1:0]    l_bresp, l_rresp;

            sb_axi2lite #(.DW(32), .AW(AW), .IDW(MAXID)) u_conv (
                .clk(sclk[i]), .resetn(rstn),
                .s_awid(sp_awid[i*MAXID +: MAXID]),
                .s_awaddr(sp_awaddr[i*AW +: AW]),
                .s_awlen(sp_awlen[i*8 +: 8]),
                .s_awvalid(sp_awvalid[i]), .s_awready(sp_awready[i]),
                .s_wdata(sp_wdata[i*MAXW +: 32]),
                .s_wstrb(sp_wstrb[i*(MAXW/8) +: 4]),
                .s_wlast(sp_wlast[i]),
                .s_wvalid(sp_wvalid[i]), .s_wready(sp_wready[i]),
                .s_bid(sp_bid[i*MAXID +: MAXID]),
                .s_bresp(sp_bresp[i*2 +: 2]),
                .s_bvalid(sp_bvalid[i]), .s_bready(sp_bready[i]),
                .s_arid(sp_arid[i*MAXID +: MAXID]),
                .s_araddr(sp_araddr[i*AW +: AW]),
                .s_arlen(sp_arlen[i*8 +: 8]),
                .s_arvalid(sp_arvalid[i]), .s_arready(sp_arready[i]),
                .s_rid(sp_rid[i*MAXID +: MAXID]),
                .s_rdata(sp_rdata[i*MAXW +: 32]),
                .s_rresp(sp_rresp[i*2 +: 2]),
                .s_rlast(sp_rlast[i]),
                .s_rvalid(sp_rvalid[i]), .s_rready(sp_rready[i]),
                .m_awaddr(l_awaddr),
                .m_awvalid(l_awvalid), .m_awready(l_awready),
                .m_wdata(l_wdata), .m_wstrb(l_wstrb),
                .m_wvalid(l_wvalid), .m_wready(l_wready),
                .m_bresp(l_bresp),
                .m_bvalid(l_bvalid), .m_bready(l_bready),
                .m_araddr(l_araddr),
                .m_arvalid(l_arvalid), .m_arready(l_arready),
                .m_rdata(l_rdata), .m_rresp(l_rresp),
                .m_rvalid(l_rvalid), .m_rready(l_rready)
            );

            axi_lite_slave2 #(.DW(32), .AW(AW),
                              .IGNORE_WSTRB((QP == 3) ? 1 : 0),
                              .NAME_HI(i)) u_lite (
                .clk(sclk[i]), .resetn(rstn),
                .awaddr(l_awaddr),
                .awvalid(l_awvalid), .awready(l_awready),
                .wdata(l_wdata), .wstrb(l_wstrb),
                .wvalid(l_wvalid), .wready(l_wready),
                .bresp(l_bresp),
                .bvalid(l_bvalid), .bready(l_bready),
                .araddr(l_araddr),
                .arvalid(l_arvalid), .arready(l_arready),
                .rdata(l_rdata), .rresp(l_rresp),
                .rvalid(l_rvalid), .rready(l_rready)
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

    // AWLEN at the sp_ boundary is legal now (the converter owns the burst);
    // the Lite contract is enforced by the strict slave and orphan checks.
    integer lite_awlen_viol = 0;
    integer lite_awsize_viol = 0;

    task check_orphans;
        input integer port;
        begin
            if (sp_wvalid[port]) begin
                proto_err = proto_err + 1;
                $display("    VIOLATION: orphan W beat(s) parked at Lite port %0d after drain (wvalid still high)",
                         port);
            end else begin
                $display("    orphan check port %0d: clean", port);
            end
        end
    endtask

    // ------------------------------------------------------------- the driver
    reg [63:0] got;
    reg [1:0]  gresp;
    reg        timed_out;

    // Manager 0 clocks on clk_ctrl, managers 1/2 on clk_xdma; driving them
    // all on clk_ctrl edges leaves mgr-2 pins unsynchronized to its port.
    task tickp;
        input integer m;
        begin
            if (m == 0) @(posedge clk_ctrl);
            else        @(posedge clk_xdma);
        end
    endtask

    task tickn;
        input integer m;
        begin
            if (m == 0) @(negedge clk_ctrl);
            else        @(negedge clk_xdma);
        end
    endtask

    task jread;
        input integer  m;
        input [AW-1:0] a;
        input [2:0]    sz;
        integer t;
        begin
            timed_out = 0;
            tickn(m);
            arid[m]   = 4'd1;
            araddr[m] = a;
            arsize[m] = sz;
            arlen[m]  = 8'd0;
            arvld[m]  = 1'b1;
            rrdy[m]   = 1'b1;
            t = 0;
            begin : jr_ar
                forever begin
                    tickp(m); t = t + 1;
                    if (arvld[m] && (arrdy[m] === 1'b1)) begin
                        arvld[m] <= 1'b0;
                        disable jr_ar;
                    end
                    if (t >= TMO) begin
                        $display("    HANG: AR never accepted (mgr %0d addr %h)", m, a);
                        timed_out = 1; hangs = hangs + 1; arvld[m] = 1'b0;
                        disable jread;
                    end
                end
            end
            t = 0;
            while (!(rvld[m] === 1'b1) && (t < TMO)) begin
                tickn(m); t = t + 1;
            end
            if (t >= TMO) begin
                $display("    HANG: R never returned (mgr %0d addr %h)", m, a);
                timed_out = 1; hangs = hangs + 1;
                disable jread;
            end
            got   = rdata_v[m*MAXW +: 64];
            gresp = rresp_v[m*2 +: 2];
            tickn(m);
        end
    endtask

    task jwrite;
        input integer   m;
        input [AW-1:0]  a;
        input [2:0]     sz;
        input [63:0]    d;
        input [7:0]     strb;
        integer t;
        reg aw_done, w_done, aw_hs, w_hs;
        begin
            timed_out = 0;
            tickn(m);
            awid[m]   = 4'd2;
            awaddr[m] = a;
            awsize[m] = sz;
            awlen[m]  = 8'd0;
            awvld[m]  = 1'b1;
            wdata[m]  = {448'd0, d};
            wstrb[m]  = {56'd0, strb};
            wlast[m]  = 1'b1;
            wvld[m]   = 1'b1;
            brdy[m]   = 1'b1;
            aw_done = 0; w_done = 0;
            t = 0;
            while (!(aw_done && w_done) && (t < TMO)) begin
                tickp(m);
                // Sample both readies BEFORE acting; NBA deasserts hold this
                // edge (a delay here fakes a handshake that never clocked).
                aw_hs = awvld[m] && (awrdy[m] === 1'b1);
                w_hs  = wvld[m] && (wrdy[m] === 1'b1);
                if (aw_hs) begin awvld[m] <= 1'b0; aw_done = 1; end
                if (w_hs)  begin wvld[m]  <= 1'b0; w_done  = 1; end
                t = t + 1;
            end
            if (t >= TMO) begin
                $display("    HANG: AW/W phase (mgr %0d addr %h aw=%b w=%b)",
                         m, a, aw_done, w_done);
                timed_out = 1; hangs = hangs + 1;
                awvld[m] = 1'b0; wvld[m] = 1'b0;
                disable jwrite;
            end
            t = 0;
            while (!(bvld[m] === 1'b1) && (t < TMO)) begin
                tickn(m); t = t + 1;
            end
            if (t >= TMO) begin
                $display("    HANG: B never returned (mgr %0d addr %h)", m, a);
                timed_out = 1; hangs = hangs + 1;
                disable jwrite;
            end
            gresp = bresp_v[m*2 +: 2];
            tickn(m);
        end
    endtask

    task settle;
        begin
            repeat (600) @(negedge clk_ctrl);
        end
    endtask

    // Backdoor views. Station 0's wizard port is g_ep[3], its ddrctl g_ep[2];
    // station 2's wizard is g_ep[11].
    task expect64;
        input [8*44-1:0] name;
        input [63:0]     want;
        begin
            checks = checks + 1;
            if (timed_out) begin
                data_err = data_err + 1;
                $display("    CHECK %0s: TIMEOUT", name);
            end else if (got !== want) begin
                data_err = data_err + 1;
                $display("    CHECK %0s: got %h want %h  DATA-ERROR", name, got, want);
            end else begin
                $display("    CHECK %0s: %h ok", name, got);
            end
        end
    endtask

    task expect_reg;
        input [8*44-1:0] name;
        input [31:0]     have;
        input [31:0]     want;
        begin
            checks = checks + 1;
            if (have !== want) begin
                data_err = data_err + 1;
                $display("    CHECK %0s: reg holds %h want %h  DATA-ERROR", name, have, want);
            end else begin
                $display("    CHECK %0s: reg %h ok", name, have);
            end
        end
    endtask

    initial begin
        #3000000;
        $display("WATCHDOG TIMEOUT");
        $display("MATRIX-FAIL hangs=%0d", hangs + 1);
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

        $display("==============================================================");
        $display(" Lite/width correctness matrix on shipped v6.7 station");
        $display(" port2 = Lite HONORS wstrb, port3 = Lite IGNORES wstrb");
        $display("==============================================================");

        repeat (40) @(posedge clk_ctrl);
        rstn = 1;
        repeat (300) @(posedge clk_ctrl);

        // ---- S1: 64-bit reads of the wizard, every lane. Registers preload
        //          as regs[k] = C0DE_000k; a correct fabric returns the pair.
        $display("");
        $display("--- S1: 64b JTAG-shaped reads, wizard stn0 (Lite, ignore-wstrb) ---");
        jread(0, 43'h900000, 3'd3);
        expect64("wiz +0x00 lane0", {32'hC0DE0001, 32'hC0DE0000});
        jread(0, 43'h900008, 3'd3);
        expect64("wiz +0x08 lane1", {32'hC0DE0003, 32'hC0DE0002});
        jread(0, 43'h900010, 3'd3);
        expect64("wiz +0x10 lane2", {32'hC0DE0005, 32'hC0DE0004});
        jread(0, 43'h900018, 3'd3);
        expect64("wiz +0x18 lane3", {32'hC0DE0007, 32'hC0DE0006});
        jread(0, 43'h900020, 3'd3);
        expect64("wiz +0x20 lane0", {32'hC0DE0009, 32'hC0DE0008});

        // ---- S2: 64-bit write to the wizard, then footprint + readback.
        $display("");
        $display("--- S2: 64b JTAG-shaped write, wizard stn0 @+0x08 ---");
        jwrite(0, 43'h900008, 3'd3, 64'hB1B1B1B1_A0A0A0A0, 8'hFF);
        $display("    bresp=%b timed_out=%b", gresp, timed_out);
        settle;
        expect_reg("wiz reg[2] (0x08)", tb_wiz0_reg(2), 32'hA0A0A0A0);
        expect_reg("wiz reg[3] (0x0C)", tb_wiz0_reg(3), 32'hB1B1B1B1);
        expect_reg("wiz reg[0] intact", tb_wiz0_reg(0), 32'hC0DE0000);
        expect_reg("wiz reg[1] intact", tb_wiz0_reg(1), 32'hC0DE0001);
        expect_reg("wiz reg[4] intact", tb_wiz0_reg(4), 32'hC0DE0004);
        check_orphans(3);

        // ---- S3: second write observes drifted pairing if beats parked.
        $display("");
        $display("--- S3: 64b JTAG-shaped write #2, wizard stn0 @+0x18 ---");
        jwrite(0, 43'h900018, 3'd3, 64'hD3D3D3D3_C2C2C2C2, 8'hFF);
        settle;
        expect_reg("wiz reg[6] (0x18)", tb_wiz0_reg(6), 32'hC2C2C2C2);
        expect_reg("wiz reg[7] (0x1C)", tb_wiz0_reg(7), 32'hD3D3D3D3);
        check_orphans(3);

        // ---- S4: honor-wstrb Lite port (ddrctl stn0): same write shape.
        $display("");
        $display("--- S4: 64b JTAG-shaped write, ddrctl stn0 @+0x08 (honors wstrb) ---");
        jwrite(0, 43'h000008, 3'd3, 64'h9999_8888_7777_6666, 8'hFF);
        settle;
        expect_reg("ddrc reg[2] (0x08)", tb_ddrc0_reg(2), 32'h77776666);
        expect_reg("ddrc reg[3] (0x0C)", tb_ddrc0_reg(3), 32'h99998888);
        expect_reg("ddrc reg[0] intact", tb_ddrc0_reg(0), 32'hC0DE0000);
        check_orphans(2);

        // ---- S5: XDMA-Lite-shaped manager (32-bit) against a FRESH wizard
        //          (station 2), the silicon clean-state experiment.
        $display("");
        $display("--- S5: 32b XDMA-Lite-shaped write+read, wizard stn2 @+0x08 ---");
        jwrite(2, 43'h920008, 3'd2, 64'h0000_0000_5555_AAAA, 8'h0F);
        $display("    bresp=%b timed_out=%b", gresp, timed_out);
        settle;
        expect_reg("wiz2 reg[2] (0x08)", tb_wiz2_reg(2), 32'h5555AAAA);
        expect_reg("wiz2 reg[0] intact", tb_wiz2_reg(0), 32'hC0DE0000);
        check_orphans(11);
        jread(2, 43'h920008, 3'd2);
        checks = checks + 1;
        if (timed_out) begin
            data_err = data_err + 1;
            $display("    CHECK wiz2 readback: TIMEOUT (the silicon FF wedge)");
        end else if (got[31:0] !== 32'h5555AAAA) begin
            data_err = data_err + 1;
            $display("    CHECK wiz2 readback: got %h want 5555aaaa  DATA-ERROR", got[31:0]);
        end else begin
            $display("    CHECK wiz2 readback: %h ok", got[31:0]);
        end

        // ---- S6: controls -- the same shapes against FULL ports must be exact.
        $display("");
        $display("--- S6: controls on full AXI4 ports ---");
        jwrite(0, 43'h800010, 3'd3, 64'hFEED_F00D_0123_4567, 8'hFF);
        jread(0, 43'h800010, 3'd3);
        expect64("ctrl +0x10 rb", 64'hFEED_F00D_0123_4567);
        jwrite(0, 43'h100_0000_0040, 3'd3, 64'h5A5A_A5A5_3C3C_C3C3, 8'hFF);
        jread(0, 43'h100_0000_0040, 3'd3);
        expect64("dram +0x40 rb", 64'h5A5A_A5A5_3C3C_C3C3);

        $display("");
        $display("==============================================================");
        $display(" checks=%0d data_errors=%0d hangs=%0d awlen_viol=%0d awsize_viol=%0d orphan/proto=%0d",
                 checks, data_err, hangs, lite_awlen_viol, lite_awsize_viol,
                 proto_err);
        if (data_err == 0 && hangs == 0 && lite_awlen_viol == 0
            && lite_awsize_viol == 0 && proto_err == 0)
            $display("MATRIX-CLEAN the station is Lite-correct");
        else
            $display("MATRIX-REPRODUCED defects are live in this RTL");
        $display("==============================================================");
        $finish;
    end

    // xsim forbids hierarchical refs into generate blocks from expressions in
    // some contexts; functions wrapping them keep the checks readable.
    function [31:0] tb_wiz0_reg;  input integer k;
        tb_wiz0_reg = g_ep[3].g_lite.u_lite.regs[k];
    endfunction
    function [31:0] tb_ddrc0_reg; input integer k;
        tb_ddrc0_reg = g_ep[2].g_lite.u_lite.regs[k];
    endfunction
    function [31:0] tb_wiz2_reg;  input integer k;
        tb_wiz2_reg = g_ep[11].g_lite.u_lite.regs[k];
    endfunction
endmodule


// Strict AXI4-Lite subordinate with a write-commit log. IGNORE_WSTRB=1 models
// the register-block slaves that legally apply every byte lane; 0 honors
// strobes byte-precisely.
module axi_lite_slave2 #(
    parameter integer DW = 32,
    parameter integer AW = 43,
    parameter integer IGNORE_WSTRB = 0,
    parameter integer NAME_HI = 0
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
                $display("%0t     lite[%0d] WRITE commit addr=%h data=%h strb=%h%s",
                         $time, NAME_HI, awaddr[15:0], wdata, wstrb,
                         (IGNORE_WSTRB != 0) ? " (wstrb IGNORED)" : "");
                if (IGNORE_WSTRB != 0) regs[wi] <= wdata;
                else
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

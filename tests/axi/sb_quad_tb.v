// sb_quad: four stations, one per SLR, eight axi4_ram endpoints on four local
// clocks. Proves the root reaches every leaf over a link and every leaf returns.

// Drive and sample at NEGEDGE, transfer at the posedge between. Sampling right
// after a posedge reads pre-NBA values and reports X for every burst.

`timescale 1ns / 1ps
`default_nettype none

module sb_quad_tb;
    // -d SB_AW32 runs the exact shape the validation BD builds: jtag_axi is a
    // 32-bit master, so the map has to fit in 32 bits there.
`ifdef SB_AW32
    localparam integer AW    = 32;
`else
    localparam integer AW    = 40;
`endif
    localparam integer MAXW  = 512;
    localparam integer MAXID = 4;
    localparam integer NM    = 3;
    localparam integer NPS   = 2;
    localparam integer NS    = 4 * NPS;
    localparam real    TS    = 0.001;

    integer errors = 0;
    integer checks = 0;
    reg [1:0] exp_b = 2'b00;

    reg bus_clk = 0, clk_ctrl = 0, clk_xdma = 0;
    reg clk_s0 = 0, clk_s1 = 0, clk_s2 = 0, clk_s3 = 0;
    always begin  // 400.0 MHz
        #1.250 bus_clk  = ~bus_clk;
    end
    always begin  // 100.0 MHz
        #5.000 clk_ctrl = ~clk_ctrl;
    end
    always begin  // 250.0 MHz
        #2.000 clk_xdma = ~clk_xdma;
    end
    always begin  // 237.1 MHz, deliberately odd
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

    // Station 1 is the root, so its endpoints are first in the flat array.
    function integer sidx;
        input integer slr;
        begin
            sidx = (slr == 1) ? 0 : (slr == 0) ? NPS
                 : (slr == 2) ? 2*NPS : 3*NPS;
        end
    endfunction

    function [AW-1:0] adr;
        input integer slr, ep;
        begin adr = (slr << (AW - 4)) | (ep << 16); end
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

`ifdef SB_CRED1
    localparam integer P_CRED = 1;
`else
    localparam integer P_CRED = 16;
`endif

    sb_quad #(.NPS(NPS), .CRED(P_CRED), .AW(AW)) u_dut (
        .bus_clk(bus_clk), .bus_rst(bus_rst),
        .clk_ctrl(clk_ctrl), .aresetn_ctrl(rstn),
        .clk_xdma(clk_xdma), .aresetn_xdma(rstn),
        .clk_s0(clk_s0), .aresetn_s0(rstn),
        .clk_s1(clk_s1), .aresetn_s1(rstn),
        .clk_s2(clk_s2), .aresetn_s2(rstn),
        .clk_s3(clk_s3), .aresetn_s3(rstn),
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

    wire [NS-1:0] sclk = {clk_s3, clk_s3, clk_s2, clk_s2,
                          clk_s0, clk_s0, clk_s1, clk_s1};

    wire [NM-1:0]    mclk = {clk_xdma, clk_xdma, clk_ctrl};
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

    genvar i;
    generate
    for (i = 0; i < NS; i = i + 1) begin : g_ram
        localparam integer DW = (i % NPS == 0) ? MAXW : 32;
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

    // ------------------------------------------- master 0, 32-bit, clk_ctrl
    task j_write;
        input [AW-1:0] a;
        begin
            @(negedge clk_ctrl); #TS;
            awid[0] = 4'd1; awaddr[0] = a; awlen[0] = 8'd0;
            awsize[0] = 3'd2; awvld[0] = 1'b1;
            wdata[0] = {480'd0, pat32(a)};
            wstrb[0] = {60'd0, 4'hF}; wlast[0] = 1'b1; wvld[0] = 1'b1;
            brdy[0]  = 1'b1;
            #TS;
            while (!awrdy[0]) begin @(negedge clk_ctrl); #TS; end
            @(negedge clk_ctrl); #TS; awvld[0] = 1'b0;
            while (!wrdy[0]) begin @(negedge clk_ctrl); #TS; end
            @(negedge clk_ctrl); #TS; wvld[0] = 1'b0;
            while (!bvld[0]) begin @(negedge clk_ctrl); #TS; end
            if (bresp_v[1:0] !== exp_b) begin
                errors = errors + 1;
                $display("%0t FAIL jtag write @%h bresp %b exp %b", $time, a,
                         bresp_v[1:0], exp_b);
            end
            @(negedge clk_ctrl); #TS; brdy[0] = 1'b1;
        end
    endtask

    task j_read;
        input [AW-1:0] a;
        input [1:0]    exp_resp;
        begin
            @(negedge clk_ctrl); #TS;
            arid[0] = 4'd2; araddr[0] = a; arlen[0] = 8'd0;
            arsize[0] = 3'd2; arvld[0] = 1'b1; rrdy[0] = 1'b1;
            #TS;
            while (!arrdy[0]) begin @(negedge clk_ctrl); #TS; end
            @(negedge clk_ctrl); #TS; arvld[0] = 1'b0;
            while (!rvld[0]) begin @(negedge clk_ctrl); #TS; end
            if (rresp_v[1:0] !== exp_resp) begin
                errors = errors + 1;
                $display("%0t FAIL jtag read @%h rresp %b exp %b", $time, a,
                         rresp_v[1:0], exp_resp);
            end else if (exp_resp == 2'b00) begin
                chk({480'd0, rdata_v[31:0]}, {480'd0, pat32(a)}, a);
            end
            @(negedge clk_ctrl); #TS; rrdy[0] = 1'b1;
        end
    endtask

    // ----------------------------------------- master 1, 512-bit, clk_xdma
    task x_write;
        input [AW-1:0] a;
        input [7:0]    len;
        integer        b;
        begin
            @(negedge clk_xdma); #TS;
            awid[1] = 4'd3; awaddr[1] = a; awlen[1] = len;
            awsize[1] = 3'd6; awvld[1] = 1'b1; brdy[1] = 1'b1;
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
            arsize[1] = 3'd6; arvld[1] = 1'b1; rrdy[1] = 1'b1;
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

    // ------------------------------------ master 2, AXI-Lite 32-bit, clk_xdma
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

    // ------------------------------------------------------------------ main
    integer s, e, n;
    integer pi_i, pc_i;
    integer t0, t1, t2, cyc_loc, cyc_rem;
    integer hol_alone, hol_busy;
    integer hw_i, hr_i;
    integer b_alone, b_busy, bw_i, br_i;
    integer seed;
    reg [AW-1:0] ra;

    initial begin
        seed = 32'h5EED_0004;
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

        $display("--- phase 1: jtag reaches every endpoint in every SLR");
        for (s = 0; s < 4; s = s + 1) begin
            for (e = 0; e < NPS; e = e + 1) begin
                j_write(adr(s, e) + 40'h40);
                j_read (adr(s, e) + 40'h40, 2'b00);
            end
        end

        $display("--- phase 2: xdma bursts to each SLR's wide endpoint");
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
        // A miss must never reach the fabric, so no endpoint may have moved.
        j_read(adr(3, 1) + 40'h40, 2'b00);

        $display("--- phase 5: three managers, three different SLRs, at once");
        fork
            begin j_write(adr(0, 1) + 40'h200); j_read(adr(0, 1) + 40'h200, 2'b00); end
            begin x_write(adr(2, 0) + 40'h900, 8'd7); x_read(adr(2, 0) + 40'h900, 8'd7); end
            begin l_write(adr(3, 1) + 40'h300); l_read(adr(3, 1) + 40'h300); end
        join

        $display("--- phase 6: two managers contend for one remote leaf");
        fork
            begin : p6_j
                for (pi_i = 0; pi_i < 6; pi_i = pi_i + 1) begin
                    j_write(adr(3, 1) + 40'h400 + pi_i*4);
                    j_read (adr(3, 1) + 40'h400 + pi_i*4, 2'b00);
                end
            end
            begin : p6_x
                for (pc_i = 0; pc_i < 6; pc_i = pc_i + 1) begin
                    x_write(adr(3, 0) + 40'h1000 + pc_i*512, 8'd7);
                    x_read (adr(3, 0) + 40'h1000 + pc_i*512, 8'd7);
                end
            end
        join

        $display("--- phase 7: randomised traffic across all four stations");
        for (n = 0; n < 24; n = n + 1) begin
            s  = {$random(seed)} % 4;
            e  = {$random(seed)} % NPS;
            ra = adr(s, e) + (({$random(seed)} % 32) * 4) + 40'h2000;
            if (e == 0) begin
                x_write(ra & ~40'h3F, 8'd3);
                x_read (ra & ~40'h3F, 8'd3);
            end else begin
                j_write(ra); j_read(ra, 2'b00);
            end
        end

        $display("--- phase 8: local versus remote 16-beat read");
        @(negedge clk_xdma); #TS;
        x_write(adr(1, 0) + 40'h3000, 8'd15);
        t0 = $time;
        x_read(adr(1, 0) + 40'h3000, 8'd15);
        cyc_loc = ($time - t0) / 4;
        x_write(adr(3, 0) + 40'h3000, 8'd15);
        t1 = $time;
        x_read(adr(3, 0) + 40'h3000, 8'd15);
        cyc_rem = ($time - t1) / 4;
        $display("    BW  local 16-beat %0d xdma cycles", cyc_loc);
        $display("    BW  remote 16-beat %0d xdma cycles", cyc_rem);

        // The cost of holding a grant for a whole packet: a short read behind
        // the longest legal write. 63 is the 4 KB rule's ceiling at 512 bits.
        $display("--- phase 9: head-of-line cost of packet-atomic arbitration");
        j_write(adr(3, 1) + 40'h600);
        t0 = $time;
        j_read(adr(3, 1) + 40'h600, 2'b00);
        hol_alone = ($time - t0) / 10;
        // A SUSTAINED stream, not one burst: a single write finishes inside the
        // store-and-forward delay and the read never actually contends.
        hol_busy = 0;
        fork
            begin : hol_w
                for (hw_i = 0; hw_i < 4; hw_i = hw_i + 1) begin
                    x_write(adr(3, 0) + 40'h4000 + hw_i*4096, 8'd63);
                end
            end
            begin : hol_r
                repeat (20) @(posedge clk_ctrl);
                for (hr_i = 0; hr_i < 5; hr_i = hr_i + 1) begin
                    t2 = $time;
                    j_read(adr(3, 1) + 40'h600, 2'b00);
                    if ((($time - t2) / 10) > hol_busy) begin
                        hol_busy = ($time - t2) / 10;
                    end
                end
            end
        join
        $display("    HOL read alone %0d ctrl cycles, worst behind 4x64-beat writes %0d",
                 hol_alone, hol_busy);

        // Does a 2-bit B queue behind 512-bit R data in the shared RSP queue?
        // This is what a separate narrow completion path would buy.
        $display("--- phase 10: write completion behind sustained read bursts");
        t0 = $time;
        j_write(adr(3, 1) + 40'h700);
        b_alone = ($time - t0) / 10;
        b_busy = 0;
        fork
            begin : br_r
                for (bw_i = 0; bw_i < 4; bw_i = bw_i + 1) begin
                    x_read(adr(3, 0) + 40'h4000 + bw_i*4096, 8'd63);
                end
            end
            begin : br_b
                repeat (20) @(posedge clk_ctrl);
                for (br_i = 0; br_i < 5; br_i = br_i + 1) begin
                    t1 = $time;
                    j_write(adr(3, 1) + 40'h700);
                    if ((($time - t1) / 10) > b_busy) begin
                        b_busy = ($time - t1) / 10;
                    end
                end
            end
        join
        $display("    B latency alone %0d ctrl cycles, behind 4x64-beat reads %0d",
                 b_alone, b_busy);

        $display("--- phase 11: reset mid-traffic, then recover");
        fork
            begin x_write(adr(2, 0) + 40'h4000, 8'd7); end
            begin repeat (30) @(posedge bus_clk); rstn = 0;
                  repeat (20) @(posedge bus_clk); rstn = 1; end
        join_any
        disable fork;
        rstn = 0;
        repeat (20) @(posedge bus_clk);
        rstn = 1;
        repeat (40) @(posedge bus_clk);
        for (n = 0; n < NM; n = n + 1) begin
            awvld[n] = 0; wvld[n] = 0; arvld[n] = 0; wlast[n] = 0;
        end
        repeat (20) @(posedge bus_clk);
        j_write(adr(0, 1) + 40'h500); j_read(adr(0, 1) + 40'h500, 2'b00);
        x_write(adr(3, 0) + 40'h5000, 8'd3);
        x_read (adr(3, 0) + 40'h5000, 8'd3);

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
endmodule

`default_nettype wire

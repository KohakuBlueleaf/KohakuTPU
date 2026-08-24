// sb_root9 against nine axi4_ram slaves on four clocks, none harmonic with the
// bus. Proves decode, CDC, mixed width, bursts and the DECERR path.

// The 32-bit manager writing a 512-bit subordinate is the interesting case: a
// narrow transfer, strobe-placed in and lane-picked back, with no converter.

// Drive and sample at NEGEDGE, transfer at the posedge between. Sampling right
// after a posedge reads pre-NBA values and reports X for every burst.

`timescale 1ns / 1ps
`default_nettype none

module sb_root9_tb;
    localparam integer AW    = 40;
    localparam integer MAXW  = 512;
    localparam integer MAXID = 4;
    localparam integer NM    = 3;
    localparam integer NS    = 9;
    localparam real    TS    = 0.001;       // combinational settle after an edge

    integer errors = 0;
    integer checks = 0;
    reg [1:0] exp_b = 2'b00;

    // ------------------------------------------------------------- clocks
    reg bus_clk = 0, clk_ctrl = 0, clk_xdma = 0, clk_mesh = 0, clk_ddr = 0;
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
        #2.109 clk_mesh = ~clk_mesh;
    end
    always begin  // 299.9 MHz
        #1.667 clk_ddr  = ~clk_ddr;
    end

    reg rstn = 0;
    wire bus_rst = !rstn;

    // ---------------------------------------------------- manager port regs
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

    wire [NM-1:0] awrdy, wrdy, bvld, arrdy, rvld, rlast;
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

    // -------------------------------------------------- subordinate side nets
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

    // -d SB_MINAREA runs the speed-for-size corner: one outstanding, no
    // store-and-forward, so the hub holds its grant through a FIFO underrun.
`ifdef SB_MINAREA
    localparam integer P_TAGW = 1, P_OST = 1, P_SF = 0;
`else
    localparam integer P_TAGW = 4, P_OST = 4, P_SF = 1;
`endif
    // -d SB_FW256 halves the fabric; the 512-bit manager then splits 2:1.
`ifdef SB_FW256
    localparam integer P_FW = 256;
`else
    localparam integer P_FW = 512;
`endif

    // -d SB_TIMEOUT proves error containment: a stalled subordinate must give
    // the manager SLVERR instead of hanging the fabric.
`ifdef SB_TIMEOUT
    localparam integer P_TO = 2000;
`else
    localparam integer P_TO = 0;
`endif

    sb_root9 #(.TAGW(P_TAGW), .OST(P_OST), .STORE_FWD(P_SF), .FW(P_FW),
               .TIMEOUT(P_TO)) u_dut (
        .bus_clk(bus_clk), .bus_rst(bus_rst),
        .clk_ctrl(clk_ctrl), .aresetn_ctrl(rstn),
        .clk_xdma(clk_xdma), .aresetn_xdma(rstn),
        .clk_mesh(clk_mesh), .aresetn_mesh(rstn),
        .clk_ddr(clk_ddr),   .aresetn_ddr(rstn),
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

    // ------------------------------------------------------------- slaves
    wire [NS-1:0] sclk = { clk_ctrl, clk_ctrl, clk_ctrl, clk_ctrl,
                           clk_ddr, clk_mesh, clk_ctrl, clk_mesh, clk_ctrl };

    // Gate both directions: masking only READY leaves the RAM seeing VALID, so
    // it completes the write behind the fabric's back and nothing ever hangs.
    reg  [NS-1:0] stall = {NS{1'b0}};
    wire [NS-1:0] ram_awready, ram_wready;
    wire [NS-1:0] ram_awvalid = sp_awvalid & ~stall;
    wire [NS-1:0] ram_wvalid  = sp_wvalid  & ~stall;
    assign sp_awready = ram_awready & ~stall;
    assign sp_wready  = ram_wready  & ~stall;

    // A checker on every shim boundary, each on ITS OWN port clock -- sampling
    // an AXI port on the fabric clock invents violations that are not there.
    wire [NM-1:0]      mclk = {clk_xdma, clk_xdma, clk_ctrl};
    wire [32*NM-1:0]   mchk_err, mchk_txn;
    wire [32*NS-1:0]   schk_err, schk_txn;

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
        localparam integer DW = (i < 3) ? P_FW : 32;
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
            .s_axi_arvalid(sp_arvalid[i]), .s_axi_arready(sp_arready[i]),
            .s_axi_rid(sp_rid[i*MAXID +: MAXID]),
            .s_axi_rdata(sp_rdata[i*MAXW +: DW]),
            .s_axi_rresp(sp_rresp[i*2 +: 2]), .s_axi_rlast(sp_rlast[i]),
            .s_axi_rvalid(sp_rvalid[i]), .s_axi_rready(sp_rready[i])
        );
    end
    endgenerate

    // ------------------------------------------------------------ patterns
    function [31:0] pat32;
        input [AW-1:0] a;
        begin pat32 = {a[15:0], ~a[15:0]} ^ 32'hA5A5_0F0F; end
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

    // Many reads outstanding on ONE id. AXI4 requires same-id responses in
    // issue order, so collecting in order IS the ordering test.
    // SEPARATE loop variables per branch: a Verilog task is static, so a shared
    // `i` is one storage the issuer advances and the collector then reads.
    integer pi_i, pc_i, pc_b;

    task x_read_pipe;
        input [AW-1:0] base;
        input integer  count;
        input [7:0]    len;
        begin
            fork
                begin : issuer
                    // Address BEFORE any wait: holding arvalid across a negedge
                    // with a stale address gets the same AR accepted twice.
                    @(negedge clk_xdma); #TS;
                    for (pi_i = 0; pi_i < count; pi_i = pi_i + 1) begin
                        arid[1] = 4'd5; araddr[1] = base + pi_i*40'd1024;
                        arlen[1] = len; arsize[1] = 3'd6; arvld[1] = 1'b1;
                        #TS;
                        while (!arrdy[1]) begin @(negedge clk_xdma); #TS; end
                        @(negedge clk_xdma); #TS;
                    end
                    arvld[1] = 1'b0;
                end
                begin : collector
                    rrdy[1] = 1'b1;
                    for (pc_i = 0; pc_i < count; pc_i = pc_i + 1) begin
                        for (pc_b = 0; pc_b <= len; pc_b = pc_b + 1) begin
                            while (!rvld[1]) begin
                                @(negedge clk_xdma); #TS;
                            end
                            chk(rdata_v[MAXW +: MAXW],
                                pat512(base + pc_i*40'd1024, pc_b),
                                base + pc_i*40'd1024);
                            @(negedge clk_xdma); #TS;
                        end
                    end
                end
            join
        end
    endtask

    // ---------------------------------------------------------- stimulus
    localparam [AW-1:0] A_M0 = 40'h80_0000_0000, A_M1 = 40'h90_0000_0000;
    localparam [AW-1:0] A_M2 = 40'hA0_0000_0000;
    localparam [AW-1:0] A_DDR = 40'h00_0030_0000, A_MC = 40'h00_0081_0000;
    localparam [AW-1:0] A_W0 = 40'h00_0090_0000, A_W1 = 40'h00_0091_0000;
    localparam [AW-1:0] A_W2 = 40'h00_0092_0000, A_W3 = 40'h00_0093_0000;

    // Flits actually injected, counted at the station boundary. A DECERR that
    // returns correctly but still injects is the bug worth catching.
    integer flits = 0;
    always @(posedge bus_clk) begin
        if (!bus_rst) begin
            if (u_dut.q_valid[0] && u_dut.q_ready[0]) begin
                flits <= flits + 1;
            end
            if (u_dut.q_valid[1] && u_dut.q_ready[1]) begin
                flits <= flits + 1;
            end
            if (u_dut.q_valid[2] && u_dut.q_ready[2]) begin
                flits <= flits + 1;
            end
        end
    end

    integer n, nj, nx, nl, base_de, base_fl, rm, rk;
    integer seed;
    reg [31:0]   rl;
    reg [AW-1:0] ra;
    initial begin
        for (n = 0; n < NM; n = n + 1) begin
            awvld[n] = 0; wvld[n] = 0; brdy[n] = 0; arvld[n] = 0; rrdy[n] = 0;
            wlast[n] = 0; awlen[n] = 0; arlen[n] = 0;
        end
        repeat (40) @(posedge bus_clk);
        rstn = 1;
        repeat (60) @(posedge clk_ctrl);

        $display("--- phase 1: control plane, 32-bit manager, four clocks");
        j_write(A_DDR);  j_read(A_DDR, 2'b00);
        j_write(A_MC);   j_read(A_MC,  2'b00);
        j_write(A_W0);   j_read(A_W0,  2'b00);
        j_write(A_W1);   j_read(A_W1,  2'b00);
        j_write(A_W2);   j_read(A_W2,  2'b00);
        j_write(A_W3);   j_read(A_W3,  2'b00);

        $display("--- phase 2: 32-bit manager into 512-bit subordinates");
        j_write(A_M0 + 40'h04); j_read(A_M0 + 40'h04, 2'b00);
        j_write(A_M0 + 40'h28); j_read(A_M0 + 40'h28, 2'b00);
        j_write(A_M1 + 40'h7C); j_read(A_M1 + 40'h7C, 2'b00);
        j_write(A_M2 + 40'h40); j_read(A_M2 + 40'h40, 2'b00);

        $display("--- phase 3: 512-bit bursts");
        x_write(A_M0 + 40'h1000, 8'd0);  x_read(A_M0 + 40'h1000, 8'd0);
        x_write(A_M1 + 40'h1000, 8'd3);  x_read(A_M1 + 40'h1000, 8'd3);
        x_write(A_M2 + 40'h2000, 8'd15); x_read(A_M2 + 40'h2000, 8'd15);

        $display("--- phase 4: AXI-Lite manager, control only");
        l_write(A_W0 + 40'h10);  l_read(A_W0 + 40'h10);
        l_write(A_DDR + 40'h20); l_read(A_DDR + 40'h20);

        $display("--- phase 5: decode error terminates at the NMU");
        base_de = stat_decerr;
        base_fl = flits;
        j_read(40'h00_0500_0000, 2'b11);
        if (stat_decerr == base_de) begin
            errors = errors + 1;
            $display("%0t FAIL decerr counter did not advance", $time);
        end
        // The response alone proves nothing: a miss must inject NO flits.
        if (flits != base_fl) begin
            errors = errors + 1;
            $display("%0t FAIL decode miss injected %0d flits", $time,
                     flits - base_fl);
        end

        $display("--- phase 6: all three managers concurrent");
        fork
            begin : f_j
                for (nj = 0; nj < 8; nj = nj + 1) begin
                    j_write(A_MC + nj*4); j_read(A_MC + nj*4, 2'b00);
                end
            end
            begin : f_x
                for (nx = 0; nx < 6; nx = nx + 1) begin
                    x_write(A_M1 + 40'h4000 + nx*512, 8'd7);
                    x_read (A_M1 + 40'h4000 + nx*512, 8'd7);
                end
            end
            begin : f_l
                for (nl = 0; nl < 8; nl = nl + 1) begin
                    l_write(A_W3 + nl*4); l_read(A_W3 + nl*4);
                end
            end
        join

        $display("--- phase 7: multi-outstanding and same-id ordering");
        for (n = 0; n < 6; n = n + 1) begin
            x_write(A_M1 + 40'h8000 + n*40'd1024, 8'd3);
        end
        x_read_pipe(A_M1 + 40'h8000, 6, 8'd3);

        $display("--- phase 8: deadlock stress, two managers on one target");
        for (n = 0; n < 4; n = n + 1) begin
            x_write(A_M0 + 40'hC000 + n*40'd1024, 8'd7);
        end
        fork
            begin : s_x
                x_read_pipe(A_M0 + 40'hC000, 4, 8'd7);
            end
            begin : s_j
                for (nj = 0; nj < 12; nj = nj + 1) begin
                    j_write(A_M0 + 40'hE000 + nj*4);
                    j_read (A_M0 + 40'hE000 + nj*4, 2'b00);
                end
            end
            begin : s_l
                for (nl = 0; nl < 12; nl = nl + 1) begin
                    l_write(A_W2 + nl*4); l_read(A_W2 + nl*4);
                end
            end
        join

        $display("--- phase 9: reset mid-traffic, then recover");
        fork
            begin : r_traffic
                for (nx = 0; nx < 4; nx = nx + 1) begin
                    x_write(A_M2 + 40'h3000 + nx*40'd1024, 8'd7);
                end
            end
            begin : r_pulse
                repeat (300) @(posedge bus_clk);
                rstn = 0;
                repeat (40) @(posedge bus_clk);
                rstn = 1;
            end
        join_any
        disable r_traffic;
        rstn = 0;
        repeat (40) @(posedge bus_clk);
        rstn = 1;
        repeat (200) @(posedge clk_ctrl);
        // Recovery, not the interrupted traffic, is what matters here.
        for (n = 0; n < NM; n = n + 1) begin
            awvld[n] = 0; wvld[n] = 0; arvld[n] = 0; wlast[n] = 0;
        end
        repeat (40) @(posedge clk_ctrl);
        j_write(A_W1 + 40'h40); j_read(A_W1 + 40'h40, 2'b00);
        x_write(A_M1 + 40'hF000, 8'd3); x_read(A_M1 + 40'hF000, 8'd3);

        $display("--- phase 10: randomised traffic, each manager in its own map");
        seed = 32'h5EED_1234;
        for (n = 0; n < 24; n = n + 1) begin
            rm = {$random(seed)} % 3;
            rk = {$random(seed)} % 3;
            // Write then read the SAME address, so RAM index aliasing between
            // random picks cannot produce a false mismatch.
            if (rm == 0) begin
                case (rk)
                    0: ra = A_M0 + 40'h600 + (({$random(seed)} % 64) * 4);
                    1: ra = A_MC + (({$random(seed)} % 64) * 4);
                    default: ra = A_W0 + (({$random(seed)} % 64) * 4);
                endcase
                j_write(ra); j_read(ra, 2'b00);
            end else if (rm == 1) begin
                case (rk)
                    0: ra = A_M0 + 40'h10000 + (({$random(seed)} % 16) * 64);
                    1: ra = A_M1 + 40'h10000 + (({$random(seed)} % 16) * 64);
                    default: ra = A_M2 + 40'h10000 + (({$random(seed)} % 16) * 64);
                endcase
                rl = {$random(seed)} % 8;
                x_write(ra, rl[7:0]); x_read(ra, rl[7:0]);
            end else begin
                case (rk)
                    0: ra = A_W1 + (({$random(seed)} % 64) * 4);
                    1: ra = A_W2 + (({$random(seed)} % 64) * 4);
                    default: ra = A_DDR + (({$random(seed)} % 64) * 4);
                endcase
                l_write(ra); l_read(ra);
            end
        end

`ifdef SB_TIMEOUT
        $display("--- phase 11: hung subordinate contained by TIMEOUT");
        stall[5] = 1'b1;
        exp_b = 2'b10;
        j_write(A_W0 + 40'h80);
        exp_b = 2'b00;
        stall[5] = 1'b0;
        repeat (200) @(posedge clk_ctrl);
        j_write(A_W1 + 40'h80); j_read(A_W1 + 40'h80, 2'b00);
`endif

        repeat (200) @(posedge bus_clk);

        // Protocol violations are counted inside the monitors, not here.
        for (n = 0; n < NM; n = n + 1) begin
            errors = errors + mchk_err[n*32 +: 32];
        end
        for (n = 0; n < NS; n = n + 1) begin
            errors = errors + schk_err[n*32 +: 32];
        end

        // A monitor that saw nothing proves nothing.
        for (n = 0; n < NM; n = n + 1) begin
            if (mchk_txn[n*32 +: 32] == 0) begin
                errors = errors + 1;
                $display("FAIL manager monitor %0d observed no transactions", n);
            end
        end
        for (n = 0; n < NS; n = n + 1) begin
            if (schk_txn[n*32 +: 32] == 0) begin
                errors = errors + 1;
                $display("FAIL subordinate monitor %0d observed no transactions", n);
            end
        end
        $display("    monitors saw %0d/%0d/%0d manager transactions",
                 mchk_txn[0 +: 32], mchk_txn[32 +: 32], mchk_txn[64 +: 32]);

        if (errors == 0) begin
            $display("PASS  %0d checks", checks);
        end
        else begin
            $display("FAIL  %0d errors in %0d checks", errors, checks);
        end
        $finish;
    end

    initial begin
        #40_000_000;
        $display("FAIL timeout -- the fabric is wedged");
        $finish;
    end
endmodule

`default_nettype wire

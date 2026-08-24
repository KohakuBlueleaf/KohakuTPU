// sb_chain2 against five axi4_ram slaves, two behind the local station and
// three across the link. Proves the cross-station route and the return path.

// A remote access exercises what one station cannot: dport routing at the far
// hub, SRC_PASS carrying the originating manager, and link credit flow.

`timescale 1ns / 1ps
`default_nettype none

module sb_chain2_tb;
    localparam integer AW    = 40;
    localparam integer MAXW  = 512;
    localparam integer MAXID = 4;
    localparam integer NM    = 3;
    localparam integer NS    = 5;
    localparam real    TS    = 0.001;

    integer errors = 0;
    integer checks = 0;

    reg bus_clk = 0, clk_ctrl = 0, clk_xdma = 0, clk_mesh = 0, clk_ddr = 0;
    always begin
        #1.250 bus_clk  = ~bus_clk;
    end
    always begin
        #5.000 clk_ctrl = ~clk_ctrl;
    end
    always begin
        #2.000 clk_xdma = ~clk_xdma;
    end
    always begin
        #2.109 clk_mesh = ~clk_mesh;
    end
    always begin
        #1.667 clk_ddr  = ~clk_ddr;
    end

    reg rstn = 0;
    wire bus_rst = !rstn;

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

    // -d SB_CRED1 starves the link to one credit: correctness must not depend
    // on credit count, only throughput.
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

    sb_chain2 #(.CRED(P_CRED), .FW(P_FW)) u_dut (
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

    wire [NS-1:0] sclk = {clk_ctrl, clk_ctrl, clk_mesh, clk_ddr, clk_mesh};

    genvar i;
    generate
    for (i = 0; i < NS; i = i + 1) begin : g_ram
        localparam integer DW = ((i == 0) || (i == 2)) ? P_FW : 32;
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
            if (bresp_v[1:0] !== 2'b00) begin
                errors = errors + 1;
                $display("%0t FAIL jtag write @%h bresp %b", $time, a,
                         bresp_v[1:0]);
            end
            @(negedge clk_ctrl); #TS; brdy[0] = 1'b0;
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
            @(negedge clk_ctrl); #TS; rrdy[0] = 1'b0;
        end
    endtask

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
            @(negedge clk_xdma); #TS; brdy[1] = 1'b0;
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
            rrdy[1] = 1'b0;
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
            @(negedge clk_xdma); #TS; brdy[2] = 1'b0;
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
            @(negedge clk_xdma); #TS; rrdy[2] = 1'b0;
        end
    endtask

    // Free-running bus_clk counter: latency and throughput are only meaningful
    // in fabric cycles, since the ports run at four unrelated rates.
    integer cyc = 0;
    always @(posedge bus_clk) begin
        cyc <= cyc + 1;
    end

    integer t0, t1;

    task x_read_timed;
        input  [AW-1:0] a;
        input  [7:0]    len;
        output integer  cycles;
        integer         b;
        begin
            @(negedge clk_xdma); #TS;
            arid[1] = 4'd4; araddr[1] = a; arlen[1] = len;
            arsize[1] = 3'd6; arvld[1] = 1'b1; rrdy[1] = 1'b1;
            #TS;
            t0 = cyc;
            while (!arrdy[1]) begin @(negedge clk_xdma); #TS; end
            @(negedge clk_xdma); #TS; arvld[1] = 1'b0;
            for (b = 0; b <= len; b = b + 1) begin
                while (!rvld[1]) begin @(negedge clk_xdma); #TS; end
                chk(rdata_v[MAXW +: MAXW], pat512(a, b), a);
                @(negedge clk_xdma); #TS;
            end
            t1 = cyc;
            rrdy[1] = 1'b0;
            cycles = t1 - t0;
        end
    endtask

    localparam [AW-1:0] A_A0 = 40'h80_0000_0000, A_A1 = 40'h00_0081_0000;
    localparam [AW-1:0] A_B0 = 40'h90_0000_0000, A_B1 = 40'h00_0090_0000;
    localparam [AW-1:0] A_B2 = 40'h00_0091_0000;

    integer n, nj, nx, nl, base_de;
    initial begin
        for (n = 0; n < NM; n = n + 1) begin
            awvld[n] = 0; wvld[n] = 0; brdy[n] = 0; arvld[n] = 0; rrdy[n] = 0;
            wlast[n] = 0; awlen[n] = 0; arlen[n] = 0;
        end
        repeat (60) @(posedge bus_clk);
        rstn = 1;
        repeat (60) @(posedge clk_ctrl);

        $display("--- phase 1: local station A");
        j_write(A_A1); j_read(A_A1, 2'b00);
        j_write(A_A0 + 40'h04); j_read(A_A0 + 40'h04, 2'b00);

        $display("--- phase 2: ACROSS THE LINK to station B");
        j_write(A_B1); j_read(A_B1, 2'b00);
        j_write(A_B2); j_read(A_B2, 2'b00);
        j_write(A_B0 + 40'h08); j_read(A_B0 + 40'h08, 2'b00);
        j_write(A_B0 + 40'h44); j_read(A_B0 + 40'h44, 2'b00);

        $display("--- phase 3: 512-bit bursts, local then remote");
        x_write(A_A0 + 40'h1000, 8'd3);  x_read(A_A0 + 40'h1000, 8'd3);
        x_write(A_B0 + 40'h1000, 8'd7);  x_read(A_B0 + 40'h1000, 8'd7);
        x_write(A_B0 + 40'h2000, 8'd15); x_read(A_B0 + 40'h2000, 8'd15);

        $display("--- phase 4: AXI-Lite manager across the link");
        l_write(A_B1 + 40'h20); l_read(A_B1 + 40'h20);
        l_write(A_B2 + 40'h30); l_read(A_B2 + 40'h30);

        $display("--- phase 5: decode error still terminates at the NMU");
        base_de = stat_decerr;
        j_read(40'h00_0500_0000, 2'b11);
        if (stat_decerr == base_de) begin
            errors = errors + 1;
            $display("%0t FAIL decerr counter did not advance", $time);
        end

        $display("--- phase 6: three managers concurrent, mixed local/remote");
        fork
            begin : f_j
                for (nj = 0; nj < 8; nj = nj + 1) begin
                    j_write(A_B2 + nj*4); j_read(A_B2 + nj*4, 2'b00);
                end
            end
            begin : f_x
                for (nx = 0; nx < 6; nx = nx + 1) begin
                    x_write(A_B0 + 40'h4000 + nx*512, 8'd7);
                    x_read (A_B0 + 40'h4000 + nx*512, 8'd7);
                end
            end
            begin : f_l
                for (nl = 0; nl < 8; nl = nl + 1) begin
                    l_write(A_A1 + nl*4); l_read(A_A1 + nl*4);
                end
            end
        join

        $display("--- phase 7: cycles, local against across-the-link");
        x_write(A_A0 + 40'h6000, 8'd0);
        x_write(A_B0 + 40'h6000, 8'd0);
        x_write(A_A0 + 40'h7000, 8'd15);
        x_write(A_B0 + 40'h7000, 8'd15);
        x_read_timed(A_A0 + 40'h6000, 8'd0,  n);
        $display("    LAT local  1-beat  %0d bus cycles", n);
        x_read_timed(A_B0 + 40'h6000, 8'd0,  n);
        $display("    LAT remote 1-beat  %0d bus cycles", n);
        x_read_timed(A_A0 + 40'h7000, 8'd15, n);
        $display("    BW  local  16-beat %0d bus cycles", n);
        x_read_timed(A_B0 + 40'h7000, 8'd15, n);
        $display("    BW  remote 16-beat %0d bus cycles", n);

        repeat (400) @(posedge bus_clk);
        if (errors == 0) begin
            $display("PASS  %0d checks", checks);
        end
        else begin
            $display("FAIL  %0d errors in %0d checks", errors, checks);
        end
        $finish;
    end

    initial begin
        #4_000_000;
        $display("FAIL timeout -- the chain is wedged");
        $finish;
    end
endmodule

`default_nettype wire

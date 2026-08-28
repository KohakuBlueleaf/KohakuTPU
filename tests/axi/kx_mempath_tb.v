// Self-checking bench for the fused xbar-cache (kx_mempath), parameterised by the
// harness (-d TB_M -d TB_N -d TB_K -d TB_TWOCLK). Masters write/read across homes,
// backed by per-home axi4_ram. Checks write path, cold miss -> line fill, cross-home
// routing, multi-master arbitration, k x IO sub-word serve, and -- when TB_TWOCLK --
// homes on a second clock domain reached through the internal CDC.

`timescale 1ns/1ps
`default_nettype none

`ifndef TB_M
  `define TB_M 4
`endif
`ifndef TB_N
  `define TB_N 4
`endif
`ifndef TB_K
  `define TB_K 1
`endif
`ifndef TB_TWOCLK
  `define TB_TWOCLK 0
`endif
`ifndef TB_RSAMD
  `define TB_RSAMD 1
`endif
`ifndef TB_SETS
  `define TB_SETS 256
`endif
`ifndef TB_SET_W
  `define TB_SET_W 8
`endif
`ifndef TB_WSAMD
  `define TB_WSAMD 1
`endif

module kx_mempath_tb;
    localparam integer M=`TB_M, N=`TB_N, K=`TB_K, TWOCLK=`TB_TWOCLK;
    localparam integer RSAMD=`TB_RSAMD, WSAMD=`TB_WSAMD;
    localparam integer W=512, IDW_S=4, HOME_LSB=32, SETS=`TB_SETS, SET_W=`TB_SET_W;
    localparam integer AW=40, IDW=IDW_S+((M<=1)?1:$clog2(M)), STRB=W/8;
    // ship clock pattern: masters on the xbar clock (no CDC); with TWOCLK the
    // DRAM side sits on a second clock and every home crosses at its edge.
    localparam [M-1:0] MCDC = {M{1'b0}};
    localparam [N-1:0] HCDC = (TWOCLK!=0) ? {N{1'b1}} : {N{1'b0}};

    reg clk0 = 0, clk1 = 0;
    reg rstn = 0, rstn1 = 0;
    always #1.667 clk0 = ~clk0;                     // xbar/master clock ~ 300 MHz
    always #2.100 clk1 = ~clk1;                     // DRAM clock ~ 238 MHz
    wire clk = clk0;
    wire dclk = (TWOCLK!=0) ? clk1 : clk0;
    wire drstn = (TWOCLK!=0) ? rstn1 : rstn;

    reg  [M*IDW_S-1:0] s_awid, s_arid;
    reg  [M*AW-1:0]    s_awaddr, s_araddr;
    reg  [M*8-1:0]     s_awlen, s_arlen;
    reg  [M*3-1:0]     s_awsize, s_arsize;
    reg  [M*2-1:0]     s_awburst, s_arburst;
    reg  [M-1:0]       s_awvalid, s_arvalid, s_wvalid, s_wlast, s_bready, s_rready;
    reg  [M*W-1:0]     s_wdata;
    reg  [M*STRB-1:0]  s_wstrb;
    wire [M-1:0]       s_awready, s_wready, s_arready, s_bvalid, s_rvalid, s_rlast;
    wire [M*IDW_S-1:0] s_bid, s_rid;
    wire [M*2-1:0]     s_bresp, s_rresp;
    wire [M*W-1:0]     s_rdata;

    wire [N*IDW-1:0] d_awid, d_arid, d_bid, d_rid;
    wire [N*AW-1:0]  d_awaddr, d_araddr;
    wire [N*8-1:0]   d_awlen, d_arlen;
    wire [N*3-1:0]   d_awsize, d_arsize;
    wire [N*2-1:0]   d_awburst, d_arburst, d_bresp, d_rresp;
    wire [N-1:0]     d_awvalid, d_awready, d_wvalid, d_wready, d_wlast;
    wire [N-1:0]     d_bvalid, d_bready, d_arvalid, d_arready, d_rvalid, d_rready, d_rlast;
    wire [N*W-1:0]   d_wdata, d_rdata;
    wire [N*STRB-1:0] d_wstrb;

    kx_mempath_e #(.M(M), .N_HOME(N), .AW(AW), .W(W), .ID_W(IDW_S), .HOME_LSB(HOME_LSB),
                 .SETS(SETS), .SET_W(SET_W), .K(K), .RAM_STYLE("block"),
                 .MCDC(MCDC), .HCDC(HCDC), .RSAMD(RSAMD), .WSAMD(WSAMD)) dut (
        .clk(clk), .rstn(rstn),
        .m_clk({M{clk}}), .m_rstn({M{rstn}}),
        .h_clk({N{dclk}}), .h_rstn({N{drstn}}),
        .s_awid(s_awid), .s_awaddr(s_awaddr), .s_awlen(s_awlen), .s_awsize(s_awsize),
        .s_awburst(s_awburst), .s_awvalid(s_awvalid), .s_awready(s_awready),
        .s_wdata(s_wdata), .s_wstrb(s_wstrb), .s_wlast(s_wlast),
        .s_wvalid(s_wvalid), .s_wready(s_wready),
        .s_bid(s_bid), .s_bresp(s_bresp), .s_bvalid(s_bvalid), .s_bready(s_bready),
        .s_arid(s_arid), .s_araddr(s_araddr), .s_arlen(s_arlen), .s_arsize(s_arsize),
        .s_arburst(s_arburst), .s_arvalid(s_arvalid), .s_arready(s_arready),
        .s_rid(s_rid), .s_rdata(s_rdata), .s_rresp(s_rresp), .s_rlast(s_rlast),
        .s_rvalid(s_rvalid), .s_rready(s_rready),
        .d_awid(d_awid), .d_awaddr(d_awaddr), .d_awlen(d_awlen), .d_awsize(d_awsize),
        .d_awburst(d_awburst), .d_awvalid(d_awvalid), .d_awready(d_awready),
        .d_wdata(d_wdata), .d_wstrb(d_wstrb), .d_wlast(d_wlast),
        .d_wvalid(d_wvalid), .d_wready(d_wready),
        .d_bid(d_bid), .d_bresp(d_bresp), .d_bvalid(d_bvalid), .d_bready(d_bready),
        .d_arid(d_arid), .d_araddr(d_araddr), .d_arlen(d_arlen), .d_arsize(d_arsize),
        .d_arburst(d_arburst), .d_arvalid(d_arvalid), .d_arready(d_arready),
        .d_rid(d_rid), .d_rdata(d_rdata), .d_rresp(d_rresp), .d_rlast(d_rlast),
        .d_rvalid(d_rvalid), .d_rready(d_rready)
    );

    genvar g;
    generate for (g = 0; g < N; g = g + 1) begin : ram
        axi4_ram #(.DATA_WIDTH(W), .ADDR_WIDTH(AW), .ID_WIDTH(IDW), .DEPTH(4096)) u (
            .clk(dclk), .resetn(drstn),
            .s_axi_awid(d_awid[g*IDW +: IDW]), .s_axi_awaddr(d_awaddr[g*AW +: AW]),
            .s_axi_awlen(d_awlen[g*8 +: 8]), .s_axi_awsize(d_awsize[g*3 +: 3]),
            .s_axi_awburst(d_awburst[g*2 +: 2]), .s_axi_awvalid(d_awvalid[g]),
            .s_axi_awready(d_awready[g]),
            .s_axi_wdata(d_wdata[g*W +: W]), .s_axi_wstrb(d_wstrb[g*STRB +: STRB]),
            .s_axi_wlast(d_wlast[g]), .s_axi_wvalid(d_wvalid[g]), .s_axi_wready(d_wready[g]),
            .s_axi_bid(d_bid[g*IDW +: IDW]), .s_axi_bresp(d_bresp[g*2 +: 2]),
            .s_axi_bvalid(d_bvalid[g]), .s_axi_bready(d_bready[g]),
            .s_axi_arid(d_arid[g*IDW +: IDW]), .s_axi_araddr(d_araddr[g*AW +: AW]),
            .s_axi_arlen(d_arlen[g*8 +: 8]), .s_axi_arsize(d_arsize[g*3 +: 3]),
            .s_axi_arburst(d_arburst[g*2 +: 2]), .s_axi_arvalid(d_arvalid[g]),
            .s_axi_arready(d_arready[g]),
            .s_axi_rid(d_rid[g*IDW +: IDW]), .s_axi_rdata(d_rdata[g*W +: W]),
            .s_axi_rresp(d_rresp[g*2 +: 2]), .s_axi_rlast(d_rlast[g]),
            .s_axi_rvalid(d_rvalid[g]), .s_axi_rready(d_rready[g])
        );
    end endgenerate

    integer errors=0, checks=0;

    function [AW-1:0] haddr; input integer h; input [AW-1:0] off;
        begin haddr = off; haddr[HOME_LSB +: ((N<=1)?1:$clog2(N))] = h[((N<=1)?0:$clog2(N)-1):0]; end
    endfunction

    // burst write of nb beats, d + beat index in the low word; strobe per beat
    task wrn(input integer mi, input [AW-1:0] a, input [W-1:0] d, input integer nb, input [STRB-1:0] strb);
        integer b;
        begin
            @(negedge clk);
            s_awid[mi*IDW_S +: IDW_S]=1; s_awaddr[mi*AW +: AW]=a; s_awlen[mi*8 +: 8]=nb-1;
            s_awsize[mi*3 +: 3]=3'd6; s_awburst[mi*2 +: 2]=2'b01; s_awvalid[mi]=1;
            do @(posedge clk); while(!s_awready[mi]); @(negedge clk); s_awvalid[mi]=0;
            for (b = 0; b < nb; b = b + 1) begin
                s_wdata[mi*W +: W]=d+b; s_wstrb[mi*STRB +: STRB]=strb; s_wlast[mi]=(b==nb-1); s_wvalid[mi]=1;
                do @(posedge clk); while(!s_wready[mi]); @(negedge clk);
            end
            s_wvalid[mi]=0; s_wlast[mi]=0;
            s_bready[mi]=1; do @(posedge clk); while(!s_bvalid[mi]);
            if (s_bresp[mi*2 +: 2]!==2'b00) begin errors=errors+1; $display("  FAIL bresp m%0d @%h",mi,a); end
            @(negedge clk); s_bready[mi]=0;
        end
    endtask
    task wr1(input integer mi, input [AW-1:0] a, input [W-1:0] d);
        begin wrn(mi, a, d, 1, {STRB{1'b1}}); end
    endtask

    // burst read of nb beats; expects exp + beat index in each beat
    task rdn(input integer mi, input [AW-1:0] a, input [W-1:0] exp, input integer nb);
        integer b;
        begin
            @(negedge clk);
            s_arid[mi*IDW_S +: IDW_S]=2; s_araddr[mi*AW +: AW]=a; s_arlen[mi*8 +: 8]=nb-1;
            s_arsize[mi*3 +: 3]=3'd6; s_arburst[mi*2 +: 2]=2'b01; s_arvalid[mi]=1;
            do @(posedge clk); while(!s_arready[mi]); @(negedge clk); s_arvalid[mi]=0;
            s_rready[mi]=1;
            for (b = 0; b < nb; b = b + 1) begin
                do @(posedge clk); while(!s_rvalid[mi]);
                checks=checks+1;
                if (s_rdata[mi*W +: W]!==exp+b) begin
                    errors=errors+1;
                    $display("  FAIL rd m%0d @%h beat %0d got %h exp %h", mi, a, b, s_rdata[mi*W +: 64], exp[63:0]+b);
                end
                if ((b==nb-1) !== s_rlast[mi]) begin
                    errors=errors+1; $display("  FAIL rlast m%0d @%h beat %0d", mi, a, b);
                end
                @(negedge clk);
            end
            s_rready[mi]=0;
        end
    endtask
    task rd1(input integer mi, input [AW-1:0] a, input [W-1:0] exp);
        begin rdn(mi, a, exp, 1); end
    endtask

    integer p, mm;
    initial begin
        s_awvalid=0; s_wvalid=0; s_bready=0; s_arvalid=0; s_rready=0; s_wlast=0;
        s_awid=0; s_arid=0; s_awaddr=0; s_araddr=0; s_awlen=0; s_arlen=0;
        s_awsize=0; s_arsize=0; s_awburst=0; s_arburst=0; s_wdata=0; s_wstrb=0;
        repeat(4) @(posedge clk); rstn = 1'b1; rstn1 = 1'b1;
        repeat(SETS+80) @(posedge clk);          // let the flush finish

        // distinct data per home: write-through then read back (fill/hit)
        for (p=0;p<N;p=p+1) wr1(0, haddr(p,40'h200), {8{64'hC0DE_0000 + (p<<20)}});
        for (p=0;p<N;p=p+1) rd1(0, haddr(p,40'h200), {8{64'hC0DE_0000 + (p<<20)}});
        // adjacent sub-word within the same K-line must not alias (k x IO)
        if (K > 1) begin
            wr1(0, haddr(0,40'h200)+64, {8{64'hA5A5_1111}});
            rd1(0, haddr(0,40'h200)+64, {8{64'hA5A5_1111}});
            rd1(0, haddr(0,40'h200),    {8{64'hC0DE_0000}});
        end
        // second master reaches the highest home (owner routing + arbitration)
        rd1((M>1)?1:0, haddr(N-1,40'h200), {8{64'hC0DE_0000 + ((N-1)<<20)}});
        // overwrite home 0 and confirm (write-through re-fetch/re-alloc)
        wr1(0, haddr(0,40'h200), {8{64'hBEEF_0000}});
        rd1(0, haddr(0,40'h200), {8{64'hBEEF_0000}});
        // 4-beat burst write then burst read back (each beat distinct, rlast checked)
        wrn(0, haddr(1,40'h800), {8{64'h1000_0000}}, 4, {STRB{1'b1}});
        rdn(0, haddr(1,40'h800), {8{64'h1000_0000}}, 4);
        // partial-strobe write (low half only) must NOT leave a stale full line: a
        // read after it goes to DRAM, which has the merged data (beat 0 of the burst
        // above holds 1000_0000+0 in the untouched upper half)
        wrn(0, haddr(1,40'h800), {8{64'h2222_2222}}, 1, {{(STRB/2){1'b0}}, {(STRB/2){1'b1}}});
        rd1(0, haddr(1,40'h800), {{4{64'h1000_0000}}, {4{64'h2222_2222}}});
        // two masters on the SAME home back to back (arbiter rotation)
        if (M > 1) begin
            wr1(1, haddr(0,40'hC00), {8{64'hAAAA_0001}});
            wr1(0, haddr(0,40'hC40), {8{64'hAAAA_0000}});
            rd1(1, haddr(0,40'hC00), {8{64'hAAAA_0001}});
            rd1(0, haddr(0,40'hC40), {8{64'hAAAA_0000}});
        end

        repeat(40) @(posedge clk);
        if (errors==0) $display("  PASS -- kx_mempath M%0d N%0d K%0d twoclk%0d, %0d checks, 0 errors",
                                M, N, K, TWOCLK, checks);
        else           $display("  FAIL -- M%0d N%0d K%0d twoclk%0d: %0d errors / %0d checks",
                                M, N, K, TWOCLK, errors, checks);
        $finish;
    end
endmodule

`default_nettype wire

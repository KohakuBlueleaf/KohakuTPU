// Self-checking bench for the Xache (kx_xache), parameterised by the harness
// (-d TB_M -d TB_N -d TB_K -d TB_TWOCLK -d TB_RSAMD -d TB_WSAMD -d TB_SETS -d TB_SET_W
//  -d TB_ILV=<bit> -d TB_RDPIPE -d TB_RDQ -d TB_DLAT -d TB_PERF). Masters write/read
// across homes, backed by per-home axi4_ram (RD_LAT_CYC = TB_DLAT from AR to R).
// Checks the write path, cold miss -> fill, cross-home routing, multi-master
// arbitration, k x IO sub-word serve, homes on a second clock through the edge
// CDC, page distribution under an interleave, and -- with TB_RDQ > 1 -- several
// bursts outstanding per master, in order, under random rready stalls. TB_PERF
// adds cycle-counted streaming scenarios.

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
`ifndef TB_ILV
  `define TB_ILV 0
`endif
`ifndef TB_RDPIPE
  `define TB_RDPIPE 0
`endif
`ifndef TB_RDQ
  `define TB_RDQ 1
`endif
`ifndef TB_DLAT
  `define TB_DLAT 0
`endif
`ifndef TB_PERF
  `define TB_PERF 0
`endif

module kx_xache_tb;
    localparam integer M=`TB_M, N=`TB_N, K=`TB_K, TWOCLK=`TB_TWOCLK;
    localparam integer RSAMD=`TB_RSAMD, WSAMD=`TB_WSAMD;
    localparam integer RDPIPE=`TB_RDPIPE, RDQ=`TB_RDQ, DLAT=`TB_DLAT;
    localparam integer W=512, IDW_S=4, HOME_LSB=32, SETS=`TB_SETS, SET_W=`TB_SET_W;
    localparam integer AW=40, IDW=IDW_S+((M<=1)?1:$clog2(M)), STRB=W/8;
    localparam integer HIW = (N<=1) ? 1 : $clog2(N);
    // ship clock pattern: masters on the xbar clock (no CDC); with TWOCLK the
    // DRAM side sits on a second clock and every home crosses at its edge.
    localparam [M-1:0] MCDC = {M{1'b0}};
    localparam [N-1:0] HCDC = (TWOCLK!=0) ? {N{1'b1}} : {N{1'b0}};
    // interleave at 2^ILV bytes: rotate [ILV, HOME_LSB+HIW) down by HIW bits as
    // a chain of swaps (i, i+HIW), i = ILV .. HOME_LSB-1
    localparam integer ILV   = `TB_ILV;
    localparam integer NSWAP = (ILV != 0) ? (HOME_LSB - ILV) : 0;
    localparam integer NSW   = (NSWAP < 1) ? 1 : NSWAP;
    function [NSW*8-1:0] mk_swap; input integer base; integer i;
        begin mk_swap = 0; for (i = 0; i < NSW; i = i + 1) mk_swap[i*8 +: 8] = base + i; end
    endfunction
    localparam [NSW*8-1:0] SWAP_A = mk_swap(ILV);
    localparam [NSW*8-1:0] SWAP_B = mk_swap(ILV + HIW);

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

    kx_xache #(.M(M), .N_HOME(N), .AW(AW), .W(W), .ID_W(IDW_S), .HOME_LSB(HOME_LSB),
                 .SETS(SETS), .SET_W(SET_W), .K(K), .RAM_STYLE("block"),
                 .MCDC(MCDC), .HCDC(HCDC), .RSAMD(RSAMD), .WSAMD(WSAMD),
                 .NSWAP(NSWAP), .SWAP_A(SWAP_A), .SWAP_B(SWAP_B),
                 .RD_PIPE(RDPIPE), .RD_OUTQ(RDQ)) dut (
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

    // per-home DRAM transaction counters: the interleave check reads these
    integer n_ar [0:N-1];
    integer n_aw [0:N-1];
    genvar g;
    generate for (g = 0; g < N; g = g + 1) begin : ram
        axi4_ram #(.DATA_WIDTH(W), .ADDR_WIDTH(AW), .ID_WIDTH(IDW), .DEPTH(4096),
                   .RD_LAT_CYC(DLAT)) u (
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
        always @(posedge dclk) begin
            if (d_arvalid[g] && d_arready[g]) n_ar[g] <= n_ar[g] + 1;
            if (d_awvalid[g] && d_awready[g]) n_aw[g] <= n_aw[g] + 1;
        end
    end endgenerate

    // AXI rule: a master's RVALID must never fall while it waits for RREADY
    integer errors=0, checks=0;
    reg [M-1:0] rv_held;
    always @(posedge clk) begin
        rv_held <= s_rvalid & ~s_rready;
        if (rstn && |(rv_held & ~s_rvalid)) begin
            errors = errors + 1;
            $display("  FAIL rvalid dropped while waiting (mask %b) @%0t", rv_held & ~s_rvalid, $time);
        end
    end
    integer cyc = 0;
    always @(posedge clk) cyc <= cyc + 1;
    // a hang fails in seconds, not at the harness's ten-minute limit
    initial begin
        #4_000_000;
        $display("  FAIL -- kx_xache watchdog: no finish after 4 ms (%0d checks, %0d errors)", checks, errors);
        $finish;
    end

    // reference model of kx_perm: the pair list forward, and reversed for the
    // inverse. Test addresses are built in FABRIC space (home h, local offset)
    // and mapped back through the inverse, so every scenario targets the home
    // and the small local offset it names whatever the interleave is.
    function [AW-1:0] perm_fwd; input [AW-1:0] a; integer s, ia, ib; reg ta, tb;
        begin
            perm_fwd = a;
            for (s = 0; s < NSWAP; s = s + 1) begin
                ia = SWAP_A[s*8 +: 8]; ib = SWAP_B[s*8 +: 8];
                ta = perm_fwd[ia]; tb = perm_fwd[ib]; perm_fwd[ia] = tb; perm_fwd[ib] = ta;
            end
        end
    endfunction
    function [AW-1:0] perm_inv; input [AW-1:0] a; integer s, ia, ib; reg ta, tb;
        begin
            perm_inv = a;
            for (s = NSWAP - 1; s >= 0; s = s - 1) begin
                ia = SWAP_A[s*8 +: 8]; ib = SWAP_B[s*8 +: 8];
                ta = perm_inv[ia]; tb = perm_inv[ib]; perm_inv[ia] = tb; perm_inv[ib] = ta;
            end
        end
    endfunction
    function [AW-1:0] haddr; input integer h; input [AW-1:0] off; reg [AW-1:0] f;
        begin f = off; f[HOME_LSB +: HIW] = h[HIW-1:0]; haddr = perm_inv(f); end
    endfunction
    function integer home_of; input [AW-1:0] a; reg [AW-1:0] f;
        begin f = perm_fwd(a); home_of = f[HOME_LSB +: HIW]; end
    endfunction

    task clear_counts; integer h;
        begin for (h = 0; h < N; h = h + 1) begin n_ar[h] = 0; n_aw[h] = 0; end end
    endtask

    // burst write of nb beats, d + beat index in the low word; strobe per beat
    task automatic wrn(input integer mi, input [AW-1:0] a, input [W-1:0] d, input integer nb, input [STRB-1:0] strb);
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

    // the read side is split so that several bursts can be in flight: rd_issue
    // presents one AR, rd_collect consumes one burst's R beats (exp + beat index)
    task automatic rd_issue(input integer mi, input [AW-1:0] a, input integer nb);
        begin
            @(negedge clk);
            s_arid[mi*IDW_S +: IDW_S]=2; s_araddr[mi*AW +: AW]=a; s_arlen[mi*8 +: 8]=nb-1;
            s_arsize[mi*3 +: 3]=3'd6; s_arburst[mi*2 +: 2]=2'b01; s_arvalid[mi]=1;
            do @(posedge clk); while(!s_arready[mi]); @(negedge clk); s_arvalid[mi]=0;
        end
    endtask
    // stall: 0 = always ready; 1 = random 1-3 cycle drops of rready (replay path)
    task automatic rd_collect(input integer mi, input [AW-1:0] a, input [W-1:0] exp, input integer nb, input integer stall);
        integer b, k;
        begin
            for (b = 0; b < nb; b = b + 1) begin
                if (stall && (($urandom % 4) == 0)) begin
                    s_rready[mi]=0;
                    for (k = 0; k < 1 + ($urandom % 3); k = k + 1) @(negedge clk);
                end
                s_rready[mi]=1;
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
    task automatic rdn(input integer mi, input [AW-1:0] a, input [W-1:0] exp, input integer nb);
        begin rd_issue(mi, a, nb); rd_collect(mi, a, exp, nb, 0); end
    endtask
    task rd1(input integer mi, input [AW-1:0] a, input [W-1:0] exp);
        begin rdn(mi, a, exp, 1); end
    endtask

    // one master streams `pages` 4 KB pages from `base`, 64-beat bursts
    localparam integer PG = 4096;
    task automatic stream_wr(input integer mi, input [AW-1:0] base, input integer pages);
        integer p;
        begin for (p = 0; p < pages; p = p + 1)
            wrn(mi, base + p*PG, {8{64'hD000_0000 + (mi<<16) + p}}, 64, {STRB{1'b1}});
        end
    endtask
    // reads with as many bursts outstanding as the DUT accepts: the issuer runs
    // ahead of the collector, which takes the bursts back in order
    task automatic stream_rd(input integer mi, input [AW-1:0] base, input integer pages, input integer stall);
        integer pi, pc;
        begin
            fork
                for (pi = 0; pi < pages; pi = pi + 1) rd_issue(mi, base + pi*PG, 64);
                for (pc = 0; pc < pages; pc = pc + 1) rd_collect(mi, base + pc*PG, {8{64'hD000_0000 + (mi<<16) + pc}}, 64, stall);
            join
        end
    endtask

    integer p, mm, h, exp_n, t0, t1;
    integer exp_cnt [0:N-1];
    real gbps;
    initial begin
        s_awvalid=0; s_wvalid=0; s_bready=0; s_arvalid=0; s_rready=0; s_wlast=0;
        s_awid=0; s_arid=0; s_awaddr=0; s_araddr=0; s_awlen=0; s_arlen=0;
        s_awsize=0; s_arsize=0; s_awburst=0; s_arburst=0; s_wdata=0; s_wstrb=0;
        clear_counts;
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
        // a burst that starts mid-line and misses, then hits, then a partial-hit
        // burst (its tail written meanwhile): the streaming engine's fetch span
        wrn(0, haddr(2,40'h1000), {8{64'h3300_0000}}, 8, {STRB{1'b1}});
        rdn(0, haddr(2,40'h1000)+128, {8{64'h3300_0000}} + 2, 6);
        rdn(0, haddr(2,40'h1000)+128, {8{64'h3300_0000}} + 2, 6);
        wrn(0, haddr(2,40'h1000)+256, {8{64'h3400_0004}}, 4, {STRB{1'b1}});
        rdn(0, haddr(2,40'h1000), {8{64'h3300_0000}}, 4);
        rdn(0, haddr(2,40'h1000)+256, {8{64'h3400_0004}}, 4);
        // 64-beat bursts: miss stream then hit stream, under random rready stalls
        stream_wr(0, haddr(3,40'h0), 2);
        stream_rd(0, haddr(3,40'h0), 2, 1);
        stream_rd(0, haddr(3,40'h0), 2, 1);

        // 16 consecutive 4 KB pages from one master, counted per home on the
        // DRAM-side AW against the reference permutation: all on home 0 without
        // an interleave, spread with one. Then read back through it.
        repeat(20) @(posedge clk);
        clear_counts;
        for (h = 0; h < N; h = h + 1) exp_cnt[h] = 0;
        for (p = 0; p < 16; p = p + 1) begin
            wr1(0, p*PG + 40'h100, {8{64'h5EED_0000 + p}});
            exp_cnt[home_of(p*PG + 40'h100)] = exp_cnt[home_of(p*PG + 40'h100)] + 1;
        end
        repeat(20) @(posedge clk);
        for (h = 0; h < N; h = h + 1) begin
            checks = checks + 1;
            if (n_aw[h] !== exp_cnt[h]) begin
                errors = errors + 1;
                $display("  FAIL interleave: home %0d took %0d of 16 pages, expected %0d (ILV=%0d)", h, n_aw[h], exp_cnt[h], ILV);
            end
            if (ILV != 0 && N > 1 && (16 * PG) > (1 << ILV) && exp_cnt[h] == 16) begin
                errors = errors + 1;
                $display("  FAIL interleave: every page on home %0d (ILV=%0d): no spread", h, ILV);
            end
        end
        for (p = 0; p < 16; p = p + 1) rd1(0, p*PG + 40'h100, {8{64'h5EED_0000 + p}});

        // outstanding bursts: one master issues 8 pages ahead of its collector
        // (misses, then hits), then every master at once on distinct regions
        if (RDQ > 1) begin
            stream_wr(0, 40'h0, 8);
            stream_rd(0, 40'h0, 8, 1);
            stream_rd(0, 40'h0, 8, 0);
            fork
                if (M > 0) stream_wr(0, 40'h0 * 4 * PG, 4);
                if (M > 1) stream_wr(1, 40'h1 * 4 * PG, 4);
                if (M > 2) stream_wr(2, 40'h2 * 4 * PG, 4);
                if (M > 3) stream_wr(3, 40'h3 * 4 * PG, 4);
            join
            fork
                if (M > 0) stream_rd(0, 40'h0 * 4 * PG, 4, 1);
                if (M > 1) stream_rd(1, 40'h1 * 4 * PG, 4, 1);
                if (M > 2) stream_rd(2, 40'h2 * 4 * PG, 4, 1);
                if (M > 3) stream_rd(3, 40'h3 * 4 * PG, 4, 1);
            join
        end

        if (`TB_PERF) begin
            // ---- streaming, cycle-counted. Every scenario re-checks data. ----
            // 1 master, 16 pages (64 KB), writes then reads (misses), then reads again
            clear_counts; t0 = cyc;
            stream_wr(0, 40'h0, 16);
            t1 = cyc; gbps = (65536.0 / ((t1 - t0) * 3.333e-9)) / 1.0e9;
            $display("@@@ PERF wr_1m     cycles=%0d bytes=65536 GBps300=%0.2f homes_aw=%0d/%0d/%0d/%0d",
                     t1 - t0, gbps, n_aw[0], n_aw[(N>1)?1:0], n_aw[(N>2)?2:0], n_aw[(N>3)?3:0]);
            clear_counts; t0 = cyc;
            stream_rd(0, 40'h0, 16, 0);
            t1 = cyc; gbps = (65536.0 / ((t1 - t0) * 3.333e-9)) / 1.0e9;
            $display("@@@ PERF rd_1m     cycles=%0d bytes=65536 GBps300=%0.2f homes_ar=%0d/%0d/%0d/%0d",
                     t1 - t0, gbps, n_ar[0], n_ar[(N>1)?1:0], n_ar[(N>2)?2:0], n_ar[(N>3)?3:0]);
            clear_counts; t0 = cyc;
            stream_rd(0, 40'h0, 16, 0);
            t1 = cyc; gbps = (65536.0 / ((t1 - t0) * 3.333e-9)) / 1.0e9;
            $display("@@@ PERF rd_1m_re  cycles=%0d bytes=65536 GBps300=%0.2f homes_ar=%0d/%0d/%0d/%0d",
                     t1 - t0, gbps, n_ar[0], n_ar[(N>1)?1:0], n_ar[(N>2)?2:0], n_ar[(N>3)?3:0]);
            // 2 KB read twice: the second pass is all hits
            t0 = cyc; rdn(0, 40'h0, {8{64'hD000_0000}}, 32); t1 = cyc;
            $display("@@@ PERF rd_2k_a   cycles=%0d bytes=2048 GBps300=%0.2f", t1 - t0,
                     (2048.0 / ((t1 - t0) * 3.333e-9)) / 1.0e9);
            t0 = cyc; rdn(0, 40'h0, {8{64'hD000_0000}}, 32); t1 = cyc;
            $display("@@@ PERF rd_2k_hit cycles=%0d bytes=2048 GBps300=%0.2f", t1 - t0,
                     (2048.0 / ((t1 - t0) * 3.333e-9)) / 1.0e9);
            // up to 8 masters concurrently, 4 pages (16 KB) each, distinct regions
            clear_counts; t0 = cyc;
            fork
                if (M > 0) stream_wr(0, 40'h0 * 4 * PG, 4);
                if (M > 1) stream_wr(1, 40'h1 * 4 * PG, 4);
                if (M > 2) stream_wr(2, 40'h2 * 4 * PG, 4);
                if (M > 3) stream_wr(3, 40'h3 * 4 * PG, 4);
                if (M > 4) stream_wr(4, 40'h4 * 4 * PG, 4);
                if (M > 5) stream_wr(5, 40'h5 * 4 * PG, 4);
                if (M > 6) stream_wr(6, 40'h6 * 4 * PG, 4);
                if (M > 7) stream_wr(7, 40'h7 * 4 * PG, 4);
            join
            t1 = cyc; gbps = ((((M > 8) ? 8 : M) * 16384.0) / ((t1 - t0) * 3.333e-9)) / 1.0e9;
            $display("@@@ PERF wr_%0dm     cycles=%0d bytes=%0d GBps300=%0.2f homes_aw=%0d/%0d/%0d/%0d",
                     (M > 8) ? 8 : M, t1 - t0, ((M > 8) ? 8 : M) * 16384, gbps,
                     n_aw[0], n_aw[(N>1)?1:0], n_aw[(N>2)?2:0], n_aw[(N>3)?3:0]);
            clear_counts; t0 = cyc;
            fork
                if (M > 0) stream_rd(0, 40'h0 * 4 * PG, 4, 0);
                if (M > 1) stream_rd(1, 40'h1 * 4 * PG, 4, 0);
                if (M > 2) stream_rd(2, 40'h2 * 4 * PG, 4, 0);
                if (M > 3) stream_rd(3, 40'h3 * 4 * PG, 4, 0);
                if (M > 4) stream_rd(4, 40'h4 * 4 * PG, 4, 0);
                if (M > 5) stream_rd(5, 40'h5 * 4 * PG, 4, 0);
                if (M > 6) stream_rd(6, 40'h6 * 4 * PG, 4, 0);
                if (M > 7) stream_rd(7, 40'h7 * 4 * PG, 4, 0);
            join
            t1 = cyc; gbps = ((((M > 8) ? 8 : M) * 16384.0) / ((t1 - t0) * 3.333e-9)) / 1.0e9;
            $display("@@@ PERF rd_%0dm     cycles=%0d bytes=%0d GBps300=%0.2f homes_ar=%0d/%0d/%0d/%0d",
                     (M > 8) ? 8 : M, t1 - t0, ((M > 8) ? 8 : M) * 16384, gbps,
                     n_ar[0], n_ar[(N>1)?1:0], n_ar[(N>2)?2:0], n_ar[(N>3)?3:0]);
            // the same regions again: hits wherever the working set fitted the
            // arrays it was spread over, misses where a granularity piled it up
            clear_counts; t0 = cyc;
            fork
                if (M > 0) stream_rd(0, 40'h0 * 4 * PG, 4, 0);
                if (M > 1) stream_rd(1, 40'h1 * 4 * PG, 4, 0);
                if (M > 2) stream_rd(2, 40'h2 * 4 * PG, 4, 0);
                if (M > 3) stream_rd(3, 40'h3 * 4 * PG, 4, 0);
                if (M > 4) stream_rd(4, 40'h4 * 4 * PG, 4, 0);
                if (M > 5) stream_rd(5, 40'h5 * 4 * PG, 4, 0);
                if (M > 6) stream_rd(6, 40'h6 * 4 * PG, 4, 0);
                if (M > 7) stream_rd(7, 40'h7 * 4 * PG, 4, 0);
            join
            t1 = cyc; gbps = ((((M > 8) ? 8 : M) * 16384.0) / ((t1 - t0) * 3.333e-9)) / 1.0e9;
            $display("@@@ PERF rd_%0dm_re  cycles=%0d bytes=%0d GBps300=%0.2f homes_ar=%0d/%0d/%0d/%0d",
                     (M > 8) ? 8 : M, t1 - t0, ((M > 8) ? 8 : M) * 16384, gbps,
                     n_ar[0], n_ar[(N>1)?1:0], n_ar[(N>2)?2:0], n_ar[(N>3)?3:0]);
        end

        repeat(40) @(posedge clk);
        if (errors==0) $display("  PASS -- kx_xache M%0d N%0d K%0d twoclk%0d ilv%0d rdpipe%0d rdq%0d dlat%0d, %0d checks, 0 errors",
                                M, N, K, TWOCLK, ILV, RDPIPE, RDQ, DLAT, checks);
        else           $display("  FAIL -- M%0d N%0d K%0d twoclk%0d ilv%0d rdpipe%0d rdq%0d dlat%0d: %0d errors / %0d checks",
                                M, N, K, TWOCLK, ILV, RDPIPE, RDQ, DLAT, errors, checks);
        $finish;
    end
endmodule

`default_nettype wire

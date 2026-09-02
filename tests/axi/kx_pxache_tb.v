// Self-checking bench for the Kohaku Partitioned Xache (kx_pxache): the
// kx_xache bench body over the partitioned fabric. -d TB_P partitions
// (master m and home h in partition m % P and h % P), -d TB_RSTAG staggers
// the partitions' reset release by that many cycles each. Every read burst
// takes a fresh ID, so bursts outstanding to different homes never meet
// the per-ID rule. Prints the hit-burst latency from master 0 to every
// home so the per-hop cost is measured, and checks a remote home costs
// more than the local one.
// (-d TB_M -d TB_N -d TB_K -d TB_TWOCLK -d TB_SETS -d TB_SET_W -d TB_ILV
//  -d TB_DLAT -d TB_PERF as for kx_xache.)

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
`ifndef TB_SETS
  `define TB_SETS 256
`endif
`ifndef TB_SET_W
  `define TB_SET_W 8
`endif
`ifndef TB_ILV
  `define TB_ILV 0
`endif
`ifndef TB_DLAT
  `define TB_DLAT 0
`endif
`ifndef TB_PERF
  `define TB_PERF 0
`endif
`ifndef TB_P
  `define TB_P 1
`endif
`ifndef TB_RSTAG
  `define TB_RSTAG 0
`endif
`ifndef TB_BANKS
  `define TB_BANKS 8
`endif
`ifndef TB_RINGREG
  `define TB_RINGREG 0
`endif
`ifndef TB_WPREG
  `define TB_WPREG 0
`endif
`ifndef TB_ARRLAT
  `define TB_ARRLAT 0
`endif
`ifndef TB_HOPRX
  `define TB_HOPRX 0
`endif
`ifndef TB_TRUNK
  `define TB_TRUNK 0
`endif
`ifndef TB_KTS
  `define TB_KTS 0
`endif
`ifndef TB_SCODE
  `define TB_SCODE 1
`endif
// Memory tier, cumulative: 1 trunk rings, 2 + reorder buffer, 4 + DRAM read
// CDC, 5 + DRAM write CDC -- all to LUTRAM. 6 is the ship point: the three
// depth-16 classes to LUTRAM and the depth-256 reorder buffer to URAM, which
// costs no LUT. An integer, not a string: a string `define does not survive
// the shell.
`ifndef TB_RBB
`define TB_RBB 0
`endif
`ifndef TB_LRAM
`define TB_LRAM 0
`endif
`ifndef TB_PCLK
  `define TB_PCLK 0
`endif
`ifndef TB_PH1
  `define TB_PH1 1.810
`endif
`ifndef TB_PH2
  `define TB_PH2 1.523
`endif
`ifndef TB_PH3
  `define TB_PH3 1.951
`endif

module kx_pxache_tb;
    localparam integer M=`TB_M, N=`TB_N, K=`TB_K, TWOCLK=`TB_TWOCLK, P=`TB_P, RSTAG=`TB_RSTAG;
    localparam integer BANKS=`TB_BANKS;
    localparam integer RINGREG=`TB_RINGREG, WPREG=`TB_WPREG, ARRLAT=`TB_ARRLAT;
    localparam integer HOPRX=`TB_HOPRX;
    localparam integer TRUNK=`TB_TRUNK;
    localparam integer DLAT=`TB_DLAT;
    localparam integer W=512, IDW_S=4, HOME_LSB=32, SETS=`TB_SETS, SET_W=`TB_SET_W;
    localparam integer AW=40, IDW=IDW_S+((M<=1)?1:$clog2(M)), STRB=W/8;
    localparam integer HIW = (N<=1) ? 1 : $clog2(N);
    localparam integer PW  = (P<=1) ? 1 : $clog2(P);
    // The bench drives every master on clk, so at TB_PCLK 1 -- where partition p
    // runs on its own clock -- the master edge must be a real crossing. What
    // ships is MCDC 0 with node p and partition p on ONE clock; that the two
    // nets are the same is a wiring property, checked in 75_verify_bd, not here.
    localparam [M-1:0] MCDC = (`TB_PCLK != 0) ? {M{1'b1}} : {M{1'b0}};
    localparam [N-1:0] HCDC = (TWOCLK != 0 || `TB_PCLK != 0) ? {N{1'b1}} : {N{1'b0}};
    function [M*PW-1:0] mk_mp; input integer dummy; integer i;
        begin mk_mp = 0; for (i = 0; i < M; i = i + 1) begin mk_mp[i*PW +: PW] = i % P; end end
    endfunction
    function [N*PW-1:0] mk_hp; input integer dummy; integer i;
        begin mk_hp = 0; for (i = 0; i < N; i = i + 1) begin mk_hp[i*PW +: PW] = i % P; end end
    endfunction
    localparam [M*PW-1:0] MP = mk_mp(0);
    localparam [N*PW-1:0] HP = mk_hp(0);
    localparam integer ILV   = `TB_ILV;
    localparam integer NSWAP = (ILV != 0) ? (HOME_LSB - ILV) : 0;
    localparam integer NSW   = (NSWAP < 1) ? 1 : NSWAP;
    function [NSW*8-1:0] mk_swap; input integer base; integer i;
        begin mk_swap = 0; for (i = 0; i < NSW; i = i + 1) begin mk_swap[i*8 +: 8] = base + i; end end
    endfunction
    localparam [NSW*8-1:0] SWAP_A = mk_swap(ILV);
    localparam [NSW*8-1:0] SWAP_B = mk_swap(ILV + HIW);

    reg clk0 = 0, clk1 = 0;
    reg rstn = 0, rstn1 = 0;
    reg [P-1:0] rstn_p = 0;
    always #1.667 begin
        clk0 = ~clk0;
    end
    always #2.100 begin
        clk1 = ~clk1;
    end
    wire clk = clk0;
    wire dclk = (TWOCLK!=0) ? clk1 : clk0;
    wire drstn = (TWOCLK!=0) ? rstn1 : rstn;

    // TB_PCLK: one clock per partition, all DIFFERENT and none a multiple of
    // another, so every boundary trunk is a real crossing that drifts. The
    // masters and the bench stay on clk (partition 0's), as the AXI edges do.
    reg [P-1:0] pclk = 0;
    localparam real PHALF0 = 1.667, PHALF1 = `TB_PH1, PHALF2 = `TB_PH2, PHALF3 = `TB_PH3;
    always begin
        #PHALF0 pclk[0] = ~pclk[0];
    end
    generate
        if (P > 1) begin : g_pc1 always #PHALF1 begin pclk[1] = ~pclk[1]; end end
        if (P > 2) begin : g_pc2 always #PHALF2 begin pclk[2] = ~pclk[2]; end end
        if (P > 3) begin : g_pc3 always #PHALF3 begin pclk[3] = ~pclk[3]; end end
    endgenerate
    // Partition 0 IS the bench clock: the bench drives master 0's AXI on clk0
    // with no edge CDC, so a separate same-rate clock there is a race, not a
    // crossing. Partitions 1..3 are the crossings under test.
    wire [P-1:0] clk_p_w = (`TB_PCLK != 0) ? {pclk[P-1:1], clk0} : {P{clk0}};

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

    kx_pxache #(.P(P), .M(M), .N_HOME(N), .MP(MP), .HP(HP), .AW(AW), .W(W), .ID_W(IDW_S),
                .HOME_LSB(HOME_LSB), .SETS(SETS), .SET_W(SET_W), .K(K), .RAM_STYLE("block"),
                .BANKS(BANKS), .RING_WR_REG(RINGREG), .ARR_WP_REG(WPREG),
                .ARR_LAT(ARRLAT), .HOP_RXREG(HOPRX), .BND_TRUNK(TRUNK),
                .BND_KTS(`TB_KTS), .BND_SCODE(`TB_SCODE), .PCLK(`TB_PCLK),
                .MEM_TRUNK((`TB_LRAM >= 1) ? "distributed" : "block"),
                .MEM_RB   ((`TB_LRAM == 6) ? "ultra"
                           : (`TB_LRAM >= 2) ? "distributed" : "block"),
                .MEM_HRD  ((`TB_LRAM >= 4) ? "distributed" : "block"),
                .MEM_HWR  ((`TB_LRAM >= 5) ? "distributed" : "block"),
                .RB_BEATS(`TB_RBB),
                .MCDC(MCDC), .HCDC(HCDC), .NSWAP(NSWAP), .SWAP_A(SWAP_A), .SWAP_B(SWAP_B)) dut (
        .clk(clk), .clk_p(clk_p_w), .rstn_p(rstn_p),
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
            if (d_arvalid[g] && d_arready[g]) begin n_ar[g] <= n_ar[g] + 1; end
            if (d_awvalid[g] && d_awready[g]) begin n_aw[g] <= n_aw[g] + 1; end
        end
    end endgenerate

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
    always @(posedge clk) begin cyc <= cyc + 1; end
    integer wm;
    initial begin
        #4_000_000;
        $display("  FAIL -- kx_pxache watchdog: no finish after 4 ms (%0d checks, %0d errors)", checks, errors);
        for (wm = 0; wm < M; wm = wm + 1) begin
            $display("  m%0d ar v/r %b/%b aw v/r %b/%b w v/r %b/%b r v/r %b/%b b v/r %b/%b  dut ar v/r %b/%b rd_ok %b wh_v %b rbusy %b",
                     wm, s_arvalid[wm], s_arready[wm], s_awvalid[wm], s_awready[wm], s_wvalid[wm], s_wready[wm],
                     s_rvalid[wm], s_rready[wm], s_bvalid[wm], s_bready[wm],
                     dut.x_arvalid[wm], dut.x_arready[wm], 1'b0, 1'b0, 1'b0);
        end
        for (wm = 0; wm < N; wm = wm + 1) begin
            $display("  h%0d slots ar_v %b ar_r %b aw_v %b aw_r %b w_v %b w_r %b  r_val %b r_rdy %b b_val %b b_rdy %b  d ar v/r %b/%b r v/r %b/%b",
                     wm, dut.hq_ar_v[wm], dut.hq_ar_r[wm], dut.hq_aw_v[wm], dut.hq_aw_r[wm], dut.hq_w_v[wm], dut.hq_w_r[wm],
                     dut.r_val_h[wm], dut.r_rdy_h[wm], dut.b_val_h[wm], dut.b_rdy_h[wm],
                     d_arvalid[wm], d_arready[wm], d_rvalid[wm], d_rready[wm]);
        end
        $finish;
    end

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

    // a burst whose beats alternate byte-lane halves: even beats write the low
    // half, odd beats the high half, so a strb changes inside one open burst
    task automatic wrn_alt(input integer mi, input [AW-1:0] a, input [W-1:0] d, input integer nb);
        integer b;
        begin
            @(negedge clk);
            s_awid[mi*IDW_S +: IDW_S]=1; s_awaddr[mi*AW +: AW]=a; s_awlen[mi*8 +: 8]=nb-1;
            s_awsize[mi*3 +: 3]=3'd6; s_awburst[mi*2 +: 2]=2'b01; s_awvalid[mi]=1;
            do @(posedge clk); while(!s_awready[mi]); @(negedge clk); s_awvalid[mi]=0;
            for (b = 0; b < nb; b = b + 1) begin
                s_wdata[mi*W +: W]=d+b;
                s_wstrb[mi*STRB +: STRB] = (b % 2 == 0)
                    ? {{(STRB/2){1'b0}}, {(STRB/2){1'b1}}}
                    : {{(STRB/2){1'b1}}, {(STRB/2){1'b0}}};
                s_wlast[mi]=(b==nb-1); s_wvalid[mi]=1;
                do @(posedge clk); while(!s_wready[mi]); @(negedge clk);
            end
            s_wvalid[mi]=0; s_wlast[mi]=0;
            s_bready[mi]=1; do @(posedge clk); while(!s_bvalid[mi]);
            if (s_bresp[mi*2 +: 2]!==2'b00) begin errors=errors+1; $display("  FAIL bresp m%0d @%h",mi,a); end
            @(negedge clk); s_bready[mi]=0;
        end
    endtask

    // The collector matches beats BY ID against a queue of expected bursts per
    // (master, ID): a fresh ID per burst by default, so bursts to different
    // homes may complete in any order, and one ID streaming across the homes,
    // which must come back in issue order.
    localparam integer NIDS = 1 << IDW_S;
    localparam integer EXQ  = 16;
    integer id_ctr [0:M-1];
    reg  [W-1:0]  ex_base [0:M-1][0:NIDS-1][0:EXQ-1];
    integer       ex_nb   [0:M-1][0:NIDS-1][0:EXQ-1];
    reg  [AW-1:0] ex_a    [0:M-1][0:NIDS-1][0:EXQ-1];
    integer       ex_hd   [0:M-1][0:NIDS-1];
    integer       ex_tl   [0:M-1][0:NIDS-1];
    integer       ex_beat [0:M-1][0:NIDS-1];
    integer ic, ic2;
    initial for (ic = 0; ic < M; ic = ic + 1) begin
        id_ctr[ic] = 0;
        for (ic2 = 0; ic2 < NIDS; ic2 = ic2 + 1) begin
            ex_hd[ic][ic2] = 0; ex_tl[ic][ic2] = 0; ex_beat[ic][ic2] = 0;
        end
    end
    task automatic rd_issue_id(input integer mi, input [AW-1:0] a, input [W-1:0] exp, input integer nb, input integer id);
        integer q;
        begin
            @(negedge clk);
            if (ex_tl[mi][id] - ex_hd[mi][id] >= EXQ) begin
                errors=errors+1; $display("  FAIL rd m%0d: ID %0d has %0d bursts queued", mi, id, EXQ);
            end
            q = ex_tl[mi][id] % EXQ;
            ex_base[mi][id][q] = exp; ex_nb[mi][id][q] = nb; ex_a[mi][id][q] = a;
            ex_tl[mi][id] = ex_tl[mi][id] + 1;
            s_arid[mi*IDW_S +: IDW_S]=id;
            s_araddr[mi*AW +: AW]=a; s_arlen[mi*8 +: 8]=nb-1;
            s_arsize[mi*3 +: 3]=3'd6; s_arburst[mi*2 +: 2]=2'b01; s_arvalid[mi]=1;
            do @(posedge clk); while(!s_arready[mi]); @(negedge clk); s_arvalid[mi]=0;
        end
    endtask
    task automatic rd_issue(input integer mi, input [AW-1:0] a, input [W-1:0] exp, input integer nb);
        integer id;
        begin
            id = id_ctr[mi]; id_ctr[mi] = (id_ctr[mi] + 1) % NIDS;
            rd_issue_id(mi, a, exp, nb, id);
        end
    endtask
    // consume `nbursts` whole bursts in whatever order the IDs arrive
    task automatic rd_collect(input integer mi, input integer nbursts, input integer stall);
        integer done, k, id, b, q;
        begin
            done = 0;
            while (done < nbursts) begin
                if (stall && (($urandom % 4) == 0)) begin
                    s_rready[mi]=0;
                    for (k = 0; k < 1 + ($urandom % 3); k = k + 1) begin @(negedge clk); end
                end
                s_rready[mi]=1;
                do @(posedge clk); while(!s_rvalid[mi]);
                id = s_rid[mi*IDW_S +: IDW_S];
                b  = ex_beat[mi][id];
                q  = ex_hd[mi][id] % EXQ;
                checks=checks+1;
                if (ex_hd[mi][id] == ex_tl[mi][id]) begin
                    errors=errors+1; $display("  FAIL rd m%0d: beat for ID %0d with nothing outstanding", mi, id);
                end else begin
                    if (s_rdata[mi*W +: W]!==ex_base[mi][id][q]+b) begin
                        errors=errors+1;
                        $display("  FAIL rd m%0d @%h beat %0d got %h exp %h", mi, ex_a[mi][id][q], b, s_rdata[mi*W +: 64], ex_base[mi][id][q][63:0]+b);
                    end
                    if ((b==ex_nb[mi][id][q]-1) !== s_rlast[mi]) begin
                        errors=errors+1; $display("  FAIL rlast m%0d @%h beat %0d", mi, ex_a[mi][id][q], b);
                    end
                    ex_beat[mi][id] = b + 1;
                    if (b == ex_nb[mi][id][q]-1) begin
                        ex_beat[mi][id] = 0; ex_hd[mi][id] = ex_hd[mi][id] + 1; done = done + 1;
                    end
                end
                @(negedge clk);
            end
            s_rready[mi]=0;
        end
    endtask
    task automatic rdn(input integer mi, input [AW-1:0] a, input [W-1:0] exp, input integer nb);
        begin rd_issue(mi, a, exp, nb); rd_collect(mi, 1, 0); end
    endtask
    task rd1(input integer mi, input [AW-1:0] a, input [W-1:0] exp);
        begin rdn(mi, a, exp, 1); end
    endtask

    // A stream moves whole 4 KB pages; the DUT's read slot (TB_RBB beats, a
    // page at 0) bounds one burst, so a page goes as CHK bursts of RBBT beats
    // carrying the same beat-indexed data as the one 64-beat burst would.
    localparam integer PG   = 4096;
    localparam integer RBBT = (`TB_RBB > 0 && `TB_RBB < 64) ? `TB_RBB : 64;
    localparam integer CHK  = 64 / RBBT;
    localparam integer LB   = (RBBT < 32) ? RBBT : 32;   // the latency burst
    task automatic stream_wr(input integer mi, input [AW-1:0] base, input integer pages);
        integer p, c;
        begin
            for (p = 0; p < pages; p = p + 1) begin
                for (c = 0; c < CHK; c = c + 1) begin
                    wrn(mi, base + p*PG + c*RBBT*STRB, {8{64'hD000_0000 + (mi<<16) + p}} + c*RBBT, RBBT, {STRB{1'b1}});
                end
            end
        end
    endtask
    task automatic stream_rd(input integer mi, input [AW-1:0] base, input integer pages, input integer stall);
        integer pi, c;
        begin
            fork
                for (pi = 0; pi < pages; pi = pi + 1) begin
                    for (c = 0; c < CHK; c = c + 1) begin
                        rd_issue(mi, base + pi*PG + c*RBBT*STRB, {8{64'hD000_0000 + (mi<<16) + pi}} + c*RBBT, RBBT);
                    end
                end
                rd_collect(mi, pages*CHK, stall);
            join
        end
    endtask
    task automatic stream_rd_id(input integer mi, input [AW-1:0] base, input integer pages, input integer stall, input integer id);
        integer pi, c;
        begin
            fork
                for (pi = 0; pi < pages; pi = pi + 1) begin
                    for (c = 0; c < CHK; c = c + 1) begin
                        rd_issue_id(mi, base + pi*PG + c*RBBT*STRB, {8{64'hD000_0000 + (mi<<16) + pi}} + c*RBBT, RBBT, id);
                    end
                end
                rd_collect(mi, pages*CHK, stall);
            join
        end
    endtask

    integer p, mm, h, exp_n, t0, t1, lat_loc, lat_h;
    integer exp_cnt [0:N-1];
    real gbps;
    initial begin
        s_awvalid=0; s_wvalid=0; s_bready=0; s_arvalid=0; s_rready=0; s_wlast=0;
        s_awid=0; s_arid=0; s_awaddr=0; s_araddr=0; s_awlen=0; s_arlen=0;
        s_awsize=0; s_arsize=0; s_awburst=0; s_arburst=0; s_wdata=0; s_wstrb=0;
        clear_counts;
        repeat(4) @(posedge clk); rstn = 1'b1; rstn1 = 1'b1;
        // partition resets: together, or each RSTAG cycles after the previous
        for (p = 0; p < P; p = p + 1) begin
            rstn_p[p] = 1'b1;
            repeat (RSTAG) @(posedge clk);
        end
        repeat(SETS+80) @(posedge clk);

        for (p=0;p<N;p=p+1) begin wr1(0, haddr(p,40'h200), {8{64'hC0DE_0000 + (p<<20)}}); end
        for (p=0;p<N;p=p+1) begin rd1(0, haddr(p,40'h200), {8{64'hC0DE_0000 + (p<<20)}}); end
        if (K > 1) begin
            wr1(0, haddr(0,40'h200)+64, {8{64'hA5A5_1111}});
            rd1(0, haddr(0,40'h200)+64, {8{64'hA5A5_1111}});
            rd1(0, haddr(0,40'h200),    {8{64'hC0DE_0000}});
        end
        rd1((M>1)?1:0, haddr(N-1,40'h200), {8{64'hC0DE_0000 + ((N-1)<<20)}});
        wr1(0, haddr(0,40'h200), {8{64'hBEEF_0000}});
        rd1(0, haddr(0,40'h200), {8{64'hBEEF_0000}});
        wrn(0, haddr(1,40'h800), {8{64'h1000_0000}}, 4, {STRB{1'b1}});
        rdn(0, haddr(1,40'h800), {8{64'h1000_0000}}, 4);
        wrn(0, haddr(1,40'h800), {8{64'h2222_2222}}, 1, {{(STRB/2){1'b0}}, {(STRB/2){1'b1}}});
        rd1(0, haddr(1,40'h800), {{4{64'h1000_0000}}, {4{64'h2222_2222}}});
        // partial strb into a home of the master's own partition
        wr1(0, haddr(0,40'h880), {8{64'h4000_0000}});
        wrn(0, haddr(0,40'h880), {8{64'h4444_4444}}, 1, {{(STRB/2){1'b0}}, {(STRB/2){1'b1}}});
        rd1(0, haddr(0,40'h880), {{4{64'h4000_0000}}, {4{64'h4444_4444}}});
        // a remote burst whose byte lanes change on every beat
        wrn(0, haddr(1,40'h900), {8{64'h6100_0000}}, 4, {STRB{1'b1}});
        wrn_alt(0, haddr(1,40'h900), {8{64'h6200_0000}}, 4);
        for (p = 0; p < 4; p = p + 1) begin
            if (p % 2 == 0) begin
                rd1(0, haddr(1,40'h900) + p*64,
                    {{4{64'h6100_0000}}, {3{64'h6200_0000}}, 64'h6200_0000 + p});
            end else begin
                rd1(0, haddr(1,40'h900) + p*64,
                    {{4{64'h6200_0000}}, {3{64'h6100_0000}}, 64'h6100_0000 + p});
            end
        end
        if (M > 1) begin
            wr1(1, haddr(0,40'hC00), {8{64'hAAAA_0001}});
            wr1(0, haddr(0,40'hC40), {8{64'hAAAA_0000}});
            rd1(1, haddr(0,40'hC00), {8{64'hAAAA_0001}});
            rd1(0, haddr(0,40'hC40), {8{64'hAAAA_0000}});
        end
        wrn(0, haddr(2,40'h1000), {8{64'h3300_0000}}, 8, {STRB{1'b1}});
        rdn(0, haddr(2,40'h1000)+128, {8{64'h3300_0000}} + 2, 6);
        rdn(0, haddr(2,40'h1000)+128, {8{64'h3300_0000}} + 2, 6);
        wrn(0, haddr(2,40'h1000)+256, {8{64'h3400_0004}}, 4, {STRB{1'b1}});
        rdn(0, haddr(2,40'h1000), {8{64'h3300_0000}}, 4);
        rdn(0, haddr(2,40'h1000)+256, {8{64'h3400_0004}}, 4);
        stream_wr(0, haddr(3,40'h0), 2);
        stream_rd(0, haddr(3,40'h0), 2, 1);
        stream_rd(0, haddr(3,40'h0), 2, 1);

        // every master to every home, all at once (both lane directions live)
        fork
            if (M > 0) begin for (h = 0; h < N; h = h + 1) begin wr1(0, haddr(h, 40'h2000), {8{64'h7700_0000 + h}}); rd1(0, haddr(h, 40'h2000), {8{64'h7700_0000 + h}}); end end
            if (M > 1) begin for (mm = 0; mm < N; mm = mm + 1) begin wr1(1, haddr(N-1-mm, 40'h2100), {8{64'h7701_0000 + mm}}); rd1(1, haddr(N-1-mm, 40'h2100), {8{64'h7701_0000 + mm}}); end end
            if (M > 2) begin for (p = 0; p < N; p = p + 1) begin wr1(2, haddr((p+1)%N, 40'h2200), {8{64'h7702_0000 + p}}); rd1(2, haddr((p+1)%N, 40'h2200), {8{64'h7702_0000 + p}}); end end
            if (M > 3) begin for (exp_n = 0; exp_n < N; exp_n = exp_n + 1) begin wr1(3, haddr((exp_n+2)%N, 40'h2300), {8{64'h7703_0000 + exp_n}}); rd1(3, haddr((exp_n+2)%N, 40'h2300), {8{64'h7703_0000 + exp_n}}); end end
        join

        repeat(20) @(posedge clk);
        clear_counts;
        for (h = 0; h < N; h = h + 1) begin exp_cnt[h] = 0; end
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
        end
        for (p = 0; p < 16; p = p + 1) begin rd1(0, p*PG + 40'h100, {8{64'h5EED_0000 + p}}); end

        // outstanding bursts: 8 pages issued ahead, then every master at once
        stream_wr(0, 40'h0, 8);
        stream_rd(0, 40'h0, 8, 1);
        stream_rd(0, 40'h0, 8, 0);
        fork
            if (M > 0) begin stream_wr(0, 40'h0 * 4 * PG, 4); end
            if (M > 1) begin stream_wr(1, 40'h1 * 4 * PG, 4); end
            if (M > 2) begin stream_wr(2, 40'h2 * 4 * PG, 4); end
            if (M > 3) begin stream_wr(3, 40'h3 * 4 * PG, 4); end
        join
        fork
            if (M > 0) begin stream_rd(0, 40'h0 * 4 * PG, 4, 1); end
            if (M > 1) begin stream_rd(1, 40'h1 * 4 * PG, 4, 1); end
            if (M > 2) begin stream_rd(2, 40'h2 * 4 * PG, 4, 1); end
            if (M > 3) begin stream_rd(3, 40'h3 * 4 * PG, 4, 1); end
        join
        // one ID across the interleaved homes, issued ahead: back in issue order
        stream_rd_id(0, 40'h0, 4, 1, 5);
        fork
            if (M > 0) begin stream_rd_id(0, 40'h0 * 4 * PG, 4, 0, 6); end
            if (M > 1) begin stream_rd_id(1, 40'h1 * 4 * PG, 4, 0, 6); end
            if (M > 2) begin stream_rd_id(2, 40'h2 * 4 * PG, 4, 0, 6); end
            if (M > 3) begin stream_rd_id(3, 40'h3 * 4 * PG, 4, 0, 6); end
        join

        // hit-burst latency from master 0 to every home: 32 beats, second pass
        lat_loc = -1;
        for (h = 0; h < N; h = h + 1) begin
            wrn(0, haddr(h, 40'h3000), {8{64'h9900_0000 + h}}, LB, {STRB{1'b1}});
            rdn(0, haddr(h, 40'h3000), {8{64'h9900_0000 + h}}, LB);
            t0 = cyc; rdn(0, haddr(h, 40'h3000), {8{64'h9900_0000 + h}}, LB); t1 = cyc;
            lat_h = t1 - t0;
            $display("  @@@ LAT m0->h%0d partition %0d->%0d hit32 cycles=%0d", h, MP[0 +: PW], HP[h*PW +: PW], lat_h);
            if (HP[h*PW +: PW] == MP[0 +: PW]) begin lat_loc = lat_h; end
        end
        if (P > 1) begin
            for (h = 0; h < N; h = h + 1) begin
                if (HP[h*PW +: PW] != MP[0 +: PW]) begin
                    wrn(0, haddr(h, 40'h3000), {8{64'h9900_0000 + h}}, LB, {STRB{1'b1}});
                    t0 = cyc; rdn(0, haddr(h, 40'h3000), {8{64'h9900_0000 + h}}, LB); t1 = cyc;
                    checks = checks + 1;
                    if (lat_loc >= 0 && (t1 - t0) <= lat_loc) begin
                        errors = errors + 1;
                        $display("  FAIL latency: remote home %0d (%0d) not above the local one (%0d)", h, t1 - t0, lat_loc);
                    end
                end
            end
        end

        if (`TB_PERF) begin
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
            t0 = cyc; rdn(0, 40'h0, {8{64'hD000_0000}}, LB); t1 = cyc;
            $display("@@@ PERF rd_2k_a   cycles=%0d bytes=2048 GBps300=%0.2f", t1 - t0,
                     (2048.0 / ((t1 - t0) * 3.333e-9)) / 1.0e9);
            t0 = cyc; rdn(0, 40'h0, {8{64'hD000_0000}}, LB); t1 = cyc;
            $display("@@@ PERF rd_2k_hit cycles=%0d bytes=2048 GBps300=%0.2f", t1 - t0,
                     (2048.0 / ((t1 - t0) * 3.333e-9)) / 1.0e9);
            clear_counts; t0 = cyc;
            fork
                if (M > 0) begin stream_wr(0, 40'h0 * 4 * PG, 4); end
                if (M > 1) begin stream_wr(1, 40'h1 * 4 * PG, 4); end
                if (M > 2) begin stream_wr(2, 40'h2 * 4 * PG, 4); end
                if (M > 3) begin stream_wr(3, 40'h3 * 4 * PG, 4); end
                if (M > 4) begin stream_wr(4, 40'h4 * 4 * PG, 4); end
                if (M > 5) begin stream_wr(5, 40'h5 * 4 * PG, 4); end
                if (M > 6) begin stream_wr(6, 40'h6 * 4 * PG, 4); end
                if (M > 7) begin stream_wr(7, 40'h7 * 4 * PG, 4); end
            join
            t1 = cyc; gbps = ((((M > 8) ? 8 : M) * 16384.0) / ((t1 - t0) * 3.333e-9)) / 1.0e9;
            $display("@@@ PERF wr_%0dm     cycles=%0d bytes=%0d GBps300=%0.2f homes_aw=%0d/%0d/%0d/%0d",
                     (M > 8) ? 8 : M, t1 - t0, ((M > 8) ? 8 : M) * 16384, gbps,
                     n_aw[0], n_aw[(N>1)?1:0], n_aw[(N>2)?2:0], n_aw[(N>3)?3:0]);
            clear_counts; t0 = cyc;
            fork
                if (M > 0) begin stream_rd(0, 40'h0 * 4 * PG, 4, 0); end
                if (M > 1) begin stream_rd(1, 40'h1 * 4 * PG, 4, 0); end
                if (M > 2) begin stream_rd(2, 40'h2 * 4 * PG, 4, 0); end
                if (M > 3) begin stream_rd(3, 40'h3 * 4 * PG, 4, 0); end
                if (M > 4) begin stream_rd(4, 40'h4 * 4 * PG, 4, 0); end
                if (M > 5) begin stream_rd(5, 40'h5 * 4 * PG, 4, 0); end
                if (M > 6) begin stream_rd(6, 40'h6 * 4 * PG, 4, 0); end
                if (M > 7) begin stream_rd(7, 40'h7 * 4 * PG, 4, 0); end
            join
            t1 = cyc; gbps = ((((M > 8) ? 8 : M) * 16384.0) / ((t1 - t0) * 3.333e-9)) / 1.0e9;
            $display("@@@ PERF rd_%0dm     cycles=%0d bytes=%0d GBps300=%0.2f homes_ar=%0d/%0d/%0d/%0d",
                     (M > 8) ? 8 : M, t1 - t0, ((M > 8) ? 8 : M) * 16384, gbps,
                     n_ar[0], n_ar[(N>1)?1:0], n_ar[(N>2)?2:0], n_ar[(N>3)?3:0]);
            clear_counts; t0 = cyc;
            fork
                if (M > 0) begin stream_rd(0, 40'h0 * 4 * PG, 4, 0); end
                if (M > 1) begin stream_rd(1, 40'h1 * 4 * PG, 4, 0); end
                if (M > 2) begin stream_rd(2, 40'h2 * 4 * PG, 4, 0); end
                if (M > 3) begin stream_rd(3, 40'h3 * 4 * PG, 4, 0); end
                if (M > 4) begin stream_rd(4, 40'h4 * 4 * PG, 4, 0); end
                if (M > 5) begin stream_rd(5, 40'h5 * 4 * PG, 4, 0); end
                if (M > 6) begin stream_rd(6, 40'h6 * 4 * PG, 4, 0); end
                if (M > 7) begin stream_rd(7, 40'h7 * 4 * PG, 4, 0); end
            join
            t1 = cyc; gbps = ((((M > 8) ? 8 : M) * 16384.0) / ((t1 - t0) * 3.333e-9)) / 1.0e9;
            $display("@@@ PERF rd_%0dm_re  cycles=%0d bytes=%0d GBps300=%0.2f homes_ar=%0d/%0d/%0d/%0d",
                     (M > 8) ? 8 : M, t1 - t0, ((M > 8) ? 8 : M) * 16384, gbps,
                     n_ar[0], n_ar[(N>1)?1:0], n_ar[(N>2)?2:0], n_ar[(N>3)?3:0]);
            clear_counts; t0 = cyc;
            fork
                if (M > 0) begin stream_rd_id(0, 40'h0 * 4 * PG, 4, 0, 7); end
                if (M > 1) begin stream_rd_id(1, 40'h1 * 4 * PG, 4, 0, 7); end
                if (M > 2) begin stream_rd_id(2, 40'h2 * 4 * PG, 4, 0, 7); end
                if (M > 3) begin stream_rd_id(3, 40'h3 * 4 * PG, 4, 0, 7); end
                if (M > 4) begin stream_rd_id(4, 40'h4 * 4 * PG, 4, 0, 7); end
                if (M > 5) begin stream_rd_id(5, 40'h5 * 4 * PG, 4, 0, 7); end
                if (M > 6) begin stream_rd_id(6, 40'h6 * 4 * PG, 4, 0, 7); end
                if (M > 7) begin stream_rd_id(7, 40'h7 * 4 * PG, 4, 0, 7); end
            join
            t1 = cyc; gbps = ((((M > 8) ? 8 : M) * 16384.0) / ((t1 - t0) * 3.333e-9)) / 1.0e9;
            $display("@@@ PERF rd_%0dm_1id cycles=%0d bytes=%0d GBps300=%0.2f homes_ar=%0d/%0d/%0d/%0d",
                     (M > 8) ? 8 : M, t1 - t0, ((M > 8) ? 8 : M) * 16384, gbps,
                     n_ar[0], n_ar[(N>1)?1:0], n_ar[(N>2)?2:0], n_ar[(N>3)?3:0]);
        end

        repeat(40) @(posedge clk);
        if (errors==0) begin
            $display("  PASS -- kx_pxache P%0d M%0d N%0d K%0d twoclk%0d ilv%0d dlat%0d rstag%0d, %0d checks, 0 errors",
                     P, M, N, K, TWOCLK, ILV, DLAT, RSTAG, checks);
        end else begin
            $display("  FAIL -- kx_pxache P%0d M%0d N%0d K%0d twoclk%0d ilv%0d dlat%0d rstag%0d: %0d errors / %0d checks",
                     P, M, N, K, TWOCLK, ILV, DLAT, RSTAG, errors, checks);
        end
        $finish;
    end
endmodule

`default_nettype wire

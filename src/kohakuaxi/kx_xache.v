// KX = Kohaku-Xache System; Xache = xbar-cache. This is the Xache: M AXI masters to
// N cached DRAM channels ("homes") as ONE system. AXI only at the two edges;
// between them no AXI. The whole fabric -- N kx_carray (one cache array per home,
// holding all wide data), the engines (control only) and the crossbar -- runs on
// ONE clock, `clk`. A crossing exists only at a port's edge and only where the
// user says that port's clock differs (MCDC[m] / HCDC[h]); a port on `clk` costs
// nothing. Every wide select is an indexed mux on a registered binary index.
// Engine grouping is a knob: SAMD = one per home, SASD = one over all homes;
// RSAMD/WSAMD independent. Channel interleaving is an address-bit permutation at
// the master edge (NSWAP/SWAP_*), i.e. wires. The read engine is a knob too:
// RD_PIPE=0 serves one beat per array round, RD_PIPE=1 streams a lookup per
// cycle and lets a master hold RD_OUTQ bursts across the homes, drained in
// order. One outstanding write per master.

`default_nettype none

module kx_xache #(
    parameter integer M         = 4,
    parameter integer N_HOME    = 4,
    parameter integer AW        = 40,
    parameter integer W         = 512,
    parameter integer ID_W      = 4,
    parameter integer HOME_LSB  = 32,
    parameter integer SETS      = 32768,
    parameter integer SET_W     = 15,
    parameter integer K         = 1,
    parameter         RAM_STYLE = "ultra",
    parameter [M-1:0]        MCDC = {M{1'b0}},        // 1: master m's clock != clk
    parameter [N_HOME-1:0]   HCDC = {N_HOME{1'b0}},   // 1: home h's DRAM clock != clk
    parameter integer RSAMD     = 1,
    parameter integer WSAMD     = 1,
    parameter integer CDC_DEPTH = 16,
    // Address permutation at the master edge: NSWAP bit-pair swaps (one byte of
    // bit index per pair in SWAP_A / SWAP_B), applied in order, 0 LUT. Channel
    // interleave at 2^G bytes = rotate [G, HOME_LSB+log2 N) down by log2 N:
    // pairs (i, i+log2 N) for i = G .. HOME_LSB-1 (see kx_perm.v for why not a
    // plain field swap). Every bit must be >= max(LINE_LSB, 12) -- a line stays
    // in one home, the AXI 4 KB rule keeps a burst in one -- enforced at elab.
    parameter integer NSWAP     = 0,
    parameter [((NSWAP < 1) ? 1 : NSWAP)*8-1:0] SWAP_A = 0,
    parameter [((NSWAP < 1) ? 1 : NSWAP)*8-1:0] SWAP_B = 0,
    // Read path. RD_PIPE=1: the streaming engine (kx_rd_pipe) -- one lookup per
    // cycle, a miss becomes one DRAM read for the rest of the burst, responses
    // ordered per master. RD_OUTQ: bursts a master may have outstanding across
    // homes; above 1 needs RD_PIPE (the one-beat engine has no ordering).
    parameter integer RD_PIPE   = 0,
    parameter integer RD_OUTQ   = 1,
    parameter integer MIDX_W    = (M <= 1) ? 1 : $clog2(M),
    parameter integer HIDX_W    = (N_HOME <= 1) ? 1 : $clog2(N_HOME),
    parameter integer IDW       = ID_W + MIDX_W,
    parameter integer WBYTES_LG = $clog2(W/8),
    parameter integer SUBW      = (K <= 1) ? 0 : $clog2(K),
    parameter integer LINE_LSB  = WBYTES_LG + SUBW,
    parameter integer TAG_W     = AW - LINE_LSB - SET_W,
    parameter integer STRB      = W/8,
    parameter integer ARW       = ID_W + AW + 8 + 3,
    parameter integer RW        = ID_W + W + 2 + 1,
    parameter integer WW        = W + STRB + 1,
    parameter integer BW        = ID_W + 2,
    parameter integer DARW      = IDW + AW + 8 + 3 + 2,
    parameter integer DRW       = IDW + W + 2 + 1,
    parameter integer DBW       = IDW + 2
)(
    input  wire                    clk,
    input  wire                    rstn,
    input  wire [M-1:0]            m_clk,      // used iff MCDC[m]
    input  wire [M-1:0]            m_rstn,
    input  wire [N_HOME-1:0]       h_clk,      // used iff HCDC[h]
    input  wire [N_HOME-1:0]       h_rstn,

    input  wire [M*ID_W-1:0]       s_awid,
    input  wire [M*AW-1:0]         s_awaddr,
    input  wire [M*8-1:0]          s_awlen,
    input  wire [M*3-1:0]          s_awsize,
    input  wire [M*2-1:0]          s_awburst,
    input  wire [M-1:0]            s_awvalid,
    output wire [M-1:0]            s_awready,
    input  wire [M*W-1:0]          s_wdata,
    input  wire [M*STRB-1:0]       s_wstrb,
    input  wire [M-1:0]            s_wlast,
    input  wire [M-1:0]            s_wvalid,
    output wire [M-1:0]            s_wready,
    output wire [M*ID_W-1:0]       s_bid,
    output wire [M*2-1:0]          s_bresp,
    output wire [M-1:0]            s_bvalid,
    input  wire [M-1:0]            s_bready,
    input  wire [M*ID_W-1:0]       s_arid,
    input  wire [M*AW-1:0]         s_araddr,
    input  wire [M*8-1:0]          s_arlen,
    input  wire [M*3-1:0]          s_arsize,
    input  wire [M*2-1:0]          s_arburst,
    input  wire [M-1:0]            s_arvalid,
    output wire [M-1:0]            s_arready,
    output wire [M*ID_W-1:0]       s_rid,
    output wire [M*W-1:0]          s_rdata,
    output wire [M*2-1:0]          s_rresp,
    output wire [M-1:0]            s_rlast,
    output wire [M-1:0]            s_rvalid,
    input  wire [M-1:0]            s_rready,

    output wire [N_HOME*IDW-1:0]   d_awid,
    output wire [N_HOME*AW-1:0]    d_awaddr,
    output wire [N_HOME*8-1:0]     d_awlen,
    output wire [N_HOME*3-1:0]     d_awsize,
    output wire [N_HOME*2-1:0]     d_awburst,
    output wire [N_HOME-1:0]       d_awvalid,
    input  wire [N_HOME-1:0]       d_awready,
    output wire [N_HOME*W-1:0]     d_wdata,
    output wire [N_HOME*STRB-1:0]  d_wstrb,
    output wire [N_HOME-1:0]       d_wlast,
    output wire [N_HOME-1:0]       d_wvalid,
    input  wire [N_HOME-1:0]       d_wready,
    input  wire [N_HOME*IDW-1:0]   d_bid,
    input  wire [N_HOME*2-1:0]     d_bresp,
    input  wire [N_HOME-1:0]       d_bvalid,
    output wire [N_HOME-1:0]       d_bready,
    output wire [N_HOME*IDW-1:0]   d_arid,
    output wire [N_HOME*AW-1:0]    d_araddr,
    output wire [N_HOME*8-1:0]     d_arlen,
    output wire [N_HOME*3-1:0]     d_arsize,
    output wire [N_HOME*2-1:0]     d_arburst,
    output wire [N_HOME-1:0]       d_arvalid,
    input  wire [N_HOME-1:0]       d_arready,
    input  wire [N_HOME*IDW-1:0]   d_rid,
    input  wire [N_HOME*W-1:0]     d_rdata,
    input  wire [N_HOME*2-1:0]     d_rresp,
    input  wire [N_HOME-1:0]       d_rlast,
    input  wire [N_HOME-1:0]       d_rvalid,
    output wire [N_HOME-1:0]       d_rready
);
    genvar e, i, m, h;
    localparam integer NRE = RSAMD ? N_HOME : 1;
    localparam integer NWE = WSAMD ? N_HOME : 1;
    localparam integer SEQW = (RD_OUTQ <= 1) ? 1 : $clog2(RD_OUTQ);
    localparam integer OSTW = $clog2(RD_OUTQ + 1);
    generate if (RD_OUTQ > 1 && RD_PIPE == 0) begin : g_outq_guard
        kx_xache_RD_OUTQ_above_1_needs_RD_PIPE u_illegal ();
    end endgenerate

    // ======================= master edge: one crossing per port iff MCDC =======================
    // x_* is the master's bus on `clk`
    wire [M*ID_W-1:0] x_awid, x_arid, x_bid, x_rid;
    wire [M*AW-1:0]   x_awaddr, x_araddr;
    wire [M*8-1:0]    x_awlen, x_arlen;
    wire [M*3-1:0]    x_awsize, x_arsize;
    wire [M-1:0]      x_awvalid, x_awready, x_wvalid, x_wready, x_wlast;
    wire [M-1:0]      x_arvalid, x_arready, x_rvalid, x_rready, x_rlast, x_bvalid, x_bready;
    wire [M*W-1:0]    x_wdata, x_rdata;
    wire [M*STRB-1:0] x_wstrb;
    wire [M*2-1:0]    x_rresp, x_bresp;

    generate for (m = 0; m < M; m = m + 1) begin : g_medge
        localparam integer SAME = MCDC[m] ? 0 : 1;
        wire mclk  = MCDC[m] ? m_clk[m]  : clk;
        wire mrst  = MCDC[m] ? ~m_rstn[m] : ~rstn;
        wire [ARW-1:0] aw_pk_o, ar_pk_o;
        kx_link #(.WIDTH(ARW), .SAME(SAME), .DEPTH(CDC_DEPTH)) u_aw (
            .wr_clk(mclk), .wr_rst(mrst),
            .s_valid(s_awvalid[m]), .s_ready(s_awready[m]),
            .s_data({s_awid[m*ID_W +: ID_W], s_awaddr[m*AW +: AW], s_awlen[m*8 +: 8], s_awsize[m*3 +: 3]}),
            .rd_clk(clk), .m_valid(x_awvalid[m]), .m_ready(x_awready[m]), .m_data(aw_pk_o));
        assign {x_awid[m*ID_W +: ID_W], x_awaddr[m*AW +: AW], x_awlen[m*8 +: 8], x_awsize[m*3 +: 3]} = aw_pk_o;
        kx_link #(.WIDTH(ARW), .SAME(SAME), .DEPTH(CDC_DEPTH)) u_ar (
            .wr_clk(mclk), .wr_rst(mrst),
            .s_valid(s_arvalid[m]), .s_ready(s_arready[m]),
            .s_data({s_arid[m*ID_W +: ID_W], s_araddr[m*AW +: AW], s_arlen[m*8 +: 8], s_arsize[m*3 +: 3]}),
            .rd_clk(clk), .m_valid(x_arvalid[m]), .m_ready(x_arready[m]), .m_data(ar_pk_o));
        assign {x_arid[m*ID_W +: ID_W], x_araddr[m*AW +: AW], x_arlen[m*8 +: 8], x_arsize[m*3 +: 3]} = ar_pk_o;
        wire [WW-1:0] w_pk_o;
        kx_link #(.WIDTH(WW), .SAME(SAME), .DEPTH(CDC_DEPTH), .MEM("block")) u_w (
            .wr_clk(mclk), .wr_rst(mrst),
            .s_valid(s_wvalid[m]), .s_ready(s_wready[m]),
            .s_data({s_wdata[m*W +: W], s_wstrb[m*STRB +: STRB], s_wlast[m]}),
            .rd_clk(clk), .m_valid(x_wvalid[m]), .m_ready(x_wready[m]), .m_data(w_pk_o));
        assign {x_wdata[m*W +: W], x_wstrb[m*STRB +: STRB], x_wlast[m]} = w_pk_o;
        wire [RW-1:0] r_pk_o;
        kx_link #(.WIDTH(RW), .SAME(SAME), .DEPTH(CDC_DEPTH), .MEM("block")) u_r (
            .wr_clk(clk), .wr_rst(~rstn),
            .s_valid(x_rvalid[m]), .s_ready(x_rready[m]),
            .s_data({x_rid[m*ID_W +: ID_W], x_rdata[m*W +: W], x_rresp[m*2 +: 2], x_rlast[m]}),
            .rd_clk(mclk), .m_valid(s_rvalid[m]), .m_ready(s_rready[m]), .m_data(r_pk_o));
        assign {s_rid[m*ID_W +: ID_W], s_rdata[m*W +: W], s_rresp[m*2 +: 2], s_rlast[m]} = r_pk_o;
        wire [BW-1:0] b_pk_o;
        kx_link #(.WIDTH(BW), .SAME(SAME), .DEPTH(CDC_DEPTH)) u_b (
            .wr_clk(clk), .wr_rst(~rstn),
            .s_valid(x_bvalid[m]), .s_ready(x_bready[m]),
            .s_data({x_bid[m*ID_W +: ID_W], x_bresp[m*2 +: 2]}),
            .rd_clk(mclk), .m_valid(s_bvalid[m]), .m_ready(s_bready[m]), .m_data(b_pk_o));
        assign {s_bid[m*ID_W +: ID_W], s_bresp[m*2 +: 2]} = b_pk_o;
    end endgenerate

    // ======================= DRAM edge: one crossing per home iff HCDC =======================
    // y_* is the home's DRAM bus on `clk`
    wire [N_HOME*IDW-1:0]  y_awid, y_arid, y_bid;
    wire [N_HOME*AW-1:0]   y_awaddr, y_araddr;
    wire [N_HOME*8-1:0]    y_awlen, y_arlen;
    wire [N_HOME*3-1:0]    y_awsize, y_arsize;
    wire [N_HOME*2-1:0]    y_awburst, y_arburst, y_bresp, y_rresp;
    wire [N_HOME-1:0]      y_awvalid, y_awready, y_wvalid, y_wready, y_wlast;
    wire [N_HOME-1:0]      y_arvalid, y_arready, y_rvalid, y_rready, y_rlast, y_bvalid, y_bready;
    wire [N_HOME*W-1:0]    y_wdata, y_rdata;
    wire [N_HOME*STRB-1:0] y_wstrb;

    generate for (h = 0; h < N_HOME; h = h + 1) begin : g_hedge
        localparam integer SAME = HCDC[h] ? 0 : 1;
        wire hclk = HCDC[h] ? h_clk[h]  : clk;
        wire hrst = HCDC[h] ? ~h_rstn[h] : ~rstn;
        wire [DARW-1:0] aw_o, ar_o;
        kx_link #(.WIDTH(DARW), .SAME(SAME), .DEPTH(CDC_DEPTH)) u_aw (
            .wr_clk(clk), .wr_rst(~rstn),
            .s_valid(y_awvalid[h]), .s_ready(y_awready[h]),
            .s_data({y_awid[h*IDW +: IDW], y_awaddr[h*AW +: AW], y_awlen[h*8 +: 8], y_awsize[h*3 +: 3], y_awburst[h*2 +: 2]}),
            .rd_clk(hclk), .m_valid(d_awvalid[h]), .m_ready(d_awready[h]), .m_data(aw_o));
        assign {d_awid[h*IDW +: IDW], d_awaddr[h*AW +: AW], d_awlen[h*8 +: 8], d_awsize[h*3 +: 3], d_awburst[h*2 +: 2]} = aw_o;
        kx_link #(.WIDTH(DARW), .SAME(SAME), .DEPTH(CDC_DEPTH)) u_ar (
            .wr_clk(clk), .wr_rst(~rstn),
            .s_valid(y_arvalid[h]), .s_ready(y_arready[h]),
            .s_data({y_arid[h*IDW +: IDW], y_araddr[h*AW +: AW], y_arlen[h*8 +: 8], y_arsize[h*3 +: 3], y_arburst[h*2 +: 2]}),
            .rd_clk(hclk), .m_valid(d_arvalid[h]), .m_ready(d_arready[h]), .m_data(ar_o));
        assign {d_arid[h*IDW +: IDW], d_araddr[h*AW +: AW], d_arlen[h*8 +: 8], d_arsize[h*3 +: 3], d_arburst[h*2 +: 2]} = ar_o;
        wire [WW-1:0] w_o;
        kx_link #(.WIDTH(WW), .SAME(SAME), .DEPTH(CDC_DEPTH), .MEM("block")) u_w (
            .wr_clk(clk), .wr_rst(~rstn),
            .s_valid(y_wvalid[h]), .s_ready(y_wready[h]),
            .s_data({y_wdata[h*W +: W], y_wstrb[h*STRB +: STRB], y_wlast[h]}),
            .rd_clk(hclk), .m_valid(d_wvalid[h]), .m_ready(d_wready[h]), .m_data(w_o));
        assign {d_wdata[h*W +: W], d_wstrb[h*STRB +: STRB], d_wlast[h]} = w_o;
        wire [DRW-1:0] r_o;
        kx_link #(.WIDTH(DRW), .SAME(SAME), .DEPTH(CDC_DEPTH), .MEM("block")) u_r (
            .wr_clk(hclk), .wr_rst(hrst),
            .s_valid(d_rvalid[h]), .s_ready(d_rready[h]),
            .s_data({d_rid[h*IDW +: IDW], d_rdata[h*W +: W], d_rresp[h*2 +: 2], d_rlast[h]}),
            .rd_clk(clk), .m_valid(y_rvalid[h]), .m_ready(y_rready[h]), .m_data(r_o));
        wire [IDW-1:0] y_rid_unused;
        assign {y_rid_unused, y_rdata[h*W +: W], y_rresp[h*2 +: 2], y_rlast[h]} = r_o;
        wire [DBW-1:0] b_o;
        kx_link #(.WIDTH(DBW), .SAME(SAME), .DEPTH(CDC_DEPTH)) u_b (
            .wr_clk(hclk), .wr_rst(hrst),
            .s_valid(d_bvalid[h]), .s_ready(d_bready[h]),
            .s_data({d_bid[h*IDW +: IDW], d_bresp[h*2 +: 2]}),
            .rd_clk(clk), .m_valid(y_bvalid[h]), .m_ready(y_bready[h]), .m_data(b_o));
        assign {y_bid[h*IDW +: IDW], y_bresp[h*2 +: 2]} = b_o;
    end endgenerate

    // ============================ fabric, all on clk ============================
    wire [HIDX_W-1:0] rhome [0:M-1];
    wire [HIDX_W-1:0] whome [0:M-1];
    wire [M*IDW-1:0]  arid_ot, awid_ot;
    // p_* is the master's address after the permutation: the space every engine,
    // array and DRAM port sees. NSWAP=0 makes it x_* by wire.
    wire [M*AW-1:0]   p_awaddr, p_araddr;
    localparam integer SWAP_MIN = (LINE_LSB > 12) ? LINE_LSB : 12;
    generate for (m = 0; m < M; m = m + 1) begin : g_route
        kx_perm #(.WIDTH(AW), .NSWAP(NSWAP), .SWAP_A(SWAP_A), .SWAP_B(SWAP_B),
                          .MIN_BIT(SWAP_MIN)) u_paw (.i(x_awaddr[m*AW +: AW]), .o(p_awaddr[m*AW +: AW]));
        kx_perm #(.WIDTH(AW), .NSWAP(NSWAP), .SWAP_A(SWAP_A), .SWAP_B(SWAP_B),
                          .MIN_BIT(SWAP_MIN)) u_par (.i(x_araddr[m*AW +: AW]), .o(p_araddr[m*AW +: AW]));
        assign rhome[m] = p_araddr[m*AW + HOME_LSB +: HIDX_W];
        assign whome[m] = p_awaddr[m*AW + HOME_LSB +: HIDX_W];
        assign arid_ot[m*IDW +: IDW] = {m[MIDX_W-1:0], x_arid[m*ID_W +: ID_W]};
        assign awid_ot[m*IDW +: IDW] = {m[MIDX_W-1:0], x_awid[m*ID_W +: ID_W]};
    end endgenerate

    wire [N_HOME-1:0]          c_flush, c_rd_en, c_rd_take, c_land, c_hit_c, c_hit;
    wire [N_HOME-1:0]          c_fill_go, c_fill_ready, c_fill_done, c_wr_en, c_wr_full;
    wire [N_HOME*SET_W-1:0]    c_rd_idx, c_fill_idx, c_wr_idx;
    wire [N_HOME*TAG_W-1:0]    c_rd_tag, c_fill_tag, c_wr_tag;
    wire [N_HOME*(SUBW+1)-1:0] c_rd_sub;
    wire [N_HOME*W-1:0]        c_word, c_wr_word;

    generate for (h = 0; h < N_HOME; h = h + 1) begin : g_carray
        kx_carray #(.AW(AW), .W(W), .SETS(SETS), .SET_W(SET_W), .K(K), .RAM_STYLE(RAM_STYLE),
                    .FILL_SERVE(RD_PIPE ? 0 : 1)) u_c (
            .clk(clk), .resetn(rstn),
            .rd_en(c_rd_en[h]), .rd_idx(c_rd_idx[h*SET_W +: SET_W]),
            .rd_tag(c_rd_tag[h*TAG_W +: TAG_W]), .rd_sub(c_rd_sub[h*(SUBW+1) +: SUBW+1]),
            .rd_take(c_rd_take[h]), .land(c_land[h]), .hit_c(c_hit_c[h]),
            .hit(c_hit[h]), .word(c_word[h*W +: W]),
            .fill_go(c_fill_go[h]), .r_data(y_rdata[h*W +: W]), .r_valid(y_rvalid[h]),
            .r_last(y_rlast[h]), .fill_idx(c_fill_idx[h*SET_W +: SET_W]),
            .fill_tag(c_fill_tag[h*TAG_W +: TAG_W]), .fill_ready(c_fill_ready[h]),
            .fill_done(c_fill_done[h]),
            .wr_en(c_wr_en[h]), .wr_idx(c_wr_idx[h*SET_W +: SET_W]),
            .wr_tag(c_wr_tag[h*TAG_W +: TAG_W]), .wr_word(c_wr_word[h*W +: W]),
            .wr_full(c_wr_full[h]), .flush_busy(c_flush[h]));
    end endgenerate

    // per-master read bookkeeping (kept in g_agg): the sequence number given to
    // each burst and the one that may drain next
    wire [SEQW-1:0]  seq_alloc [0:M-1];
    wire [SEQW-1:0]  seq_drain [0:M-1];
    wire [M-1:0]     ar_ok;                 // below RD_OUTQ: the engines may take this master's AR
    localparam integer HOSN = 1 << SEQW;

    // per-master target-home valid gating, per-home ready gathering
    wire [M-1:0]   ar_rdy_hm [0:N_HOME-1];
    wire [M-1:0]   aw_rdy_hm [0:N_HOME-1];
    wire [M-1:0]   w_rdy_hm  [0:N_HOME-1];
    localparam integer RNW = ID_W + 2 + 1;              // narrow read response: {id, resp, last}
    wire [NRE-1:0] rv_em [0:M-1];
    wire [RNW-1:0] rd_em [0:M-1][0:NRE-1];
    wire [NWE-1:0] bv_em [0:M-1];
    wire [BW-1:0]  bd_em [0:M-1][0:NWE-1];
    // the two M×N one-hots: sel_r[h][m] = home h's word goes to master m now;
    // sel_w[h][m] = master m's W beat goes to home h now. ORed across engines.
    wire [M-1:0]   sel_r_e [0:NRE-1][0:N_HOME-1];
    wire [M-1:0]   sel_w_e [0:NWE-1][0:N_HOME-1];
    // REGISTERED binary indices per engine, straight from the engines. A
    // combinational select derived from the one-hots merged into every data bit
    // (5 LUT/bit); a flop-driven select lets the 4:1 map to LUT6+MUXF7.
    wire [HIDX_W-1:0] r_hidx_e [0:NRE-1];
    wire [MIDX_W-1:0] w_midx_e [0:NWE-1];
    wire [SEQW-1:0]   r_seq_e  [0:NRE-1];

    // ================================ READ engines ================================
    generate for (e = 0; e < NRE; e = e + 1) begin : g_rd
        localparam integer BASE = RSAMD ? e : 0;
        localparam integer NH   = RSAMD ? 1 : N_HOME;
        localparam integer HIW  = (NH <= 1) ? 1 : $clog2(NH);
        wire [HIW-1:0]      r_hidx_l;
        assign r_hidx_e[e] = (NH <= 1) ? BASE[HIDX_W-1:0] : (BASE[HIDX_W-1:0] + r_hidx_l);
        wire [NH*M-1:0]     q_val, q_rdy;
        wire [NH*M*IDW-1:0] q_id;
        wire [NH*M*AW-1:0]  q_addr;
        wire [NH*M*8-1:0]   q_len;
        wire [NH*M*3-1:0]   q_size;
        wire [NH*M*SEQW-1:0] q_seq;
        wire            r_val, r_rdy, r_last;
        wire [IDW-1:0]  r_id;
        wire [1:0]      r_resp;
        wire [NH-1:0]   r_home;
        wire [SEQW-1:0] r_seq;
        wire [MIDX_W-1:0] r_own = r_id[ID_W +: MIDX_W];
        assign r_seq_e[e] = r_seq;

        wire [IDW-1:0] e_arid;  wire [AW-1:0] e_araddr; wire [7:0] e_arlen;
        wire [2:0] e_arsize;    wire [1:0] e_arburst;
        wire [SET_W-1:0] e_rd_idx, e_fl_idx; wire [TAG_W-1:0] e_rd_tag, e_fl_tag; wire [SUBW:0] e_rd_sub;

        // no r_data here: the fabric selects the home's word straight onto the
        // master (one M×N one-hot, below); this engine contributes sel_r bits only

        for (i = 0; i < NH; i = i + 1) begin : g_h
            localparam integer GH = BASE + i;
            assign y_arid[GH*IDW +: IDW] = e_arid;   assign y_araddr[GH*AW +: AW] = e_araddr;
            assign y_arlen[GH*8 +: 8] = e_arlen;     assign y_arsize[GH*3 +: 3] = e_arsize;
            assign y_arburst[GH*2 +: 2] = e_arburst;
            assign c_rd_idx[GH*SET_W +: SET_W] = e_rd_idx;
            assign c_rd_tag[GH*TAG_W +: TAG_W] = e_rd_tag;
            assign c_rd_sub[GH*(SUBW+1) +: SUBW+1] = e_rd_sub;
            assign c_fill_idx[GH*SET_W +: SET_W] = e_fl_idx;
            assign c_fill_tag[GH*TAG_W +: TAG_W] = e_fl_tag;
            for (m = 0; m < M; m = m + 1) begin : g_m
                wire tgt = (rhome[m] == GH[HIDX_W-1:0]);
                // an engine may only latch an AR the master edge will count
                assign q_val[i*M+m] = x_arvalid[m] && tgt && ar_ok[m];
                assign ar_rdy_hm[GH][m] = q_rdy[i*M+m];
                assign q_id[(i*M+m)*IDW +: IDW] = arid_ot[m*IDW +: IDW];
                assign q_addr[(i*M+m)*AW +: AW] = p_araddr[m*AW +: AW];
                assign q_len[(i*M+m)*8 +: 8]    = x_arlen[m*8 +: 8];
                assign q_size[(i*M+m)*3 +: 3]   = x_arsize[m*3 +: 3];
                assign q_seq[(i*M+m)*SEQW +: SEQW] = seq_alloc[m];
            end
        end
        // ordered drain: only the master's oldest outstanding burst presents.
        // The turn is REGISTERED: it changes a cycle after the previous burst's
        // last beat, when the engine's first landing is still >= RD_LAT away,
        // and the compare was the head of the accept path.
        reg turn_q;
        always @(posedge clk) turn_q <= (RD_PIPE == 0) || (r_seq == seq_drain[r_own]);
        for (m = 0; m < M; m = m + 1) begin : g_rsp
            assign rv_em[m][e] = r_val && (r_own == m[MIDX_W-1:0]) && turn_q;
            assign rd_em[m][e] = {r_id[ID_W-1:0], r_resp, r_last};
        end
        // RD_PIPE=0: the engine retires only on the master's DELAYED valid
        // (x_rvalid lags r_val by the index flop). RD_PIPE=1: on ITS OWN visible
        // valid -- another engine's burst may be draining to the same master.
        wire [M-1:0] rv_e_m;
        for (m = 0; m < M; m = m + 1) begin : g_rvm
            assign rv_e_m[m] = rv_em[m][e];
        end
        assign r_rdy = x_rready[r_own] && ((RD_PIPE != 0) ? (|rv_e_m) : x_rvalid[r_own]);
        // publish this engine's (home, master) one-hot for the fabric's R select
        for (h = 0; h < N_HOME; h = h + 1) begin : g_selr
            for (m = 0; m < M; m = m + 1) begin : g_m
                if (h >= BASE && h < BASE + NH) begin : in
                    assign sel_r_e[e][h][m] = r_val && r_home[h-BASE] && (r_own == m[MIDX_W-1:0]);
                end else begin : out
                    assign sel_r_e[e][h][m] = 1'b0;
                end
            end
        end

        if (RD_PIPE) begin : g_pipe
            kx_rd_pipe #(.M(M), .NH(NH), .AW(AW), .W(W), .IDW(IDW), .SET_W(SET_W),
                         .K(K), .RAM_STYLE(RAM_STYLE), .SEQW(SEQW)) u_re (
                .clk(clk), .resetn(rstn), .flush_busy(c_flush[BASE +: NH]),
                .mr_qval(q_val), .mr_qrdy(q_rdy), .mr_qid(q_id), .mr_qaddr(q_addr),
                .mr_qlen(q_len), .mr_qseq(q_seq),
                .r_val(r_val), .r_rdy(r_rdy), .r_id(r_id), .r_resp(r_resp), .r_last(r_last),
                .r_home(r_home), .r_hidx(r_hidx_l), .r_seq(r_seq),
                .c_rd_en(c_rd_en[BASE +: NH]), .c_rd_idx(e_rd_idx), .c_rd_tag(e_rd_tag),
                .c_rd_sub(e_rd_sub), .c_rd_take(c_rd_take[BASE +: NH]),
                .c_land(c_land[BASE +: NH]), .c_hit_c(c_hit_c[BASE +: NH]),
                .c_fill_go(c_fill_go[BASE +: NH]), .c_fill_idx(e_fl_idx), .c_fill_tag(e_fl_tag),
                .c_fill_ready(c_fill_ready[BASE +: NH]), .c_fill_done(c_fill_done[BASE +: NH]),
                .m_arid(e_arid), .m_araddr(e_araddr), .m_arlen(e_arlen), .m_arsize(e_arsize),
                .m_arburst(e_arburst), .m_arvalid(y_arvalid[BASE +: NH]),
                .m_arready(y_arready[BASE +: NH]),
                .m_rvalid(y_rvalid[BASE +: NH]), .m_rlast(y_rlast[BASE +: NH]),
                .m_rresp(y_rresp[BASE*2 +: NH*2]), .m_rready(y_rready[BASE +: NH]));
        end else begin : g_one
            assign r_seq = {SEQW{1'b0}};
            assign e_fl_idx = e_rd_idx;
            assign e_fl_tag = e_rd_tag;
            assign c_rd_take[BASE +: NH] = {NH{1'b1}};
            kx_rd_engine #(.M(M), .NH(NH), .AW(AW), .W(W), .IDW(IDW), .SET_W(SET_W),
                           .K(K), .RAM_STYLE(RAM_STYLE)) u_re (
                .clk(clk), .resetn(rstn), .flush_busy(c_flush[BASE +: NH]),
                .mr_qval(q_val), .mr_qrdy(q_rdy), .mr_qid(q_id), .mr_qaddr(q_addr),
                .mr_qlen(q_len), .mr_qsize(q_size),
                .r_val(r_val), .r_rdy(r_rdy), .r_id(r_id), .r_resp(r_resp), .r_last(r_last),
                .r_home(r_home), .r_hidx(r_hidx_l),
                .c_rd_en(c_rd_en[BASE +: NH]), .c_rd_idx(e_rd_idx), .c_rd_tag(e_rd_tag),
                .c_rd_sub(e_rd_sub), .c_hit(c_hit[BASE +: NH]),
                .c_fill_go(c_fill_go[BASE +: NH]), .c_fill_ready(c_fill_ready[BASE +: NH]),
                .c_fill_done(c_fill_done[BASE +: NH]),
                .m_arid(e_arid), .m_araddr(e_araddr), .m_arlen(e_arlen), .m_arsize(e_arsize),
                .m_arburst(e_arburst), .m_arvalid(y_arvalid[BASE +: NH]),
                .m_arready(y_arready[BASE +: NH]),
                .m_rresp(y_rresp[BASE*2 +: NH*2]), .m_rready(y_rready[BASE +: NH]));
        end
    end endgenerate

    // ================================ WRITE engines ===============================
    generate for (e = 0; e < NWE; e = e + 1) begin : g_wr
        localparam integer BASE = WSAMD ? e : 0;
        localparam integer NH   = WSAMD ? 1 : N_HOME;
        localparam integer PW   = ((NH*M) <= 1) ? 1 : $clog2(NH*M);
        wire [PW-1:0]       gidx_l;                     // flat (home-local, master)
        assign w_midx_e[e] = gidx_l % M;                // master index = low part
        wire [NH*M-1:0]     q_val, q_rdy, w_val, w_rdy, w_last;
        wire [NH*M*IDW-1:0] q_id;
        wire [NH*M*AW-1:0]  q_addr;
        wire [NH*M*8-1:0]   q_len;
        wire [NH*M*3-1:0]   q_size;
        wire [NH*M*W-1:0]   w_data;
        wire [NH*M*STRB-1:0] w_strb;
        wire [NH*M-1:0]     gsel;
        wire [NH-1:0]       hsel;
        wire            b_val, b_rdy;
        wire [IDW-1:0]  b_id;
        wire [1:0]      b_resp;
        wire [MIDX_W-1:0] b_own = b_id[ID_W +: MIDX_W];

        wire [IDW-1:0] e_awid; wire [AW-1:0] e_awaddr; wire [7:0] e_awlen;
        wire [2:0] e_awsize;   wire [1:0] e_awburst;
        wire [SET_W-1:0] e_wr_idx; wire [TAG_W-1:0] e_wr_tag;

        wire g_wlast = |(w_last & gsel);
        // publish this engine's (home, master) one-hot for the fabric's W select:
        // gsel is over (home-local i, master m) slots, hsel picks the home.
        for (h = 0; h < N_HOME; h = h + 1) begin : g_selw
            for (m = 0; m < M; m = m + 1) begin : g_m
                if (h >= BASE && h < BASE + NH) begin : in
                    assign sel_w_e[e][h][m] = gsel[(h-BASE)*M + m];
                end else begin : out
                    assign sel_w_e[e][h][m] = 1'b0;
                end
            end
        end

        for (i = 0; i < NH; i = i + 1) begin : g_h
            localparam integer GH = BASE + i;
            assign y_awid[GH*IDW +: IDW] = e_awid;   assign y_awaddr[GH*AW +: AW] = e_awaddr;
            assign y_awlen[GH*8 +: 8] = e_awlen;     assign y_awsize[GH*3 +: 3] = e_awsize;
            assign y_awburst[GH*2 +: 2] = e_awburst;
            assign y_wlast[GH] = g_wlast;
            assign c_wr_idx[GH*SET_W +: SET_W] = e_wr_idx;
            assign c_wr_tag[GH*TAG_W +: TAG_W] = e_wr_tag;
            for (m = 0; m < M; m = m + 1) begin : g_m
                wire tgt = (whome[m] == GH[HIDX_W-1:0]);
                assign q_val[i*M+m] = x_awvalid[m] && tgt;
                assign aw_rdy_hm[GH][m] = q_rdy[i*M+m];
                assign q_id[(i*M+m)*IDW +: IDW] = awid_ot[m*IDW +: IDW];
                assign q_addr[(i*M+m)*AW +: AW] = p_awaddr[m*AW +: AW];
                assign q_len[(i*M+m)*8 +: 8]    = x_awlen[m*8 +: 8];
                assign q_size[(i*M+m)*3 +: 3]   = x_awsize[m*3 +: 3];
                assign w_val[i*M+m] = x_wvalid[m] && tgt;
                assign w_rdy_hm[GH][m] = w_rdy[i*M+m];
                assign w_data[(i*M+m)*W +: W]       = x_wdata[m*W +: W];
                assign w_strb[(i*M+m)*STRB +: STRB] = x_wstrb[m*STRB +: STRB];
                assign w_last[i*M+m] = x_wlast[m];
            end
        end
        for (m = 0; m < M; m = m + 1) begin : g_rsp
            assign bv_em[m][e] = b_val && (b_own == m[MIDX_W-1:0]);
            assign bd_em[m][e] = {b_id[ID_W-1:0], b_resp};
        end
        assign b_rdy = x_bready[b_own];

        kx_wr_engine #(.M(M), .NH(NH), .AW(AW), .W(W), .IDW(IDW), .SET_W(SET_W), .K(K)) u_we (
            .clk(clk), .resetn(rstn), .flush_busy(c_flush[BASE +: NH]),
            .mw_qval(q_val), .mw_qrdy(q_rdy), .mw_qid(q_id), .mw_qaddr(q_addr),
            .mw_qlen(q_len), .mw_qsize(q_size),
            .mw_wval(w_val), .mw_wrdy(w_rdy), .mw_wlast(w_last),
            .gsel(gsel), .gidx(gidx_l), .hsel(hsel),
            .b_val(b_val), .b_rdy(b_rdy), .b_id(b_id), .b_resp(b_resp),
            .c_wr_en(c_wr_en[BASE +: NH]), .c_wr_idx(e_wr_idx), .c_wr_tag(e_wr_tag),
            .m_awid(e_awid), .m_awaddr(e_awaddr), .m_awlen(e_awlen), .m_awsize(e_awsize),
            .m_awburst(e_awburst), .m_awvalid(y_awvalid[BASE +: NH]),
            .m_awready(y_awready[BASE +: NH]),
            .m_wvalid(y_wvalid[BASE +: NH]), .m_wready(y_wready[BASE +: NH]),
            .m_bresp(y_bresp[BASE*2 +: NH*2]), .m_bvalid(y_bvalid[BASE +: NH]),
            .m_bready(y_bready[BASE +: NH]));
    end endgenerate

    // ================================ aggregation =================================
    generate for (m = 0; m < M; m = m + 1) begin : g_agg
        wire [N_HOME-1:0] arr, awr, wrr;
        for (h = 0; h < N_HOME; h = h + 1) begin : g_pick
            assign arr[h] = ar_rdy_hm[h][m] && (rhome[m] == h[HIDX_W-1:0]);
            assign awr[h] = aw_rdy_hm[h][m] && (whome[m] == h[HIDX_W-1:0]);
            assign wrr[h] = w_rdy_hm [h][m] && (whome[m] == h[HIDX_W-1:0]);
        end
        reg rv_q;
        // outstanding bursts per master: AR accepted while below RD_OUTQ; a
        // sequence number per burst and the home it went to, so the oldest
        // drains first and the R data select is known before it presents
        wire ar_acc = x_arvalid[m] && x_arready[m];
        wire rl_acc = x_rvalid[m] && x_rready[m] && x_rlast[m];
        reg  [SEQW-1:0]   seqa_r, seqd_r;
        reg  [OSTW-1:0]   outst;
        reg  [HIDX_W-1:0] hos [0:HOSN-1];
        wire [SEQW-1:0]   seq_drain_n = seqd_r + (rl_acc ? 1'b1 : 1'b0);
        assign seq_alloc[m] = seqa_r;
        assign seq_drain[m] = seqd_r;
        integer oq;
        always @(posedge clk) begin
            if (!rstn) begin
                seqa_r <= 0; seqd_r <= 0; outst <= 0;
                for (oq = 0; oq < HOSN; oq = oq + 1) hos[oq] <= 0;
            end else begin
                if (ar_acc) begin
                    seqa_r <= seqa_r + 1'b1;
                    hos[seqa_r] <= rhome[m];
                end
                if (rl_acc) seqd_r <= seq_drain_n;
                outst <= outst + (ar_acc ? 1'b1 : 1'b0) - (rl_acc ? 1'b1 : 1'b0);
            end
        end
        assign ar_ok[m] = (outst < RD_OUTQ);
        assign x_arready[m] = |arr && ar_ok[m];
        assign x_awready[m] = |awr;
        assign x_wready [m] = |wrr;
        // RD_PIPE=0: delayed valid, MASKED by the live one -- the cycle after
        // the engine retires, rv_q is still high and would present a phantom
        // beat of stale data (measured: every burst beat N+1 returned beat N).
        // RD_PIPE=1: the engine's registered valid, gated by turn.
        assign x_rvalid [m] = RD_PIPE ? (|rv_em[m]) : (rv_q && (|rv_em[m]));
        assign x_bvalid [m] = |bv_em[m];

        // narrow response fields, one-hot across engines
        wire [NRE*RNW-1:0] rt; wire [NWE*BW-1:0] bt;
        for (h = 0; h < NRE; h = h + 1) begin : g_rt
            assign rt[h*RNW +: RNW] = rd_em[m][h] & {RNW{rv_em[m][h]}};
        end
        for (h = 0; h < NWE; h = h + 1) begin : g_bt
            assign bt[h*BW +: BW] = bd_em[m][h] & {BW{bv_em[m][h]}};
        end
        reg [RNW-1:0] rsel; reg [BW-1:0] bsel; integer ee;
        always @(*) begin
            rsel = {RNW{1'b0}}; bsel = {BW{1'b0}};
            for (ee = 0; ee < NRE; ee = ee + 1) rsel = rsel | rt[ee*RNW +: RNW];
            for (ee = 0; ee < NWE; ee = ee + 1) bsel = bsel | bt[ee*BW +: BW];
        end
        assign x_rid  [m*ID_W +: ID_W] = rsel[3 +: ID_W];
        assign x_rresp[m*2 +: 2]       = rsel[1 +: 2];
        assign x_rlast[m]              = rsel[0];
        assign x_bid  [m*ID_W +: ID_W] = bsel[2 +: ID_W];
        assign x_bresp[m*2 +: 2]       = bsel[0 +: 2];

        // R data: an INDEXED N:1 mux on a binary home index. A one-hot AND-OR
        // was built as LUT4/5 trees at ~8 LUT/bit (census: s_rdata 4,112 LUT);
        // a binary-select mux maps to LUT6+MUXF7 at ~1 LUT/bit.
        wire [N_HOME-1:0] selr_m;
        for (h = 0; h < N_HOME; h = h + 1) begin : g_selr
            wire [NRE-1:0] per_e;
            for (i = 0; i < NRE; i = i + 1) begin : g_e
                assign per_e[i] = sel_r_e[i][h][m];
            end
            assign selr_m[h] = |per_e;
        end
        // home index for this master, REGISTERED: a combinational NRE:1 on the
        // index still merged into every data bit (census s_rdata 4,096 = 8/bit).
        // The response sits in R_DRAIN >= 1 cycle, so a flop-delayed index is
        // free and the 4:1 then maps to LUT6+MUXF7.
        reg [HIDX_W-1:0] ridx_c; integer ee2;
        always @(*) begin
            ridx_c = {HIDX_W{1'b0}};
            for (ee2 = 0; ee2 < NRE; ee2 = ee2 + 1) if (rv_em[m][ee2]) ridx_c = ridx_c | r_hidx_e[ee2];
        end
        // RD_PIPE=0: index and valid delayed together so data/valid stay
        // aligned; the engine holds r_val in R_DRAIN until this delayed valid
        // is accepted. RD_PIPE=1: the index is the home of the burst that drains
        // next, known from the AR -- registered the cycle it becomes current, so
        // it is stable before the first beat presents and streams need no delay.
        reg [HIDX_W-1:0] ridx_m;
        if (RD_PIPE == 0) begin : g_ridx_one
            always @(posedge clk) begin ridx_m <= ridx_c; rv_q <= |rv_em[m] && rstn; end
        end else if (RSAMD) begin : g_ridx_seq
            always @(posedge clk) begin
                rv_q <= 1'b0;
                ridx_m <= (ar_acc && (seqa_r == seq_drain_n)) ? rhome[m] : hos[seq_drain_n];
            end
        end else begin : g_ridx_sasd
            always @(posedge clk) begin rv_q <= 1'b0; ridx_m <= r_hidx_e[0]; end
        end
        assign x_rdata[m*W +: W] = c_word[ridx_m*W +: W];
    end endgenerate

    // W data: an INDEXED M:1 mux per home on a binary master index (same reason)
    generate for (h = 0; h < N_HOME; h = h + 1) begin : g_wsel
        wire [M-1:0] selw_h;
        for (m = 0; m < M; m = m + 1) begin : g_m
            wire [NWE-1:0] per_e;
            for (i = 0; i < NWE; i = i + 1) begin : g_e
                assign per_e[i] = sel_w_e[i][h][m];
            end
            assign selw_h[m] = |per_e;
        end
        // master index for this home = its write engine's REGISTERED gidx (SAMD:
        // engine h; SASD: the one engine), a narrow NWE:1 on MIDX_W bits
        reg [MIDX_W-1:0] widx_h; integer ee3;
        always @(*) begin
            widx_h = {MIDX_W{1'b0}};
            for (ee3 = 0; ee3 < NWE; ee3 = ee3 + 1)
                if (WSAMD ? (ee3 == h) : 1'b1) widx_h = widx_h | w_midx_e[ee3];
        end
        wire [W-1:0]    wdata_h = x_wdata[widx_h*W +: W];
        wire [STRB-1:0] wstrb_h = x_wstrb[widx_h*STRB +: STRB];
        assign y_wdata[h*W +: W]       = wdata_h;
        assign y_wstrb[h*STRB +: STRB] = wstrb_h;
        assign c_wr_word[h*W +: W]     = wdata_h;
        assign c_wr_full[h]            = &wstrb_h;
    end endgenerate
endmodule

`default_nettype wire

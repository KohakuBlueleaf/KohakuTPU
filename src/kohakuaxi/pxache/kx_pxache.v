// Kohaku Partitioned Xache: the Xache's masters and homes spread over P
// partitions of one clock domain (dies of a part, regions of a floorplan),
// with every boundary crossed by exactly one registered, credited hop.
//
// The arrays, engines, edges and fan-in are the Xache's (kx_carray,
// kx_rd_pipe, kx_wr_engine, kx_perm, kx_link); what changes is how a
// (master, home) pair that sits in two partitions meets:
//   - master m has an AR lane and an AW/W lane in each direction, home h an
//     R/B lane in each direction (kx_lane over kx_hop). A lane is tapped at
//     every partition it passes; the tap at home h's partition IS that
//     home's request slot for master m, the tap at master m's partition its
//     response source from home h. Nothing is muxed in transit, so every pair
//     keeps its own path and the crossbar's bandwidth holds at every boundary.
//   - a pair in one partition is wires, exactly as in kx_xache. P = 1 is the
//     one-partition fabric.
//   - W beats follow their AW on the same lane, so a W never waits for its AW.
//   - nothing downstream of an engine ever waits: a read reserves a slot and
//     its beats in the master's reorder ring at the AR, so R beats from any
//     home land at once and drain in issue order; a write takes a slot that
//     routes its W beats and orders its B. A response held at one home for a
//     master that waits on another home deadlocks once lane latencies differ,
//     so no turn is kept anywhere.
// Streaming read engine, one engine per home on both sides. Every register is
// on its partition's reset (rstn_p[]); a hop's halves take the resets of the
// two partitions they sit in.

`default_nettype none

module kx_pxache #(
    parameter integer P         = 4,
    parameter integer M         = 4,
    parameter integer N_HOME    = 4,
    parameter integer PW        = (P <= 1) ? 1 : $clog2(P),
    parameter [M*PW-1:0]      MP = {M*PW{1'b0}},   // partition of master m
    parameter [N_HOME*PW-1:0] HP = {N_HOME*PW{1'b0}}, // partition of home h
    parameter integer AW        = 40,
    parameter integer W         = 512,
    parameter integer ID_W      = 4,
    parameter integer HOME_LSB  = 32,
    // The shipped array: 16,384 sets of K=2 words = the same 8 MB as 32,768 x 1.
    // K=2 with write lanes (kx_carray LANE_W) at one bank is a 4-deep chain,
    // ROUTED per SLR at WNS +0.020 / 300 MHz: 11,788 LUT synth, 11,288 routed,
    // 240 URAM, one cycle less hit latency than K=1 BANKS=2 (12,816 / 256).
    parameter integer SETS      = 16384,
    parameter integer SET_W     = 14,
    parameter integer K         = 2,
    parameter         RAM_STYLE = "ultra",
    // BANKS and K both set the URAM chain depth, SETS/BANKS/4096 -- the one axis
    // the device constrains (kx_carray.v, UG573/UG901). SYNTH at P=4, W=512,
    // SETS=32768, one run each: K=1 at 8/4/2/1 banks 16,092 / 13,916 / 12,816 /
    // 10,659 LUT; K=2 without write lanes 13,772 (1 bank), 15,868 (2). Synth
    // Fmax was 368.3 at every depth but the 8-deep 329.6, yet the 8-deep chain
    // routed at UG949 congestion level 6: depth is settled by the routed run
    // (scripts/tcl/impl_pxache.tcl), not by synth.
    parameter integer BANKS     = 1,        // kx_carray banks; 1 = one array
    parameter integer RING_WR_REG = 1,      // register the reorder ring's write port
    parameter integer ARR_WP_REG  = 0,      // kx_carray's write-port bundle registered
    parameter integer ARR_LAT     = 0,      // kx_carray's primitive read latency; 0 = URAM 4 / else 1
    parameter integer LANE_W      = 8,      // kx_carray's write lane at K > 1: 8 or 9
    parameter [M-1:0]        MCDC = {M{1'b0}},
    parameter [N_HOME-1:0]   HCDC = {N_HOME{1'b0}},
    parameter integer CDC_DEPTH = 16,
    parameter integer NSWAP     = 0,
    parameter [((NSWAP < 1) ? 1 : NSWAP)*8-1:0] SWAP_A = 0,
    parameter [((NSWAP < 1) ? 1 : NSWAP)*8-1:0] SWAP_B = 0,
    // bursts a master may hold outstanding: read slots (a 4 KB page of beats
    // each in its reorder ring) and write slots (B order)
    parameter integer RD_OUTQ   = 4,
    parameter integer WR_OUTQ   = 4,
    parameter integer HOP_DEPTH = 16,
    parameter         HOP_BUF   = "lean",
    parameter integer HOP_RXREG = 0,        // 1: a flop before each landing RAM (+1 cycle per hop)
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
    input  wire [P-1:0]            rstn_p,     // one reset per partition
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
    genvar m, h, p, e;
    localparam integer NDH  = 1 << HIDX_W;              // destinations on a request lane
    localparam integer NDM  = 1 << MIDX_W;              // destinations on a response lane
    localparam integer SEQW = (RD_OUTQ <= 1) ? 1 : $clog2(RD_OUTQ);   // a read's slot
    // lane payloads. AR: {slot, id, addr, len, size}; AW: {id, addr, len, size}.
    // AW/W: kind + the wider of an AW header and a W beat. R/B: kind + {slot,
    // id, resp, last, data}.
    localparam integer AWQW = IDW + AW + 8 + 3;
    localparam integer ARQW = SEQW + AWQW;
    localparam integer WPL  = WW;                       // W beat {data, strb, last}
    localparam integer AWWW = WPL + 1;                  // kind (1 = W beat) on top
    localparam integer RNW  = SEQW + ID_W + 2 + 1;      // {slot, id, resp, last}
    localparam integer RBW  = 1 + RNW + W;              // kind (1 = B) on top
    // a read slot holds a 4 KB page of beats: AXI never lets a burst cross one
    localparam integer PGB  = 4096 / STRB;
    localparam integer PBA  = $clog2(PGB);
    localparam integer RBUF_DEPTH = RD_OUTQ * PGB;

    // ---- partition of a master / home, as constants and by wire index -----
    function integer f_mp(input integer mm);
        f_mp = MP[mm*PW +: PW];
    endfunction
    function integer f_hp(input integer hh);
        f_hp = HP[hh*PW +: PW];
    endfunction
    // TAKE tables: the lane of master mm going up (dir 1) or down (dir 0)
    // consumes destination d (a home) at tap t iff home d sits in the tap's
    // partition; the mirror for a home's response lane and master destinations
    function [P*NDH-1:0] f_take_req(input integer mm, input integer dir);
        integer t, d, tp;
        begin
            f_take_req = {P*NDH{1'b0}};
            for (t = 0; t < P; t = t + 1) begin
                tp = dir ? (f_mp(mm) + 1 + t) : (f_mp(mm) - 1 - t);
                for (d = 0; d < N_HOME; d = d + 1) begin
                    if (tp >= 0 && tp < P && f_hp(d) == tp) begin f_take_req[t*NDH + d] = 1'b1; end
                end
            end
        end
    endfunction
    function [P*NDM-1:0] f_take_rsp(input integer hh, input integer dir);
        integer t, d, tp;
        begin
            f_take_rsp = {P*NDM{1'b0}};
            for (t = 0; t < P; t = t + 1) begin
                tp = dir ? (f_hp(hh) + 1 + t) : (f_hp(hh) - 1 - t);
                for (d = 0; d < M; d = d + 1) begin
                    if (tp >= 0 && tp < P && f_mp(d) == tp) begin f_take_rsp[t*NDM + d] = 1'b1; end
                end
            end
        end
    endfunction

    // ======================= master edge: a crossing per port iff MCDC =======================
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
        localparam integer PM   = f_mp(m);
        wire rm    = rstn_p[PM];
        wire mclk  = MCDC[m] ? m_clk[m]  : clk;
        wire mrst  = MCDC[m] ? ~m_rstn[m] : ~rm;
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
            .wr_clk(clk), .wr_rst(~rm),
            .s_valid(x_rvalid[m]), .s_ready(x_rready[m]),
            .s_data({x_rid[m*ID_W +: ID_W], x_rdata[m*W +: W], x_rresp[m*2 +: 2], x_rlast[m]}),
            .rd_clk(mclk), .m_valid(s_rvalid[m]), .m_ready(s_rready[m]), .m_data(r_pk_o));
        assign {s_rid[m*ID_W +: ID_W], s_rdata[m*W +: W], s_rresp[m*2 +: 2], s_rlast[m]} = r_pk_o;
        wire [BW-1:0] b_pk_o;
        kx_link #(.WIDTH(BW), .SAME(SAME), .DEPTH(CDC_DEPTH)) u_b (
            .wr_clk(clk), .wr_rst(~rm),
            .s_valid(x_bvalid[m]), .s_ready(x_bready[m]),
            .s_data({x_bid[m*ID_W +: ID_W], x_bresp[m*2 +: 2]}),
            .rd_clk(mclk), .m_valid(s_bvalid[m]), .m_ready(s_bready[m]), .m_data(b_pk_o));
        assign {s_bid[m*ID_W +: ID_W], s_bresp[m*2 +: 2]} = b_pk_o;
    end endgenerate

    // ======================= DRAM edge: a crossing per home iff HCDC =======================
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
        localparam integer PH   = f_hp(h);
        wire rh   = rstn_p[PH];
        wire hclk = HCDC[h] ? h_clk[h]  : clk;
        wire hrst = HCDC[h] ? ~h_rstn[h] : ~rh;
        wire [DARW-1:0] aw_o, ar_o;
        kx_link #(.WIDTH(DARW), .SAME(SAME), .DEPTH(CDC_DEPTH)) u_aw (
            .wr_clk(clk), .wr_rst(~rh),
            .s_valid(y_awvalid[h]), .s_ready(y_awready[h]),
            .s_data({y_awid[h*IDW +: IDW], y_awaddr[h*AW +: AW], y_awlen[h*8 +: 8], y_awsize[h*3 +: 3], y_awburst[h*2 +: 2]}),
            .rd_clk(hclk), .m_valid(d_awvalid[h]), .m_ready(d_awready[h]), .m_data(aw_o));
        assign {d_awid[h*IDW +: IDW], d_awaddr[h*AW +: AW], d_awlen[h*8 +: 8], d_awsize[h*3 +: 3], d_awburst[h*2 +: 2]} = aw_o;
        kx_link #(.WIDTH(DARW), .SAME(SAME), .DEPTH(CDC_DEPTH)) u_ar (
            .wr_clk(clk), .wr_rst(~rh),
            .s_valid(y_arvalid[h]), .s_ready(y_arready[h]),
            .s_data({y_arid[h*IDW +: IDW], y_araddr[h*AW +: AW], y_arlen[h*8 +: 8], y_arsize[h*3 +: 3], y_arburst[h*2 +: 2]}),
            .rd_clk(hclk), .m_valid(d_arvalid[h]), .m_ready(d_arready[h]), .m_data(ar_o));
        assign {d_arid[h*IDW +: IDW], d_araddr[h*AW +: AW], d_arlen[h*8 +: 8], d_arsize[h*3 +: 3], d_arburst[h*2 +: 2]} = ar_o;
        wire [WW-1:0] w_o;
        kx_link #(.WIDTH(WW), .SAME(SAME), .DEPTH(CDC_DEPTH), .MEM("block")) u_w (
            .wr_clk(clk), .wr_rst(~rh),
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

    // ============================ per-master routing ============================
    wire [HIDX_W-1:0] rhome [0:M-1];
    wire [HIDX_W-1:0] whome [0:M-1];
    wire [M*IDW-1:0]  arid_ot, awid_ot;
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

    // ============================ slots and sources ============================
    // Home h's request slots for master m (the engines' per-master inputs):
    // valid/ready/data per (h, m); wires for a local pair, a lane tap otherwise.
    wire [M-1:0]        hq_ar_v [0:N_HOME-1];  wire [M-1:0]  hq_ar_r [0:N_HOME-1];
    wire [ARQW-1:0]     hq_ar_d [0:N_HOME-1][0:M-1];
    wire [M-1:0]        hq_aw_v [0:N_HOME-1];  wire [M-1:0]  hq_aw_r [0:N_HOME-1];
    wire [AWQW-1:0]     hq_aw_d [0:N_HOME-1][0:M-1];
    wire [M-1:0]        hq_w_v  [0:N_HOME-1];  wire [M-1:0]  hq_w_r  [0:N_HOME-1];
    wire [WPL-1:0]      hq_w_d  [0:N_HOME-1][0:M-1];
    // Master m's response sources from home h: R {slot, id, resp, last} + the
    // word, and B {id, resp}.
    wire [N_HOME-1:0]   rs_v [0:M-1];  wire [N_HOME-1:0] rs_r [0:M-1];
    wire [RNW-1:0]      rs_d [0:M-1][0:N_HOME-1];
    wire [W-1:0]        rs_w [0:M-1][0:N_HOME-1];
    wire [N_HOME-1:0]   bs_v [0:M-1];  wire [N_HOME-1:0] bs_r [0:M-1];
    wire [BW-1:0]       bs_d [0:M-1][0:N_HOME-1];
    // lane taps, keyed by (source, partition landed in); a partition with no
    // tap of that lane holds a zero valid
    wire [M*P-1:0]         ar_tv, ar_tr, aww_tv, aww_tr;
    wire [HIDX_W-1:0]      ar_tdst  [0:M*P-1];
    wire [ARQW-1:0]        ar_td    [0:M*P-1];
    wire [HIDX_W-1:0]      aww_tdst [0:M*P-1];
    wire [AWWW-1:0]        aww_td   [0:M*P-1];
    wire [N_HOME*P-1:0]    rb_tv, rb_tr;
    wire [MIDX_W-1:0]      rb_tdst  [0:N_HOME*P-1];
    wire [RBW-1:0]         rb_td    [0:N_HOME*P-1];
    // engine outputs per home, needed by the masters' sources
    wire [N_HOME-1:0]      r_val_h, r_rdy_h, r_last_h, b_val_h, b_rdy_h;
    wire [IDW-1:0]         r_id_h [0:N_HOME-1];
    wire [SEQW-1:0]        r_seq_h [0:N_HOME-1];
    wire [1:0]             r_resp_h [0:N_HOME-1];
    wire [IDW-1:0]         b_id_h [0:N_HOME-1];
    wire [1:0]             b_resp_h [0:N_HOME-1];
    wire [N_HOME*W-1:0]    c_word;

    // ============================ masters ============================
    generate for (m = 0; m < M; m = m + 1) begin : g_m
        localparam integer PM  = f_mp(m);
        localparam [PW-1:0] PMV = PM;
        localparam integer NTU = P - 1 - PM;          // taps above
        localparam integer NTD = PM;                  // taps below
        wire rm = rstn_p[PM];

        // ---- accepts. A read takes a slot (a page of the reorder ring); a write
        // takes a slot for its B and sends all its W beats before the next AW
        // is taken, so a lane never carries an AW ahead of the beats of the one
        // before it (a tap would hold that AW and wedge the beats behind it).
        // Slot rings allocate at wp and drain at dp, a bit wider than the index
        // so full and empty differ.
        wire ar_acc = x_arvalid[m] && x_arready[m];
        wire aw_acc = x_awvalid[m] && x_awready[m];
        wire wl_acc = x_wvalid[m] && x_wready[m] && x_wlast[m];
        wire b_out  = x_bvalid[m] && x_bready[m];
        wire [ID_W-1:0] arid_m = x_arid[m*ID_W +: ID_W];
        wire [ID_W-1:0] awid_m = x_awid[m*ID_W +: ID_W];
        localparam integer WQW = (WR_OUTQ <= 1) ? 1 : $clog2(WR_OUTQ);
        reg  [SEQW:0] rq_wp, rq_dp;
        wire [SEQW:0] rq_used  = rq_wp - rq_dp;
        wire [PBA:0]  ar_beats = x_arlen[m*8 +: 8] + 1'b1;
        wire rd_ok = (rq_used != RD_OUTQ[SEQW:0]);
        reg  [WQW:0] wq_wp, wq_dp;
        wire wr_ok = ((wq_wp - wq_dp) != WR_OUTQ[WQW:0]);
        // the home the W beats go to, latched at the AW
        reg              wh_v;
        reg [HIDX_W-1:0] wh_q;
        always @(posedge clk) begin
            if (!rm) begin wh_v <= 1'b0; wh_q <= {HIDX_W{1'b0}}; end
            else if (aw_acc) begin wh_v <= 1'b1; wh_q <= whome[m]; end
            else if (wl_acc) begin wh_v <= 1'b0; end
        end
`ifndef SYNTHESIS
        always @(posedge clk) begin
            if (rm && ar_acc && (x_arlen[m*8 +: 8] >= PGB)) begin
                $display("%0t ERROR kx_pxache: master %0d burst of %0d beats exceeds a page of %0d", $time, m, x_arlen[m*8 +: 8] + 1, PGB);
            end
        end
`endif

        // ---- where the request goes
        wire [PW-1:0] ar_tp = HP[rhome[m]*PW +: PW];
        wire [PW-1:0] aw_tp = HP[whome[m]*PW +: PW];
        wire ar_loc = (ar_tp == PMV);
        wire ar_up  = (ar_tp >  PMV);
        wire aw_loc = (aw_tp == PMV);
        wire aw_up  = (aw_tp >  PMV);
        wire [PW-1:0] w_tp = HP[wh_q*PW +: PW];
        wire w_loc = (w_tp == PMV);
        wire w_up  = (w_tp >  PMV);

        wire [ARQW-1:0] ar_pay = {rq_wp[SEQW-1:0], arid_ot[m*IDW +: IDW], p_araddr[m*AW +: AW], x_arlen[m*8 +: 8], x_arsize[m*3 +: 3]};
        wire [AWQW-1:0] aw_pay = {awid_ot[m*IDW +: IDW], p_awaddr[m*AW +: AW], x_awlen[m*8 +: 8], x_awsize[m*3 +: 3]};
        wire [WPL-1:0]  w_pay  = {x_wdata[m*W +: W], x_wstrb[m*STRB +: STRB], x_wlast[m]};
        // one AW/W flit vector: while a write's beats are pending only W beats
        // go out (kind = wh_v), else only its AW; the W beat's upper bits ride
        // along under an AW header, so only the header's width is muxed
        wire [AWWW-1:0]   aww_flit = {wh_v, w_pay[WPL-1:AWQW], (wh_v ? w_pay[AWQW-1:0] : aw_pay)};
        wire [HIDX_W-1:0] aww_dst  = wh_v ? wh_q : whome[m];

        // ---- local slots: wires into the homes of this partition
        for (h = 0; h < N_HOME; h = h + 1) begin : g_loc
            if (f_hp(h) == PM) begin : g_yes
                assign hq_ar_v[h][m] = x_arvalid[m] && rd_ok && (rhome[m] == h[HIDX_W-1:0]);
                assign hq_ar_d[h][m] = ar_pay;
                assign hq_aw_v[h][m] = x_awvalid[m] && wr_ok && !wh_v && (whome[m] == h[HIDX_W-1:0]);
                assign hq_aw_d[h][m] = aw_pay;
                assign hq_w_v[h][m]  = x_wvalid[m] && wh_v && (wh_q == h[HIDX_W-1:0]);
                assign hq_w_d[h][m]  = w_pay;
            end
        end

        // ---- lanes: AR and AW/W, up and down
        wire up_ar_r, dn_ar_r, up_aww_r, dn_aww_r;
        wire up_ar_v  = x_arvalid[m] && rd_ok && ar_up;
        wire dn_ar_v  = x_arvalid[m] && rd_ok && !ar_up && !ar_loc;
        wire up_aw_v  = x_awvalid[m] && wr_ok && !wh_v && aw_up;
        wire dn_aw_v  = x_awvalid[m] && wr_ok && !wh_v && !aw_up && !aw_loc;
        wire up_w_v   = x_wvalid[m] && wh_v && w_up;
        wire dn_w_v   = x_wvalid[m] && wh_v && !w_up && !w_loc;
        if (NTU > 0) begin : g_up
            wire [NTU-1:0] rst_t;
            for (p = 0; p < NTU; p = p + 1) begin : g_r
                assign rst_t[p] = rstn_p[PM + 1 + p];
            end
            wire [NTU-1:0]        tv_ar, tr_ar, tv_aww, tr_aww;
            wire [NTU*HIDX_W-1:0] td_ar_dst, td_aww_dst;
            wire [NTU*ARQW-1:0]   td_ar;
            wire [NTU*AWWW-1:0]   td_aww;
            kx_lane #(.W(ARQW), .DW(HIDX_W), .NT(NTU), .TAKE(f_take_req(m, 1)),
                      .DEPTH(HOP_DEPTH), .MEM("block"), .BUF(HOP_BUF), .RX_REG(HOP_RXREG))u_ar (
                .clk(clk), .rstn_s(rm), .rstn_t(rst_t),
                .s_valid(up_ar_v), .s_ready(up_ar_r), .s_dst(rhome[m]), .s_data(ar_pay),
                .t_valid(tv_ar), .t_ready(tr_ar), .t_dst(td_ar_dst), .t_data(td_ar));
            kx_lane #(.W(AWWW), .DW(HIDX_W), .NT(NTU), .TAKE(f_take_req(m, 1)),
                      .DEPTH(HOP_DEPTH), .MEM("block"), .BUF(HOP_BUF), .RX_REG(HOP_RXREG))u_aww (
                .clk(clk), .rstn_s(rm), .rstn_t(rst_t),
                .s_valid(up_aw_v || up_w_v), .s_ready(up_aww_r),
                .s_dst(aww_dst), .s_data(aww_flit),
                .t_valid(tv_aww), .t_ready(tr_aww), .t_dst(td_aww_dst), .t_data(td_aww));
            for (p = 0; p < NTU; p = p + 1) begin : g_t
                assign ar_tv[m*P + PM + 1 + p]    = tv_ar[p];
                assign tr_ar[p]                   = ar_tr[m*P + PM + 1 + p];
                assign ar_tdst[m*P + PM + 1 + p]  = td_ar_dst[p*HIDX_W +: HIDX_W];
                assign ar_td[m*P + PM + 1 + p]    = td_ar[p*ARQW +: ARQW];
                assign aww_tv[m*P + PM + 1 + p]   = tv_aww[p];
                assign tr_aww[p]                  = aww_tr[m*P + PM + 1 + p];
                assign aww_tdst[m*P + PM + 1 + p] = td_aww_dst[p*HIDX_W +: HIDX_W];
                assign aww_td[m*P + PM + 1 + p]   = td_aww[p*AWWW +: AWWW];
            end
        end else begin : g_noup
            assign up_ar_r = 1'b0; assign up_aww_r = 1'b0;
        end
        if (NTD > 0) begin : g_dn
            wire [NTD-1:0] rst_t;
            for (p = 0; p < NTD; p = p + 1) begin : g_r
                assign rst_t[p] = rstn_p[PM - 1 - p];
            end
            wire [NTD-1:0]        tv_ar, tr_ar, tv_aww, tr_aww;
            wire [NTD*HIDX_W-1:0] td_ar_dst, td_aww_dst;
            wire [NTD*ARQW-1:0]   td_ar;
            wire [NTD*AWWW-1:0]   td_aww;
            kx_lane #(.W(ARQW), .DW(HIDX_W), .NT(NTD), .TAKE(f_take_req(m, 0)),
                      .DEPTH(HOP_DEPTH), .MEM("block"), .BUF(HOP_BUF), .RX_REG(HOP_RXREG))u_ar (
                .clk(clk), .rstn_s(rm), .rstn_t(rst_t),
                .s_valid(dn_ar_v), .s_ready(dn_ar_r), .s_dst(rhome[m]), .s_data(ar_pay),
                .t_valid(tv_ar), .t_ready(tr_ar), .t_dst(td_ar_dst), .t_data(td_ar));
            kx_lane #(.W(AWWW), .DW(HIDX_W), .NT(NTD), .TAKE(f_take_req(m, 0)),
                      .DEPTH(HOP_DEPTH), .MEM("block"), .BUF(HOP_BUF), .RX_REG(HOP_RXREG))u_aww (
                .clk(clk), .rstn_s(rm), .rstn_t(rst_t),
                .s_valid(dn_aw_v || dn_w_v), .s_ready(dn_aww_r),
                .s_dst(aww_dst), .s_data(aww_flit),
                .t_valid(tv_aww), .t_ready(tr_aww), .t_dst(td_aww_dst), .t_data(td_aww));
            for (p = 0; p < NTD; p = p + 1) begin : g_t
                assign ar_tv[m*P + PM - 1 - p]    = tv_ar[p];
                assign tr_ar[p]                   = ar_tr[m*P + PM - 1 - p];
                assign ar_tdst[m*P + PM - 1 - p]  = td_ar_dst[p*HIDX_W +: HIDX_W];
                assign ar_td[m*P + PM - 1 - p]    = td_ar[p*ARQW +: ARQW];
                assign aww_tv[m*P + PM - 1 - p]   = tv_aww[p];
                assign tr_aww[p]                  = aww_tr[m*P + PM - 1 - p];
                assign aww_tdst[m*P + PM - 1 - p] = td_aww_dst[p*HIDX_W +: HIDX_W];
                assign aww_td[m*P + PM - 1 - p]   = td_aww[p*AWWW +: AWWW];
            end
        end else begin : g_nodn
            assign dn_ar_r = 1'b0; assign dn_aww_r = 1'b0;
        end
        // this master's own partition has no tap of its own lanes
        assign ar_tv[m*P + PM]  = 1'b0;
        assign ar_tdst[m*P + PM] = {HIDX_W{1'b0}};
        assign ar_td[m*P + PM]   = {ARQW{1'b0}};
        assign aww_tv[m*P + PM] = 1'b0;
        assign aww_tdst[m*P + PM] = {HIDX_W{1'b0}};
        assign aww_td[m*P + PM]   = {AWWW{1'b0}};

        // ---- readies back to the master: the homes of this partition only,
        // decided at elaboration, so no path leaves the partition unregistered
        wire [N_HOME-1:0] lar_h, law_h, lw_h;
        for (h = 0; h < N_HOME; h = h + 1) begin : g_lr
            if (f_hp(h) == PM) begin : g_yes
                assign lar_h[h] = hq_ar_r[h][m] && (rhome[m] == h[HIDX_W-1:0]);
                assign law_h[h] = hq_aw_r[h][m] && (whome[m] == h[HIDX_W-1:0]);
                assign lw_h[h]  = hq_w_r[h][m]  && (wh_q     == h[HIDX_W-1:0]);
            end else begin : g_no
                assign lar_h[h] = 1'b0;  assign law_h[h] = 1'b0;  assign lw_h[h] = 1'b0;
            end
        end
        assign x_arready[m] = rd_ok && (ar_loc ? (|lar_h) : (ar_up ? up_ar_r : dn_ar_r));
        assign x_awready[m] = wr_ok && !wh_v && (aw_loc ? (|law_h) : (aw_up ? up_aww_r : dn_aww_r));
        assign x_wready[m]  = wh_v  && (w_loc  ? (|lw_h)  : (w_up  ? up_aww_r : dn_aww_r));

        // ---- response sources: the taps of every remote home's lane here
        for (h = 0; h < N_HOME; h = h + 1) begin : g_src
            localparam integer PH = f_hp(h);
            if (PH != PM) begin : g_tap
                wire              tv   = rb_tv[h*P + PM];
                wire [RBW-1:0]    td   = rb_td[h*P + PM];
                wire [MIDX_W-1:0] tdst = rb_tdst[h*P + PM];
                wire              kind = td[RBW-1];
                wire              mine = (tdst == m[MIDX_W-1:0]);
                assign rs_v[m][h] = tv && !kind && mine;
                assign rs_d[m][h] = td[W +: RNW];
                assign rs_w[m][h] = td[W-1:0];
                assign bs_v[m][h] = tv && kind && mine;
                assign bs_d[m][h] = td[W+1 +: BW];
            end
        end

        // ---- R: the reorder ring. A source's beat lands at its slot's next
        // address, whatever order the homes answer in. The pick prefers the
        // home of the slot being drained, else the lowest valid: a plain
        // lowest-valid pick let a nearer home land ahead of the drain, which
        // then idled and ran a tail (rd_1m 1224 vs 1044). It is COMBINATIONAL:
        // an engine's lookahead is `room = accept || !r_val`, and a ready a
        // cycle late ran it at a beat per three cycles (hit-32 102 vs 39).
        wire [N_HOME-1:0] rsv  = rs_v[m];
        wire [N_HOME-1:0] riso = rsv & (~rsv + 1'b1);
        reg  [HIDX_W-1:0] rlow; integer pk;
        always @(*) begin
            rlow = {HIDX_W{1'b0}};
            for (pk = 0; pk < N_HOME; pk = pk + 1) begin
                if (riso[pk]) begin rlow = rlow | pk[HIDX_W-1:0]; end
            end
        end
        // read slots: home, beats landed, burst length, id; a slot's beats sit
        // at {slot, beat} in the ring, so no address is ever added
        wire [HIDX_W-1:0] rq_home_v [0:RD_OUTQ-1];
        wire [PBA:0]      rq_wc_v   [0:RD_OUTQ-1];
        wire [PBA:0]      rq_wcv_v  [0:RD_OUTQ-1];
        wire [PBA:0]      rq_nb_v   [0:RD_OUTQ-1];
        wire [ID_W-1:0]   rq_id_v   [0:RD_OUTQ-1];
        wire [SEQW-1:0]   dslot = rq_dp[SEQW-1:0];
        wire [HIDX_W-1:0] dhome = rq_home_v[dslot];
        wire              dpref = rsv[dhome];
        wire [HIDX_W-1:0] rpick = dpref ? dhome : rlow;
        for (h = 0; h < N_HOME; h = h + 1) begin : g_rr
            assign rs_r[m][h] = dpref ? (dhome == h[HIDX_W-1:0]) : riso[h];
        end
        wire [RNW-1:0]  ld_d  = rs_d[m][rpick];         // {slot, id, resp, last}
        wire [W-1:0]    ld_w  = rs_w[m][rpick];
        wire [SEQW-1:0] ld_s  = ld_d[RNW-1 -: SEQW];
        wire            ld_go = |rsv;

        // RING_WR_REG registers ONLY the ring's write port, so the K:1 landing
        // mux drives a flop instead of the port and its address arithmetic.
        // `rs_r` -- the ready back to the sources -- is untouched: a ready one
        // cycle late ran the engine at a beat per three cycles (hit-32 102 vs
        // 39), which is why the pick is combinational in the first place.
        wire                 wr_go;
        wire [SEQW+PBA-1:0]  wr_addr;
        wire [W+2:0]         wr_data;
        wire                 vis_go;    // what advances the count the drain sees
        wire [SEQW-1:0]      vis_s;
        wire [SEQW+PBA-1:0]  ld_addr = {ld_s, rq_wc_v[ld_s][PBA-1:0]};

        if (RING_WR_REG == 0) begin : g_rw_comb
            assign wr_go   = ld_go;
            assign wr_addr = ld_addr;
            assign wr_data = {ld_d[2:0], ld_w};
            assign vis_go  = ld_go;
            assign vis_s   = ld_s;
        end else begin : g_rw_reg
            reg                go_q;
            reg [SEQW+PBA-1:0] a_q;
            reg [W+2:0]        d_q;
            reg [SEQW-1:0]     s_q;
            always @(posedge clk) begin
                if (!rm) begin
                    go_q <= 1'b0;
                end else begin
                    go_q <= ld_go;
                end
                if (ld_go) begin
                    a_q <= ld_addr;
                    d_q <= {ld_d[2:0], ld_w};
                    s_q <= ld_s;
                end
            end
            assign wr_go   = go_q;
            assign wr_addr = a_q;
            assign wr_data = d_q;
            assign vis_go  = go_q;
            assign vis_s   = s_q;
        end

        for (e = 0; e < RD_OUTQ; e = e + 1) begin : g_rq
            reg [HIDX_W-1:0] home;  reg [PBA:0] wc, wcv, nb;  reg [ID_W-1:0] id;
            wire alloc = ar_acc && (rq_wp[SEQW-1:0] == e[SEQW-1:0]);
            // `wc` allocates the address, so it must advance the cycle the mux
            // picks; `wcv` is what the drain may read, so it advances with the
            // write. They are the same signal when the port is not registered.
            wire land  = ld_go  && (ld_s  == e[SEQW-1:0]);
            wire landv = vis_go && (vis_s == e[SEQW-1:0]);
            always @(posedge clk) begin
                if (!rm) begin
                    home <= {HIDX_W{1'b0}}; wc <= {(PBA+1){1'b0}};
                    wcv <= {(PBA+1){1'b0}}; nb <= {(PBA+1){1'b0}}; id <= {ID_W{1'b0}};
                end else if (alloc) begin
                    home <= rhome[m]; wc <= {(PBA+1){1'b0}};
                    wcv <= {(PBA+1){1'b0}}; nb <= ar_beats; id <= arid_m;
                end else begin
                    if (land) begin
                        wc <= wc + 1'b1;
                    end
                    if (landv) begin
                        wcv <= wcv + 1'b1;
                    end
                end
            end
            assign rq_home_v[e] = home;  assign rq_wc_v[e]  = wc;
            assign rq_wcv_v[e]  = wcv;
            assign rq_nb_v[e]   = nb;    assign rq_id_v[e] = id;
        end
        // the drain: the oldest slot, beat by beat as its beats land
        reg  [PBA:0]    rc;                             // beats of the drain slot read out
        reg             o_v;
        // the id travels with the beat: the slot frees when its last beat is
        // ISSUED, and a new AR re-owned it under that beat during a stall
        reg  [ID_W-1:0] o_id;
        wire            o_take   = o_v && x_rready[m];
        wire            readable = (rq_used != {(SEQW+1){1'b0}}) && (rc != rq_wcv_v[dslot]);
        wire            issue    = readable && (!o_v || o_take);
        wire            d_last   = ((rc + 1'b1) == rq_nb_v[dslot]);
        wire [W+2:0]    o_d;                            // {resp, last, data}
        kohaku_sdpram #(.WIDTH(W+3), .DEPTH(RBUF_DEPTH), .MEM_PRIM("block"), .READ_LAT(1)) u_rb (
            .clk(clk),
            .wr_en(wr_go), .wr_addr(wr_addr), .wr_data(wr_data),
            .rd_en(issue), .rd_addr({dslot, rc[PBA-1:0]}), .rd_data(o_d));
        always @(posedge clk) begin
            if (!rm) begin
                rq_wp <= 0; rq_dp <= 0; rc <= 0; o_v <= 1'b0; o_id <= {ID_W{1'b0}};
            end else begin
                if (ar_acc) begin rq_wp <= rq_wp + 1'b1; end
                if (issue) begin
                    o_id <= rq_id_v[dslot];
                    if (d_last) begin rq_dp <= rq_dp + 1'b1; rc <= 0; end
                    else begin rc <= rc + 1'b1; end
                end
                o_v <= issue || (o_v && !o_take);
            end
        end
        assign x_rvalid[m]           = o_v;
        assign x_rid[m*ID_W +: ID_W] = o_id;
        assign x_rresp[m*2 +: 2]     = o_d[W+2 -: 2];
        assign x_rlast[m]            = o_d[W];
        assign x_rdata[m*W +: W]     = o_d[W-1:0];

        // ---- write slots and B: a B lands at once (lowest valid source) and
        // completes the oldest open slot bound for its home (a home answers in
        // order); slots drain in issue order
        wire [N_HOME-1:0] bsv  = bs_v[m];
        wire [N_HOME-1:0] biso = bsv & (~bsv + 1'b1);
        reg  [HIDX_W-1:0] bidx; integer bk;
        always @(*) begin
            bidx = {HIDX_W{1'b0}};
            for (bk = 0; bk < N_HOME; bk = bk + 1) begin
                if (biso[bk]) begin bidx = bidx | bk[HIDX_W-1:0]; end
            end
        end
        for (h = 0; h < N_HOME; h = h + 1) begin : g_br
            assign bs_r[m][h] = biso[h];
        end
        wire               b_land = |bsv;
        wire [BW-1:0]      b_ld   = bs_d[m][bidx];
        wire [WR_OUTQ-1:0] wq_match;                    // open and bound for the B's home
        // the oldest match: rotate by dp, isolate the lowest, rotate back
        reg  [WR_OUTQ-1:0] wq_rot, wq_fill; integer wi;
        always @(*) begin
            for (wi = 0; wi < WR_OUTQ; wi = wi + 1) begin
                wq_rot[wi] = wq_match[(wi + wq_dp[WQW-1:0]) % WR_OUTQ];
            end
        end
        wire [WR_OUTQ-1:0] wq_riso = wq_rot & (~wq_rot + 1'b1);
        always @(*) begin
            for (wi = 0; wi < WR_OUTQ; wi = wi + 1) begin
                wq_fill[wi] = wq_riso[(wi + WR_OUTQ - wq_dp[WQW-1:0]) % WR_OUTQ];
            end
        end
        wire [ID_W-1:0]    wq_id_v   [0:WR_OUTQ-1];
        wire [1:0]         wq_resp_v [0:WR_OUTQ-1];
        wire [WR_OUTQ-1:0] wq_done;
        for (e = 0; e < WR_OUTQ; e = e + 1) begin : g_wq
            reg v, done;  reg [HIDX_W-1:0] home;  reg [ID_W-1:0] id;  reg [1:0] resp;
            wire alloc = aw_acc && (wq_wp[WQW-1:0] == e[WQW-1:0]);
            wire drain = b_out  && (wq_dp[WQW-1:0] == e[WQW-1:0]);
            always @(posedge clk) begin
                if (!rm) begin
                    v <= 1'b0; done <= 1'b0; home <= {HIDX_W{1'b0}}; id <= {ID_W{1'b0}}; resp <= 2'b00;
                end else begin
                    if (alloc) begin v <= 1'b1; done <= 1'b0; home <= whome[m]; id <= awid_m; end
                    else if (drain) begin v <= 1'b0; end
                    if (b_land && wq_fill[e]) begin done <= 1'b1; resp <= b_ld[1:0]; end
                end
            end
            assign wq_match[e]  = v && !done && (home == bidx);
            assign wq_id_v[e]   = id;  assign wq_resp_v[e] = resp;
            assign wq_done[e]   = v && done;
        end
        assign x_bvalid[m]           = wq_done[wq_dp[WQW-1:0]];
        assign x_bid[m*ID_W +: ID_W] = wq_id_v[wq_dp[WQW-1:0]];
        assign x_bresp[m*2 +: 2]     = wq_resp_v[wq_dp[WQW-1:0]];
        always @(posedge clk) begin
            if (!rm) begin wq_wp <= 0; wq_dp <= 0; end
            else begin
                if (aw_acc) begin wq_wp <= wq_wp + 1'b1; end
                if (b_out)  begin wq_dp <= wq_dp + 1'b1; end
            end
        end
    end endgenerate

    // ============================ homes ============================
    wire [N_HOME-1:0]          c_flush, c_rd_en, c_rd_take, c_land, c_hit_c, c_hit;
    wire [N_HOME-1:0]          c_fill_go, c_fill_ready, c_fill_done, c_wr_en, c_wr_full;
    wire [N_HOME*SET_W-1:0]    c_rd_idx, c_fill_idx, c_wr_idx;
    wire [N_HOME*TAG_W-1:0]    c_rd_tag, c_fill_tag, c_wr_tag;
    wire [N_HOME*(SUBW+1)-1:0] c_rd_sub;
    wire [N_HOME*W-1:0]        c_wr_word;

    generate for (h = 0; h < N_HOME; h = h + 1) begin : g_home
        localparam integer PH  = f_hp(h);
        localparam [PW-1:0] PHV = PH;
        localparam integer NTU = P - 1 - PH;
        localparam integer NTD = PH;
        wire rh = rstn_p[PH];

        // ---- remote request slots: the taps of every remote master's lanes here
        for (m = 0; m < M; m = m + 1) begin : g_slot
            localparam integer PM = f_mp(m);
            if (PM != PH) begin : g_tap
                wire            tv   = ar_tv[m*P + PH];
                wire [HIDX_W-1:0] tdst = ar_tdst[m*P + PH];
                assign hq_ar_v[h][m] = tv && (tdst == h[HIDX_W-1:0]);
                assign hq_ar_d[h][m] = ar_td[m*P + PH];
                wire            wv   = aww_tv[m*P + PH];
                wire [AWWW-1:0] wd   = aww_td[m*P + PH];
                wire [HIDX_W-1:0] wdst = aww_tdst[m*P + PH];
                wire            kind = wd[AWWW-1];
                assign hq_aw_v[h][m] = wv && !kind && (wdst == h[HIDX_W-1:0]);
                assign hq_aw_d[h][m] = wd[AWQW-1:0];
                assign hq_w_v[h][m]  = wv && kind && (wdst == h[HIDX_W-1:0]);
                assign hq_w_d[h][m]  = wd[WPL-1:0];
            end
        end

        // ---- the array
        kx_carray #(.AW(AW), .W(W), .SETS(SETS), .SET_W(SET_W), .K(K), .RAM_STYLE(RAM_STYLE),
                    .BANKS(BANKS), .WPORT_REG(ARR_WP_REG), .FILL_SERVE(0),
                    .ARR_LAT(ARR_LAT), .LANE_W(LANE_W)) u_c (
            .clk(clk), .resetn(rh),
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

        // ---- the read engine: one, over this home's M slots
        wire [M*IDW-1:0]  q_id;  wire [M*AW-1:0] q_addr;  wire [M*8-1:0] q_len;
        wire [M*SEQW-1:0] q_seq;
        wire [M*3-1:0]    q_size_unused;
        for (m = 0; m < M; m = m + 1) begin : g_q
            assign {q_seq[m*SEQW +: SEQW], q_id[m*IDW +: IDW], q_addr[m*AW +: AW], q_len[m*8 +: 8], q_size_unused[m*3 +: 3]} = hq_ar_d[h][m];
        end
        wire            r_val, r_rdy, r_last;
        wire [IDW-1:0]  r_id;
        wire [1:0]      r_resp;
        wire [SEQW-1:0] r_seq;
        wire            r_home_unused, r_hidx_unused;
        wire [IDW-1:0] e_arid; wire [AW-1:0] e_araddr; wire [7:0] e_arlen;
        wire [2:0] e_arsize;   wire [1:0] e_arburst;
        wire [SET_W-1:0] e_rd_idx, e_fl_idx; wire [TAG_W-1:0] e_rd_tag, e_fl_tag; wire [SUBW:0] e_rd_sub;
        kx_rd_pipe #(.M(M), .NH(1), .AW(AW), .W(W), .IDW(IDW), .SET_W(SET_W),
                     .K(K), .RAM_STYLE(RAM_STYLE), .BANKS(BANKS), .ARR_LAT(ARR_LAT), .SEQW(SEQW)) u_re (
            .clk(clk), .resetn(rh), .flush_busy(c_flush[h]),
            .mr_qval(hq_ar_v[h]), .mr_qrdy(hq_ar_r[h]), .mr_qid(q_id), .mr_qaddr(q_addr),
            .mr_qlen(q_len), .mr_qseq(q_seq),
            .r_val(r_val), .r_rdy(r_rdy), .r_id(r_id), .r_resp(r_resp), .r_last(r_last),
            .r_home(r_home_unused), .r_hidx(r_hidx_unused), .r_seq(r_seq),
            .c_rd_en(c_rd_en[h]), .c_rd_idx(e_rd_idx), .c_rd_tag(e_rd_tag),
            .c_rd_sub(e_rd_sub), .c_rd_take(c_rd_take[h]),
            .c_land(c_land[h]), .c_hit_c(c_hit_c[h]),
            .c_fill_go(c_fill_go[h]), .c_fill_idx(e_fl_idx), .c_fill_tag(e_fl_tag),
            .c_fill_ready(c_fill_ready[h]), .c_fill_done(c_fill_done[h]),
            .m_arid(e_arid), .m_araddr(e_araddr), .m_arlen(e_arlen), .m_arsize(e_arsize),
            .m_arburst(e_arburst), .m_arvalid(y_arvalid[h]), .m_arready(y_arready[h]),
            .m_rvalid(y_rvalid[h]), .m_rlast(y_rlast[h]),
            .m_rresp(y_rresp[h*2 +: 2]), .m_rready(y_rready[h]));
        assign y_arid[h*IDW +: IDW] = e_arid;   assign y_araddr[h*AW +: AW] = e_araddr;
        assign y_arlen[h*8 +: 8] = e_arlen;     assign y_arsize[h*3 +: 3] = e_arsize;
        assign y_arburst[h*2 +: 2] = e_arburst;
        assign c_rd_idx[h*SET_W +: SET_W] = e_rd_idx;
        assign c_rd_tag[h*TAG_W +: TAG_W] = e_rd_tag;
        assign c_rd_sub[h*(SUBW+1) +: SUBW+1] = e_rd_sub;
        assign c_fill_idx[h*SET_W +: SET_W] = e_fl_idx;
        assign c_fill_tag[h*TAG_W +: TAG_W] = e_fl_tag;
        assign r_val_h[h] = r_val;  assign r_id_h[h] = r_id;  assign r_resp_h[h] = r_resp;
        assign r_last_h[h] = r_last;  assign r_seq_h[h] = r_seq;
        assign r_rdy = r_rdy_h[h];

        // ---- the write engine: one, over this home's M AW and W slots
        wire [M*IDW-1:0]  wq_id;  wire [M*AW-1:0] wq_addr;  wire [M*8-1:0] wq_len; wire [M*3-1:0] wq_size;
        wire [M-1:0]      w_last_m;
        for (m = 0; m < M; m = m + 1) begin : g_wq
            assign {wq_id[m*IDW +: IDW], wq_addr[m*AW +: AW], wq_len[m*8 +: 8], wq_size[m*3 +: 3]} = hq_aw_d[h][m];
            assign w_last_m[m] = hq_w_d[h][m][0];
        end
        wire [M-1:0]      gsel;
        wire [MIDX_W-1:0] gidx;
        wire              hsel_unused;
        wire            b_val, b_rdy;
        wire [IDW-1:0]  b_id;
        wire [1:0]      b_resp;
        wire [IDW-1:0] e_awid; wire [AW-1:0] e_awaddr; wire [7:0] e_awlen;
        wire [2:0] e_awsize;   wire [1:0] e_awburst;
        wire [SET_W-1:0] e_wr_idx; wire [TAG_W-1:0] e_wr_tag;
        kx_wr_engine #(.M(M), .NH(1), .AW(AW), .W(W), .IDW(IDW), .SET_W(SET_W), .K(K)) u_we (
            .clk(clk), .resetn(rh), .flush_busy(c_flush[h]),
            .mw_qval(hq_aw_v[h]), .mw_qrdy(hq_aw_r[h]), .mw_qid(wq_id), .mw_qaddr(wq_addr),
            .mw_qlen(wq_len), .mw_qsize(wq_size),
            .mw_wval(hq_w_v[h]), .mw_wrdy(hq_w_r[h]), .mw_wlast(w_last_m),
            .gsel(gsel), .gidx(gidx), .hsel(hsel_unused),
            .b_val(b_val), .b_rdy(b_rdy), .b_id(b_id), .b_resp(b_resp),
            .c_wr_en(c_wr_en[h]), .c_wr_idx(e_wr_idx), .c_wr_tag(e_wr_tag),
            .m_awid(e_awid), .m_awaddr(e_awaddr), .m_awlen(e_awlen), .m_awsize(e_awsize),
            .m_awburst(e_awburst), .m_awvalid(y_awvalid[h]), .m_awready(y_awready[h]),
            .m_wvalid(y_wvalid[h]), .m_wready(y_wready[h]),
            .m_bresp(y_bresp[h*2 +: 2]), .m_bvalid(y_bvalid[h]), .m_bready(y_bready[h]));
        assign y_awid[h*IDW +: IDW] = e_awid;   assign y_awaddr[h*AW +: AW] = e_awaddr;
        assign y_awlen[h*8 +: 8] = e_awlen;     assign y_awsize[h*3 +: 3] = e_awsize;
        assign y_awburst[h*2 +: 2] = e_awburst;
        assign y_wlast[h] = |(w_last_m & gsel);
        assign c_wr_idx[h*SET_W +: SET_W] = e_wr_idx;
        assign c_wr_tag[h*TAG_W +: TAG_W] = e_wr_tag;
        assign b_val_h[h] = b_val;  assign b_id_h[h] = b_id;  assign b_resp_h[h] = b_resp;
        assign b_rdy = b_rdy_h[h];
        // W data: an M:1 mux on the engine's REGISTERED master index, over the
        // slots' {data, strb} -- a master's own beat or its lane's landed beat
        wire [WPL-1:0] wsel = hq_w_d[h][gidx];
        assign y_wdata[h*W +: W]       = wsel[WPL-1 -: W];
        assign y_wstrb[h*STRB +: STRB] = wsel[1 +: STRB];
        assign c_wr_word[h*W +: W]     = wsel[WPL-1 -: W];
        assign c_wr_full[h]            = &wsel[1 +: STRB];

        // ---- responses: local wires, or this home's lanes
        wire [MIDX_W-1:0] r_own = r_id[ID_W +: MIDX_W];
        wire [MIDX_W-1:0] b_own = b_id[ID_W +: MIDX_W];
        wire [PW-1:0] r_tp = MP[r_own*PW +: PW];
        wire [PW-1:0] b_tp = MP[b_own*PW +: PW];
        wire r_loc = (r_tp == PHV);  wire r_up = (r_tp > PHV);
        wire b_loc = (b_tp == PHV);  wire b_up = (b_tp > PHV);
        for (m = 0; m < M; m = m + 1) begin : g_lsrc
            if (f_mp(m) == PH) begin : g_yes
                assign rs_v[m][h] = r_val && r_loc && (r_own == m[MIDX_W-1:0]);
                assign rs_d[m][h] = {r_seq, r_id[ID_W-1:0], r_resp, r_last};
                assign rs_w[m][h] = c_word[h*W +: W];
                assign bs_v[m][h] = b_val && b_loc && (b_own == m[MIDX_W-1:0]);
                assign bs_d[m][h] = {b_id[ID_W-1:0], b_resp};
            end
        end
        // the owner's accept, over the masters of this partition only
        wire [M-1:0] rs_r_h, bs_r_h;
        for (m = 0; m < M; m = m + 1) begin : g_lrdy
            if (f_mp(m) == PH) begin : g_yes
                assign rs_r_h[m] = rs_r[m][h] && (r_own == m[MIDX_W-1:0]);
                assign bs_r_h[m] = bs_r[m][h] && (b_own == m[MIDX_W-1:0]);
            end else begin : g_no
                assign rs_r_h[m] = 1'b0;
                assign bs_r_h[m] = 1'b0;
            end
        end
        wire up_rb_r, dn_rb_r;
        wire up_b = b_val && b_up;                          // B first: one flit
        wire dn_b = b_val && !b_up && !b_loc;
        wire up_r = r_val && r_up && !up_b;
        wire dn_r = r_val && !r_up && !r_loc && !dn_b;
        // one flit vector per lane: the word rides on every flit (a B ignores
        // it), so only the header's width is muxed
        wire [RNW-1:0] rb_hdr_r = {r_seq, r_id[ID_W-1:0], r_resp, r_last};
        wire [RNW-1:0] rb_hdr_b = {{SEQW{1'b0}}, b_id[ID_W-1:0], b_resp, 1'b0};
        wire [RBW-1:0] rb_up = {up_b, (up_b ? rb_hdr_b : rb_hdr_r), c_word[h*W +: W]};
        wire [RBW-1:0] rb_dn = {dn_b, (dn_b ? rb_hdr_b : rb_hdr_r), c_word[h*W +: W]};
        if (NTU > 0) begin : g_up
            wire [NTU-1:0] rst_t;
            for (p = 0; p < NTU; p = p + 1) begin : g_r
                assign rst_t[p] = rstn_p[PH + 1 + p];
            end
            wire [NTU-1:0]        tv, tr;
            wire [NTU*MIDX_W-1:0] tdst;
            wire [NTU*RBW-1:0]    td;
            kx_lane #(.W(RBW), .DW(MIDX_W), .NT(NTU), .TAKE(f_take_rsp(h, 1)),
                      .DEPTH(HOP_DEPTH), .MEM("block"), .BUF(HOP_BUF), .RX_REG(HOP_RXREG))u_rb (
                .clk(clk), .rstn_s(rh), .rstn_t(rst_t),
                .s_valid(up_b || up_r), .s_ready(up_rb_r),
                .s_dst(up_b ? b_own : r_own), .s_data(rb_up),
                .t_valid(tv), .t_ready(tr), .t_dst(tdst), .t_data(td));
            for (p = 0; p < NTU; p = p + 1) begin : g_t
                assign rb_tv[h*P + PH + 1 + p]   = tv[p];
                assign tr[p]                     = rb_tr[h*P + PH + 1 + p];
                assign rb_tdst[h*P + PH + 1 + p] = tdst[p*MIDX_W +: MIDX_W];
                assign rb_td[h*P + PH + 1 + p]   = td[p*RBW +: RBW];
            end
        end else begin : g_noup
            assign up_rb_r = 1'b0;
        end
        if (NTD > 0) begin : g_dn
            wire [NTD-1:0] rst_t;
            for (p = 0; p < NTD; p = p + 1) begin : g_r
                assign rst_t[p] = rstn_p[PH - 1 - p];
            end
            wire [NTD-1:0]        tv, tr;
            wire [NTD*MIDX_W-1:0] tdst;
            wire [NTD*RBW-1:0]    td;
            kx_lane #(.W(RBW), .DW(MIDX_W), .NT(NTD), .TAKE(f_take_rsp(h, 0)),
                      .DEPTH(HOP_DEPTH), .MEM("block"), .BUF(HOP_BUF), .RX_REG(HOP_RXREG))u_rb (
                .clk(clk), .rstn_s(rh), .rstn_t(rst_t),
                .s_valid(dn_b || dn_r), .s_ready(dn_rb_r),
                .s_dst(dn_b ? b_own : r_own), .s_data(rb_dn),
                .t_valid(tv), .t_ready(tr), .t_dst(tdst), .t_data(td));
            for (p = 0; p < NTD; p = p + 1) begin : g_t
                assign rb_tv[h*P + PH - 1 - p]   = tv[p];
                assign tr[p]                     = rb_tr[h*P + PH - 1 - p];
                assign rb_tdst[h*P + PH - 1 - p] = tdst[p*MIDX_W +: MIDX_W];
                assign rb_td[h*P + PH - 1 - p]   = td[p*RBW +: RBW];
            end
        end else begin : g_nodn
            assign dn_rb_r = 1'b0;
        end
        assign rb_tv[h*P + PH]   = 1'b0;
        assign rb_tdst[h*P + PH] = {MIDX_W{1'b0}};
        assign rb_td[h*P + PH]   = {RBW{1'b0}};
        assign r_rdy_h[h] = r_loc ? (|rs_r_h) : (r_up ? (up_rb_r && !up_b) : (dn_rb_r && !dn_b));
        assign b_rdy_h[h] = b_loc ? (|bs_r_h) : (b_up ? up_rb_r : dn_rb_r);
    end endgenerate

    // ============================ tap readies ============================
    // A request tap answers with the slot ready of the home it addresses; a
    // response tap with the accept of the master it addresses, by flit kind.
    // Only the homes / masters OF THAT PARTITION are gathered, at elaboration.
    generate for (m = 0; m < M; m = m + 1) begin : g_trq
        for (p = 0; p < P; p = p + 1) begin : g_p
            if (p != f_mp(m)) begin : g_t
                wire [HIDX_W-1:0] ad = ar_tdst[m*P + p];
                wire [HIDX_W-1:0] wd = aww_tdst[m*P + p];
                wire              wk = aww_td[m*P + p][AWWW-1];
                wire [N_HOME-1:0] ar_h, aw_h, w_h;
                for (h = 0; h < N_HOME; h = h + 1) begin : g_h
                    if (f_hp(h) == p) begin : g_yes
                        assign ar_h[h] = hq_ar_r[h][m] && (ad == h[HIDX_W-1:0]);
                        assign aw_h[h] = hq_aw_r[h][m] && (wd == h[HIDX_W-1:0]);
                        assign w_h[h]  = hq_w_r[h][m]  && (wd == h[HIDX_W-1:0]);
                    end else begin : g_no
                        assign ar_h[h] = 1'b0;  assign aw_h[h] = 1'b0;  assign w_h[h] = 1'b0;
                    end
                end
                assign ar_tr[m*P + p]  = |ar_h;
                assign aww_tr[m*P + p] = wk ? (|w_h) : (|aw_h);
            end else begin : g_self
                assign ar_tr[m*P + p]  = 1'b0;
                assign aww_tr[m*P + p] = 1'b0;
            end
        end
    end endgenerate
    generate for (h = 0; h < N_HOME; h = h + 1) begin : g_trs
        for (p = 0; p < P; p = p + 1) begin : g_p
            if (p != f_hp(h)) begin : g_t
                wire [MIDX_W-1:0] md = rb_tdst[h*P + p];
                wire              mk = rb_td[h*P + p][RBW-1];
                wire [M-1:0] rr, br;
                for (m = 0; m < M; m = m + 1) begin : g_g
                    if (f_mp(m) == p) begin : g_yes
                        assign rr[m] = rs_r[m][h] && (md == m[MIDX_W-1:0]);
                        assign br[m] = bs_r[m][h] && (md == m[MIDX_W-1:0]);
                    end else begin : g_no
                        assign rr[m] = 1'b0;
                        assign br[m] = 1'b0;
                    end
                end
                assign rb_tr[h*P + p] = mk ? (|br) : (|rr);
            end else begin : g_self
                assign rb_tr[h*P + p] = 1'b0;
            end
        end
    end endgenerate
endmodule

`default_nettype wire

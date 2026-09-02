// kx_trunk -- one boundary crossing shared by N_CH hop channels: K slots per
// beat, a private ring + credit per channel (kx_hop's isolation on 1/N the
// wires). Wires one direction: K*(1 + CHW + SLOT) forward, N_CH + 1 back.

`default_nettype none

module kx_trunk #(
    parameter integer N_CH  = 4,
    parameter integer SLOT  = 592,
    parameter integer K     = 1,               // slots per beat
    parameter integer DEPTH = 16,              // ring per channel
    parameter         MEM   = "block",
    parameter         BUF   = "lean",
    parameter integer FASTW = 0,
    // channel c's real width (top-aligned in its slot); its ring stores only
    // that -- a 61-bit channel must not pay a 580-bit BRAM row
    parameter [N_CH*16-1:0] WVEC = {N_CH{16'd592}},
    // FUSE: each channel has TWO senders (s = forward, s2 = inject) and the
    // slot mux takes all of them in one AND-OR (a separate 2:1 ahead of it
    // costs ~950 LUT a boundary-direction). LOCK_CH's write bursts stay
    // atomic across the pair: slot[LK_TP+:2] 0 opens, 1 with slot[LK_WL]
    // closes.
    parameter integer FUSE    = 0,
    parameter integer LOCK_CH = 0,
    parameter integer LK_TP   = 0,
    parameter integer LK_WL   = 0,
    // 1: the landing ring IS the clock crossing -- everything up to and
    // including its write port runs on clk, the read side on m_clk. Credits
    // still govern the send (they are spent at grant, two registers before the
    // write, so a full flag cannot replace them); the pop count returns gray.
    parameter integer ASYNC = 0,
    parameter integer CHW   = (N_CH <= 1) ? 1 : $clog2(N_CH)
)(
    input  wire                  clk,           // sending partition's clock
    input  wire                  m_clk,         // receiving partition's; = clk at ASYNC 0
    input  wire                  s_rstn,        // sending partition's reset
    input  wire                  m_rstn,        // receiving partition's reset

    input  wire [N_CH-1:0]       s_valid,
    output wire [N_CH-1:0]       s_ready,
    input  wire [N_CH*SLOT-1:0]  s_data,
    input  wire [N_CH-1:0]       s2_valid,
    output wire [N_CH-1:0]       s2_ready,
    input  wire [N_CH*SLOT-1:0]  s2_data,

    output wire [N_CH-1:0]       m_valid,
    input  wire [N_CH-1:0]       m_ready,
    output wire [N_CH*SLOT-1:0]  m_data
);
    localparam integer CW = $clog2(DEPTH + 1);

    // ---- TX: per-channel credit, K slots picked round-robin ----------------
    wire [N_CH-1:0] ring_full;              // write domain, ASYNC only
    wire [N_CH-1:0] pp_w;                   // one pop, rebuilt in the send domain
    reg  [CW-1:0]   credit  [0:N_CH-1];
    reg  [N_CH-1:0] credit_nz;              // credit != 0, from the next state
    reg  [N_CH-1:0] pp_q;
    reg             fok_q;
    reg             lk_v, lk_m;
    reg  [N_CH-1:0] tg2;
    wire [N_CH-1:0] eligible, pick2;
    genvar c, k;
    generate for (c = 0; c < N_CH; c = c + 1) begin : g_el
        wire m0 = s_valid[c];
        wire m1 = (FUSE != 0) && s2_valid[c];
        wire lk_here = (FUSE != 0) && lk_v && (c == LOCK_CH);
        assign pick2[c] = lk_here ? lk_m : (m1 && (!m0 || tg2[c]));
        wire mv = lk_here ? (lk_m ? m1 : m0) : (m0 || m1);
        assign eligible[c] = credit_nz[c] && fok_q && mv;
    end endgenerate

    // grant up to K channels per beat: slot k takes the k-th eligible channel
    // at and after the round-robin pointer (wrap once)
    reg  [CHW-1:0]  rr;
    wire [N_CH-1:0]   grant [0:K-1];
    wire [CHW-1:0]    gch   [0:K-1];
    wire [K-1:0]      gv;
    generate for (k = 0; k < K; k = k + 1) begin : g_pick
        // the k-th set bit of elig2 (positions rr..rr+N_CH-1), as a one-hot
        // over channels; serial over K only (K is 1 or 2)
        wire [N_CH-1:0] prev_mask;
        if (k == 0) begin : g_p0
            assign prev_mask = {N_CH{1'b0}};
        end else begin : g_pk
            assign prev_mask = g_pick[k-1].taken_mask;
        end
        wire [N_CH-1:0] cand = eligible & ~prev_mask;
        wire [2*N_CH-1:0] c2 = {cand, cand} >> rr;
        wire [N_CH-1:0] c2n  = c2[N_CH-1:0];
        wire [N_CH-1:0] low  = c2n & (~c2n + 1'b1);     // lowest from rr
        integer i;
        reg [N_CH-1:0] oh;
        reg [CHW-1:0]  sel;
        always @(*) begin
            oh  = {N_CH{1'b0}};
            sel = {CHW{1'b0}};
            for (i = 0; i < N_CH; i = i + 1) begin
                if (low[i]) begin
                    oh  = {{(N_CH-1){1'b0}}, 1'b1} << ((i + rr) % N_CH);
                    sel = (i + rr) % N_CH;
                end
            end
        end
        wire [N_CH-1:0] taken_mask = prev_mask | oh;
        assign grant[k] = oh;
        assign gch[k]   = sel;
        assign gv[k]    = |cand;
    end endgenerate
    wire [N_CH-1:0] taken_all = g_pick[K-1].taken_mask;
    assign s_ready  = taken_all & ~pick2;
    assign s2_ready = taken_all &  pick2;

    wire       lkg   = (FUSE != 0) && taken_all[LOCK_CH];
    wire [1:0] g_typ = pick2[LOCK_CH] ? s2_data[LOCK_CH*SLOT + LK_TP +: 2]
                                      : s_data[LOCK_CH*SLOT + LK_TP +: 2];
    wire       g_wl  = pick2[LOCK_CH] ? s2_data[LOCK_CH*SLOT + LK_WL]
                                      : s_data[LOCK_CH*SLOT + LK_WL];
    integer tc;
    always @(posedge clk) begin
        if (!s_rstn) begin
            rr <= {CHW{1'b0}};
            lk_v <= 1'b0; lk_m <= 1'b0; tg2 <= {N_CH{1'b0}};
        end else begin
            if (|gv) begin
                rr <= (gch[0] + 1'b1 == N_CH[CHW:0]) ? {CHW{1'b0}} : gch[0] + 1'b1;
            end
            if (lkg) begin
                if (g_typ == 2'd0) begin lk_v <= 1'b1; lk_m <= pick2[LOCK_CH]; end
                else if (g_typ == 2'd1 && g_wl) begin lk_v <= 1'b0; end
            end
            for (tc = 0; tc < N_CH; tc = tc + 1) begin
                if (taken_all[tc] && s_valid[tc] && s2_valid[tc]) begin
                    tg2[tc] <= !tg2[tc];
                end
            end
        end
    end
    integer ci;
    reg [CW-1:0] credit_n;
    always @(posedge clk) begin
        for (ci = 0; ci < N_CH; ci = ci + 1) begin
            credit_n = credit[ci] - (taken_all[ci] ? 1'b1 : 1'b0)
                                  + (pp_q[ci] ? 1'b1 : 1'b0);
            if (!s_rstn) begin
                credit[ci]    <= DEPTH[CW-1:0];
                credit_nz[ci] <= 1'b1;
            end else begin
                credit[ci]    <= credit_n;
                credit_nz[ci] <= (credit_n != {CW{1'b0}});
            end
        end
    end

    // ---- the crossing: one register each side, single load ----------------
    reg  [K-1:0]        tx_v;
    reg  [K*CHW-1:0]    tx_ch;
    reg  [K*SLOT-1:0]   tx_d;
    // every sender as one bus, indexed {channel, member}; kohaku_mux is the LUT6
    // tree, one LUT a bit (an inferred mux on this combinational select is two)
    localparam integer NSRC = (FUSE != 0) ? 2*N_CH : N_CH;
    localparam integer MSW  = (NSRC <= 1) ? 1 : $clog2(NSRC);
    wire [NSRC*SLOT-1:0] cand;
    generate for (c = 0; c < N_CH; c = c + 1) begin : g_cand
        if (FUSE != 0) begin : g_pair
            assign cand[(c*2)*SLOT +: SLOT]     = s_data[c*SLOT +: SLOT];
            assign cand[(c*2 + 1)*SLOT +: SLOT] = s2_data[c*SLOT +: SLOT];
        end else begin : g_one
            assign cand[c*SLOT +: SLOT] = s_data[c*SLOT +: SLOT];
        end
    end endgenerate
    generate for (k = 0; k < K; k = k + 1) begin : g_tx
        wire [SLOT-1:0] mx;
        wire            mbr = (FUSE != 0) && pick2[gch[k]];
        wire [MSW-1:0]  msel = (FUSE != 0) ? {gch[k], mbr} : gch[k];
        kohaku_mux #(.W(SLOT), .N(NSRC)) u_mx (.d(cand), .sel(msel), .o(mx));
        always @(posedge clk) begin
            tx_v[k] <= gv[k] && s_rstn;
            if (gv[k]) begin
                tx_ch[k*CHW +: CHW] <= gch[k];
                tx_d[k*SLOT +: SLOT] <= mx;
            end
        end
    end endgenerate

    // ---- RX: land each slot in its channel's ring -------------------------
    reg  [K-1:0]      rx_v;
    reg  [K*CHW-1:0]  rx_ch;
    reg  [K*SLOT-1:0] rx_d;
    // These land in the SENDING domain and feed the ring's write port, so the
    // "far side is out of reset" gate has to be fok_q, which is that condition
    // already synchronised into this clock. m_rstn here is m_clk's, and at
    // ASYNC 1 it is a reset crossing a domain (70_analyze counts them).
    always @(posedge clk) begin
        rx_v  <= tx_v & {K{fok_q}};
        rx_ch <= tx_ch;
        rx_d  <= tx_d;
    end
    wire [N_CH-1:0] wr_en;
    wire [N_CH-1:0] rd_busy;
    generate for (c = 0; c < N_CH; c = c + 1) begin : g_ring
        localparam integer WV = WVEC[c*16 +: 16];
        integer j2;
        reg          we;
        reg [WV-1:0] wd;
        always @(*) begin
            we = 1'b0;
            wd = rx_d[SLOT - 1 -: WV];
            for (j2 = 0; j2 < K; j2 = j2 + 1) begin
                if (rx_v[j2] && (rx_ch[j2*CHW +: CHW] == c[CHW-1:0])) begin
                    we = 1'b1;
                    wd = rx_d[j2*SLOT + SLOT - 1 -: WV];
                end
            end
        end
        assign wr_en[c] = we;
        kx_hop_ring #(.WIDTH(WV), .DEPTH(DEPTH), .MEM(MEM), .FASTW(FASTW),
                      .ASYNC(ASYNC)) u_f (
            .clk(m_clk), .wr_clk(clk), .rstn(m_rstn), .wr_rstn(s_rstn),
            .wr_en(we), .wr_data(wd), .wr_busy(ring_full[c]),
            .rd_en(m_valid[c] && m_ready[c]), .rd_data(m_data[c*SLOT + SLOT - 1 -: WV]),
            .rd_busy(rd_busy[c]));
        if (SLOT > WV) begin : g_z
            assign m_data[c*SLOT +: SLOT - WV] = {(SLOT - WV){1'b0}};
        end
        assign m_valid[c] = !rd_busy[c];
    end endgenerate

    // ---- the return: pop bitmap + fok, registered both ends ----------------
    // The credit is decremented at GRANT and the flit reaches the ring two
    // registers later, so a full flag cannot stand in for it -- writes in
    // flight would be dropped. ASYNC therefore returns the pop COUNT, gray
    // coded, and the sender turns the difference back into single-cycle pops.
    reg  [N_CH-1:0] pp_tx;
    reg             fok_tx;
    generate if (ASYNC != 0) begin : g_ret_a
        reg fok_m, fok_s1, fok_s2;
        always @(posedge m_clk) begin fok_m <= m_rstn; end
        always @(posedge clk) begin
            fok_s1 <= fok_m;
            fok_s2 <= fok_s1;
            fok_q  <= fok_s2 && s_rstn;
        end
        genvar gc;
        for (gc = 0; gc < N_CH; gc = gc + 1) begin : g_pc
            reg  [CW:0] pb, pg, ps1, ps2, seen;
            wire [CW:0] pnext = pb + 1'b1;
            always @(posedge m_clk) begin
                if (!m_rstn) begin pb <= 0; pg <= 0; end
                else if (m_valid[gc] && m_ready[gc]) begin
                    pb <= pnext;
                    pg <= pnext ^ (pnext >> 1);
                end
            end
            integer gi;
            reg [CW:0] pbin;
            always @(*) begin
                pbin[CW] = ps2[CW];
                for (gi = CW - 1; gi >= 0; gi = gi - 1) begin
                    pbin[gi] = pbin[gi+1] ^ ps2[gi];
                end
            end
            always @(posedge clk) begin
                if (!s_rstn) begin ps1 <= 0; ps2 <= 0; seen <= 0; end
                else begin
                    ps1 <= pg;
                    ps2 <= ps1;
                    if (pbin != seen) begin seen <= seen + 1'b1; end
                end
            end
            assign pp_w[gc] = s_rstn && (pbin != seen);
        end
        always @(posedge clk) begin pp_q <= pp_w; end
    end else begin : g_ret_s
        always @(posedge clk) begin
            pp_tx  <= (m_valid & m_ready) & {N_CH{m_rstn}};
            fok_tx <= m_rstn;
            pp_q   <= pp_tx & {N_CH{s_rstn}};
            fok_q  <= fok_tx && s_rstn;
        end
    end endgenerate

`ifndef SYNTHESIS
    generate for (k = 0; k < K; k = k + 1) begin : g_chk
        always @(posedge clk) begin
            if (m_rstn && rx_v[k] && (rx_ch[k*CHW +: CHW] >= N_CH[CHW:0])) begin
                $display("%0t ERROR kx_trunk: slot %0d carries channel %0d of %0d", $time, k, rx_ch[k*CHW +: CHW], N_CH);
            end
        end
    end endgenerate
`endif
endmodule

`default_nettype wire

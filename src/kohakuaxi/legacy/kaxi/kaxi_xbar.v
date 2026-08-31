// KohakuAXI M x N same-width AXI4 crossbar. Reuses axi_n1 (the proven N->1
// arbitrate-then-cross merger) once per home; adds address decode, per-master
// demux of AW/AR/W to the target home, and RR merge of B/R back. Same-width
// full<->full only routes on address (burst/size/lock/cache/prot pass through),
// so the station-bus width-conversion bug class cannot occur here.
// Home decode = addr[HOME_LSB +: HIDX_W]; homes aligned >= 4 KB so a legal burst
// (never crosses 4 KB) maps to one home.
// MILESTONE 1: single clock; same-ID ordering NOT yet enforced (distinct IDs per
// outstanding home, or 1 outstanding/ID) -- stall + test land next (PROGRESS T1.1).

`default_nettype none

module kaxi_xbar #(
    parameter integer M        = 4,           // masters
    parameter integer N_HOME   = 2,           // homes (DRAM/MIG slaves)
    parameter integer ADDR_W   = 40,
    parameter integer DATA_W   = 256,
    parameter integer ID_W     = 4,
    parameter integer HOME_LSB = 32,          // home = addr[HOME_LSB +: HIDX_W]
    parameter integer AW_DEPTH = 16,
    parameter integer W_DEPTH  = 64,
    parameter integer B_DEPTH  = 16,
    parameter integer AR_DEPTH = 16,
    parameter integer R_DEPTH  = 64,
    parameter         WR_MEM   = "block",
    // Derived (port list needs them; do not override).
    parameter integer HIDX_W   = (N_HOME <= 1) ? 1 : $clog2(N_HOME),
    parameter integer MIDX_W   = (M <= 1) ? 1 : $clog2(M),
    parameter integer SID_W    = ID_W + MIDX_W
)(
    input  wire                    clk,
    input  wire                    resetn,

    // ---- M master-facing AXI4 slave ports (flattened) ----
    input  wire [M*ID_W-1:0]       s_awid,
    input  wire [M*ADDR_W-1:0]     s_awaddr,
    input  wire [M*8-1:0]          s_awlen,
    input  wire [M*3-1:0]          s_awsize,
    input  wire [M*2-1:0]          s_awburst,
    input  wire [M-1:0]            s_awvalid,
    output wire [M-1:0]            s_awready,

    input  wire [M*DATA_W-1:0]     s_wdata,
    input  wire [M*(DATA_W/8)-1:0] s_wstrb,
    input  wire [M-1:0]            s_wlast,
    input  wire [M-1:0]            s_wvalid,
    output wire [M-1:0]            s_wready,

    output wire [M*ID_W-1:0]       s_bid,
    output wire [M*2-1:0]          s_bresp,
    output wire [M-1:0]            s_bvalid,
    input  wire [M-1:0]            s_bready,

    input  wire [M*ID_W-1:0]       s_arid,
    input  wire [M*ADDR_W-1:0]     s_araddr,
    input  wire [M*8-1:0]          s_arlen,
    input  wire [M*3-1:0]          s_arsize,
    input  wire [M*2-1:0]          s_arburst,
    input  wire [M-1:0]            s_arvalid,
    output wire [M-1:0]            s_arready,

    output wire [M*ID_W-1:0]       s_rid,
    output wire [M*DATA_W-1:0]     s_rdata,
    output wire [M*2-1:0]          s_rresp,
    output wire [M-1:0]            s_rlast,
    output wire [M-1:0]            s_rvalid,
    input  wire [M-1:0]            s_rready,

    // ---- N_HOME slave-facing AXI4 master ports (flattened) ----
    output wire [N_HOME*SID_W-1:0]     m_awid,
    output wire [N_HOME*ADDR_W-1:0]    m_awaddr,
    output wire [N_HOME*8-1:0]         m_awlen,
    output wire [N_HOME*3-1:0]         m_awsize,
    output wire [N_HOME*2-1:0]         m_awburst,
    output wire [N_HOME-1:0]           m_awvalid,
    input  wire [N_HOME-1:0]           m_awready,

    output wire [N_HOME*DATA_W-1:0]    m_wdata,
    output wire [N_HOME*(DATA_W/8)-1:0] m_wstrb,
    output wire [N_HOME-1:0]           m_wlast,
    output wire [N_HOME-1:0]           m_wvalid,
    input  wire [N_HOME-1:0]           m_wready,

    input  wire [N_HOME*SID_W-1:0]     m_bid,
    input  wire [N_HOME*2-1:0]         m_bresp,
    input  wire [N_HOME-1:0]           m_bvalid,
    output wire [N_HOME-1:0]           m_bready,

    output wire [N_HOME*SID_W-1:0]     m_arid,
    output wire [N_HOME*ADDR_W-1:0]    m_araddr,
    output wire [N_HOME*8-1:0]         m_arlen,
    output wire [N_HOME*3-1:0]         m_arsize,
    output wire [N_HOME*2-1:0]         m_arburst,
    output wire [N_HOME-1:0]           m_arvalid,
    input  wire [N_HOME-1:0]           m_arready,

    input  wire [N_HOME*SID_W-1:0]     m_rid,
    input  wire [N_HOME*DATA_W-1:0]    m_rdata,
    input  wire [N_HOME*2-1:0]         m_rresp,
    input  wire [N_HOME-1:0]           m_rlast,
    input  wire [N_HOME-1:0]           m_rvalid,
    output wire [N_HOME-1:0]           m_rready
);
    localparam integer STRB_W = DATA_W/8;

    genvar gm, gh;

    // Per-master decode: which home does this master's AW / AR address select.
    wire [HIDX_W-1:0] aw_home [0:M-1];
    wire [HIDX_W-1:0] ar_home [0:M-1];
    generate
    for (gm = 0; gm < M; gm = gm + 1) begin : g_dec
        assign aw_home[gm] = s_awaddr[gm*ADDR_W + HOME_LSB +: HIDX_W];
        assign ar_home[gm] = s_araddr[gm*ADDR_W + HOME_LSB +: HIDX_W];
    end
    endgenerate

    // W-follows-AW, deadlock-free by serializing each master's WRITES: one
    // write's W in flight per master (reads stay multi-outstanding). This avoids
    // the write-crossbar deadlock where per-master W order (AW order) conflicts
    // with per-home AW-grant order. `wbusy` gates AW; the home holds the W route.
    // (Multi-outstanding writes with home-order matching is a later optimization.)
    wire [HIDX_W-1:0] w_home    [0:M-1];
    wire              w_home_ne [0:M-1];   // a write is in flight (wbusy)
    wire              aw_fire   [0:M-1];
    wire              w_pop     [0:M-1];   // wlast beat accepted this cycle

    generate
    for (gm = 0; gm < M; gm = gm + 1) begin : g_wsel
        reg              wbusy_r;
        reg [HIDX_W-1:0] whome_r;
        always @(posedge clk) begin
            if (!resetn) begin
                wbusy_r <= 1'b0;
            end else if (aw_fire[gm]) begin
                wbusy_r <= 1'b1; whome_r <= aw_home[gm];
            end else if (w_pop[gm]) begin
                wbusy_r <= 1'b0;
            end
        end
        assign w_home[gm]    = whome_r;
        assign w_home_ne[gm] = wbusy_r;
    end
    endgenerate

    // Same-ID read ordering (AXI4 A5): responses for one ARID must return in
    // request order. Writes already satisfy this (serialized per master). For
    // reads, stall an AR whose ID is outstanding to a DIFFERENT home. Per master,
    // per read-ID: {home, count}. (Writes need no such table -- one in flight.)
    localparam integer NID = 1 << ID_W;
    localparam integer RCW = 5;                       // outstanding-per-id counter
    wire              ar_fire   [0:M-1];
    wire              ar_ord_ok [0:M-1];
    wire              r_retire  [0:M-1];              // rlast R beat accepted

    generate
    for (gm = 0; gm < M; gm = gm + 1) begin : g_rord
        reg [HIDX_W-1:0] rid_home [0:NID-1];
        reg [RCW-1:0]    rid_cnt  [0:NID-1];
        integer k;
        wire [ID_W-1:0]  a_id = s_arid[gm*ID_W +: ID_W];
        wire [ID_W-1:0]  r_id = s_rid [gm*ID_W +: ID_W];
        assign ar_ord_ok[gm] = (rid_cnt[a_id] == {RCW{1'b0}})
                            || (rid_home[a_id] == ar_home[gm]);
        always @(posedge clk) begin
            if (!resetn) begin
                for (k = 0; k < NID; k = k + 1) begin
                    rid_cnt[k] <= {RCW{1'b0}};
                end
            end else begin
                if (ar_fire[gm] && (rid_cnt[a_id] == {RCW{1'b0}})) begin
                    rid_home[a_id] <= ar_home[gm];
                end
                // Same-id retire+issue in one cycle nets zero; else adjust each.
                if (ar_fire[gm] && r_retire[gm] && (a_id == r_id)) begin
                    rid_cnt[a_id] <= rid_cnt[a_id];
                end else begin
                    if (ar_fire[gm]) begin
                        rid_cnt[a_id] <= rid_cnt[a_id] + 1'b1;
                    end
                    if (r_retire[gm]) begin
                        rid_cnt[r_id] <= rid_cnt[r_id] - 1'b1;
                    end
                end
            end
        end
    end
    endgenerate

    // ================= per-home axi_n1 merger + the M x N gating ==============
    // For home h, master m participates iff its AW/AR/W currently targets h.
    // Ready signals come back from the targeted home only.

    // Per (home,master) request-present and ready wires.
    wire [M-1:0] h_awvalid [0:N_HOME-1];
    wire [M-1:0] h_awready [0:N_HOME-1];
    wire [M-1:0] h_wvalid  [0:N_HOME-1];
    wire [M-1:0] h_wready  [0:N_HOME-1];
    wire [M-1:0] h_arvalid [0:N_HOME-1];
    wire [M-1:0] h_arready [0:N_HOME-1];
    wire [M-1:0] h_bvalid  [0:N_HOME-1];
    wire [M-1:0] h_bready  [0:N_HOME-1];
    wire [M-1:0] h_rvalid  [0:N_HOME-1];
    wire [M-1:0] h_rready  [0:N_HOME-1];
    // Per-home per-master response payloads (from that home's axi_n1).
    wire [ID_W-1:0]  h_bid   [0:N_HOME-1][0:M-1];
    wire [1:0]       h_bresp [0:N_HOME-1][0:M-1];
    wire [ID_W-1:0]  h_rid   [0:N_HOME-1][0:M-1];
    wire [DATA_W-1:0] h_rdata[0:N_HOME-1][0:M-1];
    wire [1:0]       h_rresp [0:N_HOME-1][0:M-1];
    wire             h_rlast [0:N_HOME-1][0:M-1];

    generate
    for (gh = 0; gh < N_HOME; gh = gh + 1) begin : g_home
        // Assemble the M-wide input vectors for this home's axi_n1: lane m is
        // master m's request when it targets home gh, idle otherwise.
        wire [M*ID_W-1:0]   hn_awid;
        wire [M*ADDR_W-1:0] hn_awaddr;
        wire [M*8-1:0]      hn_awlen;
        wire [M*3-1:0]      hn_awsize;
        wire [M*2-1:0]      hn_awburst;
        wire [M-1:0]        hn_awvalid;
        wire [M-1:0]        hn_awready;

        wire [M*DATA_W-1:0] hn_wdata;
        wire [M*STRB_W-1:0] hn_wstrb;
        wire [M-1:0]        hn_wlast;
        wire [M-1:0]        hn_wvalid;
        wire [M-1:0]        hn_wready;

        wire [M*ID_W-1:0]   hn_bid;
        wire [M*2-1:0]      hn_bresp;
        wire [M-1:0]        hn_bvalid;
        wire [M-1:0]        hn_bready;

        wire [M*ID_W-1:0]   hn_arid;
        wire [M*ADDR_W-1:0] hn_araddr;
        wire [M*8-1:0]      hn_arlen;
        wire [M*3-1:0]      hn_arsize;
        wire [M*2-1:0]      hn_arburst;
        wire [M-1:0]        hn_arvalid;
        wire [M-1:0]        hn_arready;

        wire [M*ID_W-1:0]   hn_rid;
        wire [M*DATA_W-1:0] hn_rdata;
        wire [M*2-1:0]      hn_rresp;
        wire [M-1:0]        hn_rlast;
        wire [M-1:0]        hn_rvalid;
        wire [M-1:0]        hn_rready;

        for (gm = 0; gm < M; gm = gm + 1) begin : g_gate
            wire aw_hit = (aw_home[gm] == gh[HIDX_W-1:0]);
            wire ar_hit = (ar_home[gm] == gh[HIDX_W-1:0]);
            wire w_hit  = w_home_ne[gm] && (w_home[gm] == gh[HIDX_W-1:0]);

            assign hn_awid  [gm*ID_W   +: ID_W]   = s_awid  [gm*ID_W   +: ID_W];
            assign hn_awaddr[gm*ADDR_W +: ADDR_W] = s_awaddr[gm*ADDR_W +: ADDR_W];
            assign hn_awlen [gm*8      +: 8]      = s_awlen [gm*8      +: 8];
            assign hn_awsize[gm*3      +: 3]      = s_awsize[gm*3      +: 3];
            assign hn_awburst[gm*2     +: 2]      = s_awburst[gm*2     +: 2];
            assign hn_awvalid[gm] = s_awvalid[gm] && aw_hit && !w_home_ne[gm];
            assign h_awvalid[gh][gm] = hn_awvalid[gm];
            assign h_awready[gh][gm] = hn_awready[gm];

            assign hn_wdata[gm*DATA_W +: DATA_W] = s_wdata[gm*DATA_W +: DATA_W];
            assign hn_wstrb[gm*STRB_W +: STRB_W] = s_wstrb[gm*STRB_W +: STRB_W];
            assign hn_wlast[gm] = s_wlast[gm];
            assign hn_wvalid[gm] = s_wvalid[gm] && w_hit;
            assign h_wvalid[gh][gm] = hn_wvalid[gm];
            assign h_wready[gh][gm] = hn_wready[gm];

            assign hn_arid  [gm*ID_W   +: ID_W]   = s_arid  [gm*ID_W   +: ID_W];
            assign hn_araddr[gm*ADDR_W +: ADDR_W] = s_araddr[gm*ADDR_W +: ADDR_W];
            assign hn_arlen [gm*8      +: 8]      = s_arlen [gm*8      +: 8];
            assign hn_arsize[gm*3      +: 3]      = s_arsize[gm*3      +: 3];
            assign hn_arburst[gm*2     +: 2]      = s_arburst[gm*2     +: 2];
            assign hn_arvalid[gm] = s_arvalid[gm] && ar_hit && ar_ord_ok[gm];
            assign h_arvalid[gh][gm] = hn_arvalid[gm];
            assign h_arready[gh][gm] = hn_arready[gm];

            // Responses out of this home for master m -> collected for merge.
            assign h_bvalid[gh][gm] = hn_bvalid[gm];
            assign hn_bready[gm]    = h_bready[gh][gm];
            assign h_bid  [gh][gm]  = hn_bid  [gm*ID_W +: ID_W];
            assign h_bresp[gh][gm]  = hn_bresp[gm*2    +: 2];

            assign h_rvalid[gh][gm] = hn_rvalid[gm];
            assign hn_rready[gm]    = h_rready[gh][gm];
            assign h_rid  [gh][gm]  = hn_rid  [gm*ID_W   +: ID_W];
            assign h_rdata[gh][gm]  = hn_rdata[gm*DATA_W +: DATA_W];
            assign h_rresp[gh][gm]  = hn_rresp[gm*2      +: 2];
            assign h_rlast[gh][gm]  = hn_rlast[gm];
        end

        axi_n1 #(.N(M), .ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(ID_W),
                 .AW_DEPTH(AW_DEPTH), .W_DEPTH(W_DEPTH), .B_DEPTH(B_DEPTH),
                 .AR_DEPTH(AR_DEPTH), .R_DEPTH(R_DEPTH), .WR_MEM(WR_MEM)) u_home (
            .s_aclk(clk), .s_aresetn(resetn),
            .s_awid(hn_awid), .s_awaddr(hn_awaddr), .s_awlen(hn_awlen),
            .s_awsize(hn_awsize), .s_awburst(hn_awburst),
            .s_awvalid(hn_awvalid), .s_awready(hn_awready),
            .s_wdata(hn_wdata), .s_wstrb(hn_wstrb), .s_wlast(hn_wlast),
            .s_wvalid(hn_wvalid), .s_wready(hn_wready),
            .s_bid(hn_bid), .s_bresp(hn_bresp), .s_bvalid(hn_bvalid),
            .s_bready(hn_bready),
            .s_arid(hn_arid), .s_araddr(hn_araddr), .s_arlen(hn_arlen),
            .s_arsize(hn_arsize), .s_arburst(hn_arburst),
            .s_arvalid(hn_arvalid), .s_arready(hn_arready),
            .s_rid(hn_rid), .s_rdata(hn_rdata), .s_rresp(hn_rresp),
            .s_rlast(hn_rlast), .s_rvalid(hn_rvalid), .s_rready(hn_rready),
            .m_aclk(clk), .m_aresetn(resetn),
            .m_awid(m_awid[gh*SID_W +: SID_W]), .m_awaddr(m_awaddr[gh*ADDR_W +: ADDR_W]),
            .m_awlen(m_awlen[gh*8 +: 8]), .m_awsize(m_awsize[gh*3 +: 3]),
            .m_awburst(m_awburst[gh*2 +: 2]), .m_awvalid(m_awvalid[gh]),
            .m_awready(m_awready[gh]),
            .m_wdata(m_wdata[gh*DATA_W +: DATA_W]), .m_wstrb(m_wstrb[gh*STRB_W +: STRB_W]),
            .m_wlast(m_wlast[gh]), .m_wvalid(m_wvalid[gh]), .m_wready(m_wready[gh]),
            .m_bid(m_bid[gh*SID_W +: SID_W]), .m_bresp(m_bresp[gh*2 +: 2]),
            .m_bvalid(m_bvalid[gh]), .m_bready(m_bready[gh]),
            .m_arid(m_arid[gh*SID_W +: SID_W]), .m_araddr(m_araddr[gh*ADDR_W +: ADDR_W]),
            .m_arlen(m_arlen[gh*8 +: 8]), .m_arsize(m_arsize[gh*3 +: 3]),
            .m_arburst(m_arburst[gh*2 +: 2]), .m_arvalid(m_arvalid[gh]),
            .m_arready(m_arready[gh]),
            .m_rid(m_rid[gh*SID_W +: SID_W]), .m_rdata(m_rdata[gh*DATA_W +: DATA_W]),
            .m_rresp(m_rresp[gh*2 +: 2]), .m_rlast(m_rlast[gh]),
            .m_rvalid(m_rvalid[gh]), .m_rready(m_rready[gh])
        );
    end
    endgenerate

    // ================= per-master fan-in: ready select + B/R merge ============
    generate
    for (gm = 0; gm < M; gm = gm + 1) begin : g_mfan
        // AW/AR/W ready: from the targeted home only.
        wire [N_HOME-1:0] awr, arr, wr;
        wire [N_HOME-1:0] bv, rv;
        for (gh = 0; gh < N_HOME; gh = gh + 1) begin : g_pick
            assign awr[gh] = h_awready[gh][gm] && (aw_home[gm] == gh[HIDX_W-1:0]);
            assign arr[gh] = h_arready[gh][gm] && (ar_home[gm] == gh[HIDX_W-1:0]);
            assign wr[gh]  = h_wready[gh][gm]  && w_home_ne[gm] && (w_home[gm] == gh[HIDX_W-1:0]);
            assign bv[gh]  = h_bvalid[gh][gm];
            assign rv[gh]  = h_rvalid[gh][gm];
        end
        assign s_awready[gm] = |awr;
        assign s_arready[gm] = |arr;
        assign s_wready[gm]  = |wr;
        assign aw_fire[gm]   = s_awvalid[gm] && s_awready[gm];
        assign ar_fire[gm]   = s_arvalid[gm] && s_arready[gm];
        assign w_pop[gm]     = s_wvalid[gm]  && s_wready[gm] && s_wlast[gm];

        // ---- B merge: single-beat. Lock the selected home from the cycle
        //      valid asserts until accepted, so the payload stays stable (A2.3).
        reg [HIDX_W-1:0] b_rr, b_cur;
        reg              b_busy;
        reg [HIDX_W-1:0] b_sel;
        reg              b_any;
        integer          bi;
        always @(*) begin
            b_sel = {HIDX_W{1'b0}};
            b_any = 1'b0;
            for (bi = N_HOME-1; bi >= 0; bi = bi - 1) begin
                if (bv[(bi + b_rr) % N_HOME]) begin
                    b_sel = (bi + b_rr) % N_HOME;
                    b_any = 1'b1;
                end
            end
        end
        wire [HIDX_W-1:0] b_use = b_busy ? b_cur : b_sel;
        wire              b_val = b_busy ? bv[b_cur] : b_any;
        wire              b_beat = b_val && s_bready[gm];
        always @(posedge clk) begin
            if (!resetn) begin
                b_busy <= 1'b0; b_cur <= {HIDX_W{1'b0}}; b_rr <= {HIDX_W{1'b0}};
            end else if (b_beat) begin
                b_busy <= 1'b0; b_rr <= (b_use + 1'b1) % N_HOME;
            end else if (!b_busy && b_val) begin
                b_busy <= 1'b1; b_cur <= b_sel;
            end
        end
        assign s_bvalid[gm]           = b_val;
        assign s_bid[gm*ID_W +: ID_W] = h_bid[b_use][gm];
        assign s_bresp[gm*2 +: 2]     = h_bresp[b_use][gm];
        for (gh = 0; gh < N_HOME; gh = gh + 1) begin : g_brdy
            assign h_bready[gh][gm] = b_beat && (b_use == gh[HIDX_W-1:0]);
        end

        // ---- R merge: burst-atomic. Lock the home from the cycle valid asserts
        //      through the rlast beat, so beats of different homes never
        //      interleave and the payload stays stable while stalled (A2.3/A5).
        reg              r_busy;
        reg [HIDX_W-1:0] r_cur, r_rr;
        reg [HIDX_W-1:0] r_sel;
        reg              r_any;
        integer          rj;
        always @(*) begin
            r_sel = {HIDX_W{1'b0}};
            r_any = 1'b0;
            for (rj = N_HOME-1; rj >= 0; rj = rj - 1) begin
                if (rv[(rj + r_rr) % N_HOME]) begin
                    r_sel = (rj + r_rr) % N_HOME;
                    r_any = 1'b1;
                end
            end
        end
        wire [HIDX_W-1:0] r_use  = r_busy ? r_cur : r_sel;
        wire              r_val  = r_busy ? rv[r_cur] : r_any;
        wire              r_beat = r_val && s_rready[gm];
        wire              r_last = h_rlast[r_use][gm];
        assign r_retire[gm] = r_beat && r_last;
        always @(posedge clk) begin
            if (!resetn) begin
                r_busy <= 1'b0; r_cur <= {HIDX_W{1'b0}}; r_rr <= {HIDX_W{1'b0}};
            end else if (r_beat) begin
                if (r_last) begin r_busy <= 1'b0; r_rr <= (r_use + 1'b1) % N_HOME; end
                else begin r_busy <= 1'b1; r_cur <= r_use; end
            end else if (!r_busy && r_val) begin
                r_busy <= 1'b1; r_cur <= r_sel;
            end
        end
        assign s_rvalid[gm]              = r_val;
        assign s_rid[gm*ID_W +: ID_W]    = h_rid[r_use][gm];
        assign s_rdata[gm*DATA_W +: DATA_W] = h_rdata[r_use][gm];
        assign s_rresp[gm*2 +: 2]        = h_rresp[r_use][gm];
        assign s_rlast[gm]               = r_last;
        for (gh = 0; gh < N_HOME; gh = gh + 1) begin : g_rrdy
            assign h_rready[gh][gm] = r_beat && (r_use == gh[HIDX_W-1:0]);
        end
    end
    endgenerate

endmodule

`default_nettype wire

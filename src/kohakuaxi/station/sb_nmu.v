// Network manager unit: one external AXI4 manager onto the station.

// TWO async FIFOs, not five. AW+W pack into one REQ stream and B+R into one
// RSP stream, and a unidirectional stream is what an async FIFO handles.

// The shim takes NO parameter describing the clock relationship -- not the
// ratio, not whether it is synchronous. That is what makes it automatic.

// DEADLOCK INVARIANT: a request is injected only once space for its complete
// response is reserved. `rsp_credit` is that reservation and lives in s_aclk.

// REQ_DEPTH >= max AxLEN + 1, or a packet longer than the FIFO wedges: earlier
// packets always drain on their token, an incomplete one has no token yet.

// MW may be any width from 8 up to FW. A narrow port PLACES its beat into the
// flit's byte lane, which is a demux; extraction back is a small mux.

`default_nettype none

module sb_nmu #(
    parameter integer MW        = 512,          // this port's AXI data width
    parameter integer MIDW      = 4,            // this port's AXI ID width
    parameter integer AW        = 40,
    parameter integer FW        = 512,          // flit payload; MW <= FW
    parameter integer TAGW      = 4,
    parameter integer DSTW      = 4,
    parameter integer NSEG      = 8,
    parameter integer REQ_DEPTH = 512,
    parameter integer RSP_DEPTH = 256,          // beats; AXI4 max burst is 256
    // Longest burst this port may issue, in beats. 0 = the 4 KB bound for MW.
    // Set 1 on an AXI4-Lite master; a longer burst then hangs it, not corrupts.
    parameter integer MAX_BURST = 0,
    // Storage per FIFO class. "distributed" | "block" | "ultra" -- a design
    // that will not spend BRAM on its interconnect sets all three to the first.
    parameter         REQ_MEM   = "block",
    parameter         RSP_MEM   = "block",
    parameter         TOK_MEM   = "distributed",
    // Non-zero overrides the three above and picks per FIFO: what ONE BRAM is
    // worth in LUTs. KohakuTPU trades at ~820, crossover near depth 360.
    parameter integer LUT_PER_BRAM = 0,
    // 0 drops the packet-complete token: the hub holds its grant through an
    // underrun, stalling the station, and REQ_DEPTH stops covering a burst.
    parameter integer STORE_FWD   = 1,
    // Decode table. Last match wins; overlapping entries are a config error.
    parameter [NSEG*AW-1:0]   SEG_BASE = {NSEG*AW{1'b0}},
    parameter [NSEG*AW-1:0]   SEG_MASK = {NSEG*AW{1'b0}},   // 1 = compare
    parameter [NSEG*AW-1:0]   SEG_XLT  = {NSEG*AW{1'b0}},   // emitted high bits
    parameter [NSEG*DSTW-1:0] SEG_DST  = {NSEG*DSTW{1'b0}},
    // Port at the DESTINATION station. SEG_DST routes this hop only, so a flit
    // leaving over a link would otherwise arrive with a meaningless index.
    parameter [NSEG*DSTW-1:0] SEG_DPORT = {NSEG*DSTW{1'b0}},
    parameter [NSEG-1:0]      SEG_VLD  = {NSEG{1'b0}}
)(
    input  wire                s_aclk,
    input  wire                s_aresetn,

    input  wire [MIDW-1:0]     s_awid,
    input  wire [AW-1:0]       s_awaddr,
    input  wire [7:0]          s_awlen,
    input  wire [2:0]          s_awsize,
    input  wire [1:0]          s_awburst,
    input  wire                s_awvalid,
    output wire                s_awready,

    input  wire [MW-1:0]       s_wdata,
    input  wire [MW/8-1:0]     s_wstrb,
    input  wire                s_wlast,
    input  wire                s_wvalid,
    output wire                s_wready,

    output wire [MIDW-1:0]     s_bid,
    output wire [1:0]          s_bresp,
    output wire                s_bvalid,
    input  wire                s_bready,

    input  wire [MIDW-1:0]     s_arid,
    input  wire [AW-1:0]       s_araddr,
    input  wire [7:0]          s_arlen,
    input  wire [2:0]          s_arsize,
    input  wire [1:0]          s_arburst,
    input  wire                s_arvalid,
    output wire                s_arready,

    output wire [MIDW-1:0]     s_rid,
    output wire [MW-1:0]       s_rdata,
    output wire [1:0]          s_rresp,
    output wire                s_rlast,
    output wire                s_rvalid,
    input  wire                s_rready,

    input  wire                bus_clk,
    input  wire                bus_rst,

    output wire                req_valid,
    input  wire                req_ready,
    output wire [DSTW-1:0]     req_dst,
    output wire [DSTW-1:0]     req_dport,
    output wire [TAGW-1:0]     req_tag,
    output wire                req_wr,
    output wire                req_head,
    output wire                req_last,
    output wire [AW-1:0]       req_addr,
    output wire [7:0]          req_len,
    output wire [2:0]          req_size,
    output wire [FW-1:0]       req_data,
    output wire [FW/8-1:0]     req_strb,

    input  wire                rsp_valid,
    output wire                rsp_ready,
    input  wire [TAGW-1:0]     rsp_tag,
    input  wire                rsp_wr,
    input  wire                rsp_last,
    input  wire [1:0]          rsp_resp,
    input  wire [FW-1:0]       rsp_data,

    output wire [31:0]         stat_decerr
);
    localparam integer NTAG  = 1 << TAGW;
    // MW > FW splits a port beat into SUB flits, so the fabric can run narrower
    // than its widest manager -- every mux and FIFO scales with FW.

    localparam integer SUB   = (MW > FW) ? (MW / FW) : 1;
    localparam integer SUBW  = (SUB <= 1) ? 1 : $clog2(SUB);
    localparam [2:0]   FSZ   = $clog2(FW/8);
    localparam integer NLANE = (FW > MW) ? (FW / MW) : 1;
    localparam integer LANEW = (NLANE <= 1) ? 1 : $clog2(NLANE);
    localparam integer LSB   = (NLANE <= 1) ? 0 : $clog2(MW/8);
    // XPM's async FIFO $finish-es below depth 16, so a Lite port asking for 4
    // kills the simulation at time 0 rather than warning.
    localparam integer RQD   = (REQ_DEPTH < 16) ? 16 : REQ_DEPTH;
    // The 4 KB rule caps a legal burst; sizing the queues past it buys nothing.
    localparam integer AXI_MAXB = (4096/(MW/8) > 256) ? 256 : 4096/(MW/8);
    // The 4 KB bound is 256 beats on a 32-bit port. An AXI4-Lite master is
    // single-beat BY PROTOCOL, so MAX_BURST=1 there is a fact, not a hope.
    localparam integer MAXB = (MAX_BURST > 0 && MAX_BURST < AXI_MAXB)
                              ? MAX_BURST : AXI_MAXB;
    // Reserving the whole response before inject means a SHORT rsp FIFO hangs
    // the port, never overflows: MW=512 over FW=256 needs 128, and 64 looks fine.
    // Both floors apply: the burst reservation, and XPM's own depth-16 minimum
    // below which it $finish-es at time 0 rather than warning.
    localparam integer RSP_MIN = MAXB * SUB;
    localparam integer RSP_FLR = (RSP_MIN < 16) ? 16 : RSP_MIN;
    localparam integer RSD   = (RSP_DEPTH < RSP_FLR) ? RSP_FLR : RSP_DEPTH;
    localparam integer CWR   = $clog2(RSD) + 1;
    localparam integer CW    = (CWR < 9) ? 9 : CWR;
    localparam integer SZ    = $clog2(MW/8);
    // Store the PORT's beat, not the flit's: a 32-bit port queuing 638-bit
    // flits burns 9 RAMB36 on width at 3% depth use. MW + lane is 102 bits.
    localparam integer REQ_W = 2*DSTW + TAGW + 3 + AW + 8 + 3 + LANEW
                               + MW + MW/8;
    localparam integer RSP_W = TAGW + 2 + 2 + FW;

    wire srst = !s_aresetn;

    // LUTRAM is W*ceil(D/32) LUTs and wastes nothing at any depth; a RAMB36 in
    // SDP is 72x512, so it costs ceil(W/72) tiles however shallow the FIFO is.
    function integer lram_lut;
        input integer w;
        input integer d;
        begin lram_lut = w * ((d + 31) / 32); end
    endfunction

    function integer bram_tiles;
        input integer w;
        input integer d;
        begin bram_tiles = ((w + 71) / 72) * ((d + 511) / 512); end
    endfunction

    localparam AUTO_REQ = (lram_lut(REQ_W, RQD)
                           > LUT_PER_BRAM * bram_tiles(REQ_W, RQD))
                          ? "block" : "distributed";
    localparam AUTO_RSP = (lram_lut(RSP_W, RSD)
                           > LUT_PER_BRAM * bram_tiles(RSP_W, RSD))
                          ? "block" : "distributed";

    localparam REQ_STY = (LUT_PER_BRAM > 0) ? AUTO_REQ : REQ_MEM;
    localparam RSP_STY = (LUT_PER_BRAM > 0) ? AUTO_RSP : RSP_MEM;

    // ================================================================ decode
    localparam integer DECW = 1 + DSTW + DSTW + AW;
    // Identity translation folds the whole rewrite away at elaboration, which
    // is 40 bits of mask-and-or per segment that most ports never need.
    localparam XLT_ID = (SEG_XLT == SEG_BASE);

    function [DECW-1:0] decode;
        input [AW-1:0] a;
        integer i;
        reg [DECW-1:0] r;
        begin
            r = {DECW{1'b0}};
            for (i = 0; i < NSEG; i = i + 1)
                if (SEG_VLD[i] &&
                    (((a ^ SEG_BASE[i*AW +: AW]) & SEG_MASK[i*AW +: AW])
                     == {AW{1'b0}}))
                    r = { 1'b1, SEG_DST[i*DSTW +: DSTW],
                          SEG_DPORT[i*DSTW +: DSTW],
                          XLT_ID ? a
                                 : ((a & ~SEG_MASK[i*AW +: AW])
                                    | SEG_XLT[i*AW +: AW]) };
            decode = r;
        end
    endfunction

    wire [DECW-1:0] awd = decode(s_awaddr);
    wire [DECW-1:0] ard = decode(s_araddr);
    wire            aw_hit  = awd[DECW-1];
    wire [DSTW-1:0] aw_dst  = awd[DECW-2 -: DSTW];
    wire [DSTW-1:0] aw_dpt  = awd[DECW-2-DSTW -: DSTW];
    wire [AW-1:0]   aw_xadr = awd[AW-1:0];
    wire            ar_hit  = ard[DECW-1];
    wire [DSTW-1:0] ar_dst  = ard[DECW-2 -: DSTW];
    wire [DSTW-1:0] ar_dpt  = ard[DECW-2-DSTW -: DSTW];
    wire [AW-1:0]   ar_xadr = ard[AW-1:0];

    // ============================================================= tag table
    reg [NTAG-1:0]  tag_busy;
    reg [MIDW-1:0]  tg_id   [0:NTAG-1];
    reg [LANEW-1:0] tg_lane [0:NTAG-1];
    // Flits STILL RESERVED for this tag. A subordinate may answer with fewer --
    // a Lite port tied rlast high, a TIMEOUT SLVERR -- and the rest would leak.
    reg [8:0]       tg_rsv  [0:NTAG-1];

    reg [TAGW-1:0] tag_new;
    reg            tag_avail;
    integer        t;
    always @(*) begin
        tag_new   = {TAGW{1'b0}};
        tag_avail = 1'b0;
        for (t = NTAG-1; t >= 0; t = t - 1)
            if (!tag_busy[t]) begin
                tag_new   = t[TAGW-1:0];
                tag_avail = 1'b1;
            end
    end

    // ====================================================== request assembly
    // ONE REQ FIFO, so an AR flit must never land inside a write packet. AR is
    // gated on the write FSM being idle; reads and writes therefore serialise.
    localparam W_IDLE = 2'd0, W_DATA = 2'd1, W_ESINK = 2'd2, W_ERESP = 2'd3;

    reg  [1:0]       wst;
    reg  [TAGW-1:0]  w_tag;
    reg  [DSTW-1:0]  w_dst, w_dpt;
    reg  [AW-1:0]    w_addr;
    reg  [7:0]       w_len;
    reg  [2:0]       w_size;
    reg  [LANEW-1:0] w_lane;
    reg              w_head;
    reg  [MIDW-1:0]  w_eid;
    reg  [CW-1:0]    rsp_credit;
    reg              pri;
    reg              sending;
    reg  [2:0]       tok_dly;
    reg  [31:0]      decerr_cnt;
    reg              er_busy;
    reg  [7:0]       er_left;
    reg  [MIDW-1:0]  er_id;

    wire             reqf_full, tokf_full, reqf_empty, tokf_empty;
    wire             rspf_empty, rspf_full;
    wire             rsp_pop;
    // In FLITS, not beats: a split read returns SUB flits per beat and every
    // one of them needs reserved room, or the RSP FIFO overruns.
    wire [8:0]       ar_beats = ({1'b0, s_arlen} + 9'd1) * SUB;

    // Declared here because the main always block retires tags on them.
    wire b_err, b_nrm, r_err, r_raw, r_nrm, r_eat, g_last;
    reg  [((SUB > 1) ? (MW-FW) : 1)-1:0] gath;
    reg  [SUBW-1:0] gsub;

    wire [RSP_W-1:0] rspf_out;
    wire [TAGW-1:0]  rf_tag     = rspf_out[RSP_W-1 -: TAGW];
    wire             rsp_wr_o   = rspf_out[RSP_W-TAGW-1];
    wire             rsp_last_o = rspf_out[RSP_W-TAGW-2];
    wire [1:0]       rf_resp    = rspf_out[FW+1 -: 2];
    wire [FW-1:0]    rf_data    = rspf_out[FW-1:0];

    wire aw_ok = s_awvalid && (wst == W_IDLE) && !reqf_full && !tokf_full
                 && (!aw_hit || (tag_avail && (rsp_credit >= {{(CW-1){1'b0}}, 1'b1})));
    wire ar_ok = s_arvalid && (wst == W_IDLE) && !reqf_full && !tokf_full
                 && ar_hit && tag_avail
                 && (rsp_credit >= {{(CW-9){1'b0}}, ar_beats});

    wire aw_go   = aw_ok && (!ar_ok || !pri);
    wire ar_go   = ar_ok && !aw_go;
    // A miss takes no tag and no credit, so the RSP path cannot answer it --
    // the replay counter below owes the manager its DECERR beats.
    wire ar_miss = s_arvalid && (wst == W_IDLE) && !ar_hit && !er_busy && !aw_go;

    assign s_awready = aw_go;
    assign s_arready = ar_go || ar_miss;

    wire w_beat = (wst == W_DATA) && s_wvalid && !reqf_full && !tokf_full;
    wire e_beat = (wst == W_ESINK) && s_wvalid;
    assign s_wready = w_beat || e_beat;

    wire req_push = w_beat || ar_go;
    wire tok_push = (w_beat && s_wlast) || ar_go;


    // Mux the HEADER only. A read flit's data/strb are never read, so selecting
    // zeros into them costs FW + FW/8 LUTs to produce something nobody looks at.

    // LUT-ONLY WIN. Transistor reference: on an ASIC the zero branch is a
    // tie-off and the mux is one AND per bit; here it is one LUT6 per bit.
    // The manager's burst is re-expressed in flits before it leaves, so the
    // NSU never learns that a split happened.
    wire [8:0] aw_flits = (({1'b0, s_awlen} + 9'd1) * SUB) - 9'd1;
    wire [8:0] ar_flits = (({1'b0, s_arlen} + 9'd1) * SUB) - 9'd1;

    wire [DSTW-1:0] hdr_dst  = ar_go ? ar_dst   : w_dst;
    wire [DSTW-1:0] hdr_dpt  = ar_go ? ar_dpt   : w_dpt;
    wire [TAGW-1:0] hdr_tag  = ar_go ? tag_new  : w_tag;
    wire            hdr_head = ar_go ? 1'b1     : w_head;
    wire            hdr_last = ar_go ? 1'b1     : s_wlast;
    wire [AW-1:0]   hdr_addr = ar_go ? ar_xadr  : w_addr;
    wire [7:0]      hdr_len  = (SUB > 1) ? (ar_go ? ar_flits[7:0] : w_len)
                                         : (ar_go ? s_arlen : w_len);
    wire [2:0]      hdr_size = (SUB > 1) ? FSZ
                                         : (ar_go ? s_arsize : w_size);

    wire [REQ_W-1:0] req_in = { hdr_dst, hdr_dpt, hdr_tag, !ar_go, hdr_head,
                                hdr_last, hdr_addr, hdr_len, hdr_size,
                                w_lane, s_wdata, s_wstrb };

    always @(posedge s_aclk) begin
        if (srst) begin
            wst      <= W_IDLE;
            w_head   <= 1'b1;
            w_lane   <= {LANEW{1'b0}};
            pri      <= 1'b0;
            tag_busy <= {NTAG{1'b0}};
        end else begin
            if (aw_go || ar_go) pri <= !pri;

            if (aw_go) begin
                w_tag  <= tag_new;
                w_dst  <= aw_dst;
                w_dpt  <= aw_dpt;
                w_addr <= aw_xadr;
                w_len  <= (SUB > 1) ? aw_flits[7:0] : s_awlen;
                w_size <= s_awsize;
                w_eid  <= s_awid;
                w_head <= 1'b1;
                w_lane <= (NLANE <= 1) ? {LANEW{1'b0}} : s_awaddr[LSB +: LANEW];
                wst    <= aw_hit ? W_DATA : W_ESINK;
                if (aw_hit) begin
                    tag_busy[tag_new] <= 1'b1;
                    tg_id  [tag_new]  <= s_awid;
                    tg_lane[tag_new]  <= {LANEW{1'b0}};
                    tg_rsv [tag_new]  <= 9'd1;
                end
            end

            if (ar_go) begin
                tag_busy[tag_new] <= 1'b1;
                tg_id  [tag_new]  <= s_arid;
                tg_lane[tag_new]  <= (NLANE <= 1) ? {LANEW{1'b0}}
                                                  : s_araddr[LSB +: LANEW];
                tg_rsv [tag_new]  <= ar_beats;
            end

            // rf_tag is busy and tag_new is free, so this never races the two
            // writes above for the same entry.
            if (rsp_pop) tg_rsv[rf_tag] <= tg_rsv[rf_tag] - 9'd1;

            if (w_beat) begin
                w_head <= 1'b0;
                w_lane <= (NLANE <= 1) ? {LANEW{1'b0}} : w_lane + 1'b1;
                if (s_wlast) wst <= W_IDLE;
            end
            if (e_beat && s_wlast)                          wst <= W_ERESP;
            if ((wst == W_ERESP) && s_bvalid && s_bready)   wst <= W_IDLE;

            // Free on the MANAGER-visible last beat: with SUB>1 the flits eaten
            // to fill the gather are not beats and must not retire the tag.
            if ((b_nrm && s_bready) ||
                (r_nrm && s_rready && rsp_last_o)) tag_busy[rf_tag] <= 1'b0;
            if (r_nrm && s_rready && !rsp_last_o)
                tg_lane[rf_tag] <= (NLANE <= 1) ? {LANEW{1'b0}}
                                                : tg_lane[rf_tag] + 1'b1;
        end
    end

    always @(posedge s_aclk) begin
        if (srst) rsp_credit <= RSD[CW-1:0];
        // The last term reclaims what a SHORT answer never returned; tg_rsv is
        // read pre-decrement, so a full-length burst reclaims exactly zero.
        else rsp_credit <= rsp_credit
             - (ar_go              ? {{(CW-9){1'b0}}, ar_beats} : {CW{1'b0}})
             - ((aw_go && aw_hit)  ? {{(CW-1){1'b0}}, 1'b1}     : {CW{1'b0}})
             + (rsp_pop            ? {{(CW-1){1'b0}}, 1'b1}     : {CW{1'b0}})
             + ((rsp_pop && rsp_last_o)
                ? {{(CW-9){1'b0}}, (tg_rsv[rf_tag] - 9'd1)}     : {CW{1'b0}});
    end

    always @(posedge s_aclk) begin
        if (srst) er_busy <= 1'b0;
        else if (ar_miss) begin
            er_busy <= 1'b1;
            er_left <= s_arlen;
            er_id   <= s_arid;
        end else if (er_busy && s_rvalid && s_rready) begin
            if (er_left == 8'd0) er_busy <= 1'b0;
            else                 er_left <= er_left - 1'b1;
        end
    end

    always @(posedge s_aclk)
        if (srst) decerr_cnt <= 32'd0;
        else if (ar_miss || (aw_go && !aw_hit)) decerr_cnt <= decerr_cnt + 1'b1;
    assign stat_decerr = decerr_cnt;

    // ========================================================= clock crossing
    // Local copy of the bus reset: undivided its fanout reaches every shim and
    // caps bus_clk at 284 MHz against 360-510 on the port clocks.
    (* dont_touch = "yes" *) reg bus_rst_q;
    always @(posedge bus_clk) bus_rst_q <= bus_rst;

    wire [REQ_W-1:0] reqf_out;

    async_fifo #(.DATA_WIDTH(REQ_W), .FIFO_DEPTH(RQD),
                 .MEMORY_TYPE(REQ_STY)) u_reqf (
        .wr_clk(s_aclk), .wr_rst(srst), .wr_en(req_push), .wr_data(req_in),
        .wr_full(reqf_full),
        .rd_clk(bus_clk), .rd_en(flit_go && sub_last), .rd_data(reqf_out),
        .rd_empty(reqf_empty)
    );

    // STORE AND FORWARD: the station holds its grant for a whole packet, so a
    // FIFO underrun mid-packet stalls everyone. Token is 2 cycles late by CDC.
    always @(posedge s_aclk)
        if (srst) tok_dly <= 3'd0;
        else      tok_dly <= {tok_dly[1:0], tok_push};

    generate
    if (STORE_FWD) begin : g_sf
        wire tok_pop = !sending && req_valid && req_ready;

        async_fifo #(.DATA_WIDTH(1), .FIFO_DEPTH(RQD),
                     .MEMORY_TYPE(TOK_MEM)) u_tokf (
            .wr_clk(s_aclk), .wr_rst(srst), .wr_en(tok_dly[2]),
            .wr_data(1'b0), .wr_full(tokf_full),
            .rd_clk(bus_clk), .rd_en(tok_pop), .rd_data(),
            .rd_empty(tokf_empty)
        );

        assign req_valid = (sending || !tokf_empty) && !reqf_empty;

        always @(posedge bus_clk)
            if (bus_rst_q) sending <= 1'b0;
            else if (req_valid && req_ready) sending <= !req_last;
    end else begin : g_nosf
        assign tokf_full  = 1'b0;
        assign tokf_empty = 1'b1;
        assign req_valid  = !reqf_empty;
    end
    endgenerate

    // Expand on the READ side: replication is wiring, and the strobe demux is
    // FW/8 bits of (one strobe bit, lane) -- the same cost, moved off the FIFO.
    wire [LANEW-1:0] o_lane;
    wire [MW-1:0]    o_data;
    wire [MW/8-1:0]  o_strb;
    wire             o_head, o_last;

    assign {req_dst, req_dport, req_tag, req_wr, o_head, o_last, req_addr,
            req_len, req_size, o_lane, o_data, o_strb} = reqf_out;

    // A READ is NEVER split: start_rd sets no body state at the NSU, so a
    // phantom second flit sits at the queue head forever and deadlocks.
    reg  [SUBW-1:0] subc;
    wire            sub_last = (SUB <= 1) ? 1'b1
                             : (!req_wr || (subc == SUB[SUBW-1:0]-1'b1));
    wire            flit_go  = req_valid && req_ready;

    always @(posedge bus_clk)
        if (bus_rst_q)    subc <= {SUBW{1'b0}};
        else if (flit_go) subc <= sub_last ? {SUBW{1'b0}} : subc + 1'b1;

    assign req_head = o_head && (subc == {SUBW{1'b0}});
    assign req_last = o_last && sub_last;

    // Only the narrow-port form places into lanes; at MW > FW the lane write
    // would be MW/8 bits into an FW/8 vector.
    generate
    if (SUB <= 1) begin : g_place
        reg [FW/8-1:0] strb_x;
        always @(*) begin
            strb_x = {(FW/8){1'b0}};
            strb_x[o_lane*(MW/8) +: MW/8] = o_strb;
        end
        assign req_data = {NLANE{o_data}};
        assign req_strb = strb_x;
    end else begin : g_split
        assign req_data = o_data[subc*FW     +: FW];
        assign req_strb = o_strb[subc*(FW/8) +: FW/8];
    end
    endgenerate

    async_fifo #(.DATA_WIDTH(RSP_W), .FIFO_DEPTH(RSD),
                 .MEMORY_TYPE(RSP_STY)) u_rspf (
        .wr_clk(bus_clk), .wr_rst(bus_rst_q), .wr_en(rsp_valid && rsp_ready),
        .wr_data({rsp_tag, rsp_wr, rsp_last, rsp_resp, rsp_data}),
        .wr_full(rspf_full),
        .rd_clk(s_aclk), .rd_en(rsp_pop), .rd_data(rspf_out),
        .rd_empty(rspf_empty)
    );

    assign rsp_ready = 1'b1;                    // reserved before injection

    // ============================================================ response out
    wire [LANEW-1:0] rd_lane = (NLANE <= 1) ? {LANEW{1'b0}} : tg_lane[rf_tag];

    // A split read comes back as SUB flits; gather them before the manager
    // sees a beat. The oldest sits low, so the newest concatenates on top.
    assign g_last = (SUB <= 1) ? 1'b1 : (gsub == SUB[SUBW-1:0]-1'b1);

    assign b_err = (wst == W_ERESP);
    assign b_nrm = !rspf_empty && rsp_wr_o && !b_err;
    assign r_err = er_busy;
    assign r_raw = !rspf_empty && !rsp_wr_o && !r_err;
    assign r_nrm = r_raw && g_last;

    // Sub-beats before the last are consumed without the manager seeing them.
    assign r_eat = r_raw && !g_last;

    always @(posedge s_aclk) begin
        if (srst) gsub <= {SUBW{1'b0}};
        else if (r_eat || (r_nrm && s_rready))
            gsub <= g_last ? {SUBW{1'b0}} : gsub + 1'b1;
    end

    // SUB==2 holds one flit, so there is nothing to shift. A generate, not an
    // if: at SUB==2 the shift's part-select reverses and fails elaboration.
    generate
    if (SUB == 2) begin : g_gather2
        always @(posedge s_aclk) if (r_eat) gath <= rf_data;
    end else if (SUB > 2) begin : g_gatherN
        always @(posedge s_aclk)
            if (r_eat || (r_nrm && s_rready))
                gath <= {rf_data, gath[MW-FW-1:FW]};
    end
    endgenerate

    assign s_bvalid = b_err || b_nrm;
    assign s_bid    = b_err ? w_eid  : tg_id[rf_tag];
    assign s_bresp  = b_err ? 2'b11  : rf_resp;

    assign s_rvalid = r_err || r_nrm;
    assign s_rid    = r_err ? er_id  : tg_id[rf_tag];
    assign s_rdata  = (SUB <= 1) ? rf_data[rd_lane*MW +: MW]
                                 : {rf_data, gath};
    assign s_rresp  = r_err ? 2'b11  : rf_resp;
    assign s_rlast  = r_err ? (er_left == 8'd0) : rsp_last_o;

    assign rsp_pop  = (b_nrm && s_bready) || r_eat || (r_nrm && s_rready);

`ifndef SYNTHESIS
    // Only a write INTO a full FIFO is the bug. `wr_full` also carries XPM's
    // reset-busy for tens of cycles after release, which is not one.
    always @(posedge bus_clk)
        if (!bus_rst && rsp_valid && rspf_full)
            $display("%0t ERROR sb_nmu: RSP overflow -- credit accounting broke",
                     $time);

    always @(posedge s_aclk) begin
        if (!srst && aw_go && aw_hit && ({1'b0, s_awlen} + 9'd1 > RQD))
            $display("%0t ERROR sb_nmu: AWLEN %0d exceeds REQ depth %0d",
                     $time, s_awlen, RQD);
        // AXI4 forbids crossing a 4 KB boundary, so a port can never legally
        // burst past min(256, 4096/(MW/8)) beats -- 64 on a 512-bit port.
        // MAXB is in BEATS; ar_beats is in flits. Comparing them directly
        // fires on every legal max-length burst as soon as SUB > 1.
        if (!srst && ar_go && (({1'b0, s_arlen} + 9'd1) > MAXB))
            $display("%0t ERROR sb_nmu: ARLEN %0d exceeds MAX_BURST %0d",
                     $time, s_arlen, MAXB);
        if (!srst && aw_go && (({1'b0, s_awlen} + 9'd1) > MAXB))
            $display("%0t ERROR sb_nmu: AWLEN %0d exceeds MAX_BURST %0d",
                     $time, s_awlen, MAXB);
        // A read needs arlen+1 response credits. Ask for more than the FIFO
        // holds and `ar_ok` never asserts -- a silent hang, not an error.
        if (!srst && s_arvalid && ar_hit && (ar_beats > RSD))
            $display("%0t ERROR sb_nmu: ARLEN %0d needs %0d credits, RSP depth %0d -- stalls forever",
                     $time, s_arlen, ar_beats, RSD);
        if (!srst && aw_go && (s_awburst != 2'b01))
            $display("%0t ERROR sb_nmu: AWBURST %b, INCR only", $time, s_awburst);
        if (!srst && aw_go && (s_awsize != SZ[2:0]))
            $display("%0t ERROR sb_nmu: AWSIZE %0d, full-width beats only",
                     $time, s_awsize);
        if (!srst && ar_go && (s_arsize != SZ[2:0]))
            $display("%0t ERROR sb_nmu: ARSIZE %0d, full-width beats only",
                     $time, s_arsize);
    end
`endif
endmodule

`default_nettype wire

// Network subordinate unit: the station onto one external AXI4 subordinate.

// IDs DO NOT CROSS THE FABRIC -- routes do. The flit carries (src, tag), this
// unit issues its own local id, and a table maps back on B/R.

// Width rule: SDW <= FW. A transfer wider than the port splits into port-width
// beats before the request FIFO; read words pack back into flits after the
// response FIFO. Sub-width transfers pass through at their own size.

`default_nettype none

module sb_nsu #(
    parameter integer SDW       = 512,          // this port's AXI data width
    parameter integer SIDW      = 4,            // this port's AXI ID width
    parameter integer AW        = 40,
    parameter integer FW        = 512,
    parameter integer TAGW      = 4,
    parameter integer SRCW      = 2,
    parameter integer WOST      = 4,
    parameter integer ROST      = 4,
    parameter integer REQ_DEPTH = 16,
    parameter integer RSP_DEPTH = 16,
    parameter integer CHAN_DEPTH = 16,
    // CHAN_MEM is the LUTRAM lever: the aw/w/ar queues are 816 LUTRAM on a
    // 512-bit port, and "block" trades all of it for one BRAM.
    parameter         REQ_MEM   = "block",
    parameter         RSP_MEM   = "block",
    parameter         CHAN_MEM  = "distributed",
    // Non-zero overrides the three above and picks per FIFO: what ONE BRAM is
    // worth in LUTs. KohakuTPU trades at ~820, crossover near depth 360.
    parameter integer LUT_PER_BRAM = 0,
    // 0 disables. Bounds how long a hung subordinate holds a REQ buffer:
    // SLVERR on expiry, slot becomes a zombie so a late response is swallowed.
    parameter integer TIMEOUT = 0
)(
    input  wire                bus_clk,
    input  wire                bus_rst,

    input  wire                req_valid,
    output wire                req_ready,
    input  wire [SRCW-1:0]     req_src,
    input  wire [TAGW-1:0]     req_tag,
    input  wire                req_wr,
    input  wire                req_head,
    input  wire                req_last,
    input  wire [AW-1:0]       req_addr,
    input  wire [7:0]          req_len,
    input  wire [2:0]          req_size,
    input  wire [FW-1:0]       req_data,
    input  wire [FW/8-1:0]     req_strb,

    output wire                rsp_valid,
    input  wire                rsp_ready,
    output wire [SRCW-1:0]     rsp_dst,
    output wire [TAGW-1:0]     rsp_tag,
    output wire                rsp_wr,
    output wire                rsp_last,
    output wire [1:0]          rsp_resp,
    output wire [FW-1:0]       rsp_data,

    // ---- external AXI4 subordinate attaches here --------------------------
    input  wire                m_aclk,
    input  wire                m_aresetn,

    output wire [SIDW-1:0]     m_awid,
    output wire [AW-1:0]       m_awaddr,
    output wire [7:0]          m_awlen,
    output wire [2:0]          m_awsize,
    output wire [1:0]          m_awburst,
    output wire                m_awvalid,
    input  wire                m_awready,

    output wire [SDW-1:0]      m_wdata,
    output wire [SDW/8-1:0]    m_wstrb,
    output wire                m_wlast,
    output wire                m_wvalid,
    input  wire                m_wready,

    input  wire [SIDW-1:0]     m_bid,
    input  wire [1:0]          m_bresp,
    input  wire                m_bvalid,
    output wire                m_bready,

    output wire [SIDW-1:0]     m_arid,
    output wire [AW-1:0]       m_araddr,
    output wire [7:0]          m_arlen,
    output wire [2:0]          m_arsize,
    output wire [1:0]          m_arburst,
    output wire                m_arvalid,
    input  wire                m_arready,

    input  wire [SIDW-1:0]     m_rid,
    input  wire [SDW-1:0]      m_rdata,
    input  wire [1:0]          m_rresp,
    input  wire                m_rlast,
    input  wire                m_rvalid,
    output wire                m_rready
);
    localparam integer FBW    = $clog2(FW/8);
    localparam integer SBW    = $clog2(SDW/8);
    localparam integer NSLICE = (FW > SDW) ? (FW / SDW) : 1;
    localparam integer SLW    = (NSLICE <= 1) ? 1 : $clog2(NSLICE);

    // No gather path: SDW > FW elaborates and synthesises cleanly while
    // corrupting every wide beat. The undefined module is the abort.
    generate if (SDW > FW) begin : g_width_rule
        sb_nsu_requires_SDW_le_FW u_illegal ();
    end endgenerate
    localparam integer WIDW   = (WOST <= 1) ? 1 : $clog2(WOST);
    localparam integer RIDW   = (ROST <= 1) ? 1 : $clog2(ROST);
    // SDW, not FW: slice on the WRITE side so a 32-bit port queues 96 bits
    // instead of 636. The offset has to be tracked in bus_clk to do it.
    localparam integer RQW    = SRCW + TAGW + 3 + AW + 8 + 3 + SDW + SDW/8;
    // SDW, not FW: the flit is assembled AFTER this FIFO, so queuing the wide
    // form would buy nothing and cost 8 RAMB36 of pure width. The slice index
    // rides along so the packer knows which lane a word belongs in.
    localparam integer RSW    = SRCW + TAGW + 2 + 2 + SLW + SDW;
    localparam integer AQW    = AW + 8 + 3;
    localparam integer WQW    = SDW + SDW/8 + 1;
    // XPM's async FIFO $finish-es below depth 16 rather than warning.
    localparam integer RQD    = (REQ_DEPTH  < 16) ? 16 : REQ_DEPTH;
    localparam integer RSD    = (RSP_DEPTH  < 16) ? 16 : RSP_DEPTH;
    localparam integer CHD    = (CHAN_DEPTH < 16) ? 16 : CHAN_DEPTH;

    wire mrst = !m_aresetn;

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

    localparam A_REQ = (lram_lut(RQW, RQD) > LUT_PER_BRAM * bram_tiles(RQW, RQD)) ? "block" : "distributed";
    localparam A_RSP = (lram_lut(RSW, RSD) > LUT_PER_BRAM * bram_tiles(RSW, RSD)) ? "block" : "distributed";
    localparam A_CHN = (lram_lut(WQW, CHD) > LUT_PER_BRAM * bram_tiles(WQW, CHD)) ? "block" : "distributed";

    localparam REQ_STY = (LUT_PER_BRAM > 0) ? A_REQ : REQ_MEM;
    localparam RSP_STY = (LUT_PER_BRAM > 0) ? A_RSP : RSP_MEM;
    localparam CHN_STY = (LUT_PER_BRAM > 0) ? A_CHN : CHAN_MEM;

    wire            awq_full, wq_full, arq_full, rsf_full, rsf_empty;
    wire            b_go, r_go;
    wire [WIDW-1:0] bw_id = m_bid[WIDW-1:0];
    wire [RIDW-1:0] rd_id = m_rid[RIDW-1:0];
    wire [WIDW-1:0] m_awid_lo;
    wire [RIDW-1:0] m_arid_lo;
    wire [SDW-1:0]  o_rdata;

    // ======================================================== inbound crossing
    // Local copy: undivided the bus reset's fanout caps bus_clk at 284 MHz
    // against 360-510 on the port clocks.
    // dont_touch: these copies are identical FFs, so Vivado merges them back
    // into the one high-fanout net they exist to break.
    (* dont_touch = "yes" *) reg bus_rst_q;
    always @(posedge bus_clk) begin
        bus_rst_q <= bus_rst;
    end

    wire [RQW-1:0] rq;
    wire           rq_empty, rqf_full, rq_pop;

    // WRITE UNPACK, before the FIFO so it stays SDW wide: a flit whose beat is
    // wider than this port becomes one FIFO ENTRY PER WORD, and the flit is
    // held (req_ready low) until its last word. At size <= SBW this is one
    // entry and the walk below is the lane pick it always was.
    reg  [FBW-1:0] w_off;
    reg  [2:0]     w_sz;
    reg            w_body;
    reg  [SLW-1:0] wsub;
    wire [2:0]     w_size  = req_head ? req_size : w_sz;
    // WRITES only: a read is one header flit however wide its beats are, and
    // slicing it makes NSLICE-1 headless entries that block the queue forever.
    wire [4:0]     w_nsub  = ((NSLICE > 1) && req_wr && (w_size > SBW[2:0]))
                             ? ((5'd1 << (w_size - SBW[2:0])) - 5'd1) : 5'd0;
    wire           w_multi = (w_nsub != 5'd0);
    wire           w_subl  = ({{(5-SLW){1'b0}}, wsub} == w_nsub);
    wire [FBW-1:0] w_use   = req_head ? req_addr[FBW-1:0] : w_off;
    wire [SLW-1:0] w_slice = (NSLICE <= 1) ? {SLW{1'b0}}
                                           : (w_use[FBW-1 -: SLW] + wsub);
    wire           w_take  = req_valid && !rqf_full;
    // Flit-level head and last become ENTRY-level: only the first slice of
    // the first flit opens the burst, only the last slice of the last closes.
    wire           w_head  = req_head && (wsub == {SLW{1'b0}});
    wire           w_last  = req_last && w_subl;

    always @(posedge bus_clk) begin
        if (bus_rst_q) begin
            w_body <= 1'b0;
            wsub   <= {SLW{1'b0}};
        end else if (w_take) begin
            wsub <= w_subl ? {SLW{1'b0}} : wsub + 1'b1;
            if (w_subl) begin
                if (req_head) begin
                    w_sz   <= req_size;
                    w_off  <= req_addr[FBW-1:0] + (1 << req_size);
                    w_body <= !req_last;
                end else begin
                    w_off <= w_off + (1 << w_sz);
                    if (req_last) begin
                        w_body <= 1'b0;
                    end
                end
            end
        end
    end

    async_fifo #(.DATA_WIDTH(RQW), .FIFO_DEPTH(RQD),
                 .MEMORY_TYPE(REQ_STY)) u_reqf (
        .wr_clk(bus_clk), .wr_rst(bus_rst_q), .wr_en(w_take),
        .wr_data({req_src, req_tag, req_wr, w_head, w_last, req_addr,
                  req_len, req_size,
                  req_data[w_slice*SDW     +: SDW],
                  req_strb[w_slice*(SDW/8) +: SDW/8]}),
        .wr_full(rqf_full),
        .rd_clk(m_aclk), .rd_en(rq_pop), .rd_data(rq), .rd_empty(rq_empty)
    );

    assign req_ready = !rqf_full && (!w_multi || w_subl);

    wire [SRCW-1:0] rq_src  = rq[RQW-1 -: SRCW];
    wire [TAGW-1:0] rq_tag  = rq[RQW-SRCW-1 -: TAGW];
    wire            rq_wr   = rq[RQW-SRCW-TAGW-1];
    wire            rq_head = rq[RQW-SRCW-TAGW-2];
    wire            rq_last = rq[RQW-SRCW-TAGW-3];
    wire [AW-1:0]    rq_addr = rq[SDW+SDW/8+11 +: AW];
    wire [7:0]       rq_len  = rq[SDW+SDW/8+3  +: 8];
    wire [2:0]       rq_size = rq[SDW+SDW/8    +: 3];
    wire [SDW-1:0]   rq_data = rq[SDW/8 +: SDW];
    wire [SDW/8-1:0] rq_strb = rq[SDW/8-1:0];

    // ============================================================== id tables
    reg [WOST-1:0] w_busy;
    reg [ROST-1:0] r_busy;
    reg [WOST-1:0] w_zomb;
    reg [ROST-1:0] r_zomb;
    wire b_zom = m_bvalid && w_zomb[bw_id];
    wire r_zom = m_rvalid && r_zomb[rd_id];
    reg [SRCW-1:0] wt_src [0:WOST-1];
    reg [TAGW-1:0] wt_tag [0:WOST-1];
    reg [SRCW-1:0] rt_src [0:ROST-1];
    reg [TAGW-1:0] rt_tag [0:ROST-1];

    reg [WIDW-1:0] w_new; reg w_avail;
    reg [RIDW-1:0] r_new; reg r_avail;
    integer wi, ri;
    always @(*) begin
        w_new = {WIDW{1'b0}}; w_avail = 1'b0;
        for (wi = WOST-1; wi >= 0; wi = wi - 1) begin
            if (!w_busy[wi]) begin w_new = wi[WIDW-1:0]; w_avail = 1'b1; end
        end
        r_new = {RIDW{1'b0}}; r_avail = 1'b0;
        for (ri = ROST-1; ri >= 0; ri = ri - 1) begin
            if (!r_busy[ri]) begin r_new = ri[RIDW-1:0]; r_avail = 1'b1; end
        end
    end

    // ================================================================= unpack
    reg in_body;

    wire start_wr = !rq_empty && rq_head &&  rq_wr && !in_body
                    && w_avail && !awq_full && !wq_full;
    wire start_rd = !rq_empty && rq_head && !rq_wr && !in_body
                    && r_avail && !arq_full;
    wire body_wr  = !rq_empty && in_body && !wq_full;

    assign rq_pop = start_wr || start_rd || body_wr;

    always @(posedge m_aclk) begin
        if (mrst) begin
            in_body <= 1'b0;
            w_busy  <= {WOST{1'b0}};
            r_busy  <= {ROST{1'b0}};
        end else begin
            if (start_wr) begin
                w_busy[w_new] <= 1'b1;
                wt_src[w_new] <= rq_src;
                wt_tag[w_new] <= rq_tag;
                in_body       <= !rq_last;
            end
            if (start_rd) begin
                r_busy[r_new] <= 1'b1;
                rt_src[r_new] <= rq_src;
                rt_tag[r_new] <= rq_tag;
            end
            if (body_wr && rq_last) begin
                in_body <= 1'b0;
            end
            // Free on the zombie path too, or the slot re-arms its timer and
            // fires a SECOND SLVERR for a transaction already answered.
            if (b_go || b_zom) begin
                w_busy[bw_id] <= 1'b0;
            end
            if ((r_go || r_zom) && m_rlast) begin
                r_busy[rd_id] <= 1'b0;
            end
        end
    end

    // ====================================================== AXI channel queues
    wire [AQW-1:0] awq_out, arq_out;
    wire           awq_empty, arq_empty;

    // The burst re-expressed in THIS port's beats: aligned base, length in
    // words covering the head and tail offsets, size = the port's own width.
    // A sub-word transfer stays one beat, so a Lite register op is unchanged.
    wire [17:0]   rq_bytes = ({10'd0, rq_len} + 18'd1) << rq_size;
    wire [17:0]   rq_span  = rq_bytes + {{(18-SBW){1'b0}}, rq_addr[SBW-1:0]};
    wire [17:0]   rq_wtmp  = (rq_span >> SBW)
                             + ((|rq_span[SBW-1:0]) ? 18'd1 : 18'd0);
    wire [7:0]    rq_wlen  = rq_wtmp[7:0] - 8'd1;
    wire [AW-1:0] rq_wal   = {rq_addr[AW-1:SBW], {SBW{1'b0}}};

    sync_fifo #(.DATA_WIDTH(AQW + WIDW), .FIFO_DEPTH(CHD),
                .MEMORY_TYPE(CHN_STY)) u_awq (
        .clk(m_aclk), .rst(mrst),
        .wr_en(start_wr), .wr_data({rq_wal, rq_wlen, SBW[2:0], w_new}),
        .wr_busy(awq_full), .wr_almost(),
        .rd_en(m_awvalid && m_awready), .rd_data({awq_out, m_awid_lo}),
        .rd_busy(awq_empty)
    );

    assign m_awvalid = !awq_empty;
    assign m_awaddr  = awq_out[AQW-1 -: AW];
    assign m_awlen   = awq_out[10:3];
    assign m_awsize  = awq_out[2:0];
    assign m_awburst = 2'b01;
    assign m_awid    = {{(SIDW-WIDW){1'b0}}, m_awid_lo};

    wire [WQW-1:0] wq_out;
    wire           wq_empty;

    sync_fifo #(.DATA_WIDTH(WQW), .FIFO_DEPTH(CHD),
                .MEMORY_TYPE(CHN_STY)) u_wq (
        .clk(m_aclk), .rst(mrst),
        .wr_en(start_wr || body_wr), .wr_data({rq_data, rq_strb, rq_last}),
        .wr_busy(wq_full), .wr_almost(),
        .rd_en(m_wvalid && m_wready), .rd_data(wq_out), .rd_busy(wq_empty)
    );

    assign m_wvalid = !wq_empty;
    assign m_wdata  = wq_out[WQW-1 -: SDW];
    assign m_wstrb  = wq_out[1 +: SDW/8];
    assign m_wlast  = wq_out[0];

    sync_fifo #(.DATA_WIDTH(AQW + RIDW), .FIFO_DEPTH(CHD),
                .MEMORY_TYPE(CHN_STY)) u_arq (
        .clk(m_aclk), .rst(mrst),
        .wr_en(start_rd), .wr_data({rq_wal, rq_wlen, SBW[2:0], r_new}),
        .wr_busy(arq_full), .wr_almost(),
        .rd_en(m_arvalid && m_arready), .rd_data({arq_out, m_arid_lo}),
        .rd_busy(arq_empty)
    );

    assign m_arvalid = !arq_empty;
    assign m_araddr  = arq_out[AQW-1 -: AW];
    assign m_arlen   = arq_out[10:3];
    assign m_arsize  = arq_out[2:0];
    assign m_arburst = 2'b01;
    assign m_arid    = {{(SIDW-RIDW){1'b0}}, m_arid_lo};

    // ====================================================== response assembly
    // A B flit must not land inside an R burst -- the station holds its RSP
    // grant until `last`, so interleaving corrupts packet boundaries.
    reg r_active;

    // ------------------------------------------------------ timeout, optional
    localparam integer TOW = (TIMEOUT <= 1) ? 1 : $clog2(TIMEOUT) + 1;

    reg [TOW-1:0]  wt_age [0:WOST-1];
    reg [TOW-1:0]  rt_age [0:ROST-1];
    reg            to_pend, to_wr;
    reg [SRCW-1:0] to_src;
    reg [TAGW-1:0] to_tag;

    wire to_go = to_pend && !rsf_full && !r_active;

    // The manager was already answered at expiry, so this would be a duplicate.
    assign b_go = m_bvalid && !b_zom && !rsf_full && !r_active && !to_go;
    assign r_go = m_rvalid && !r_zom && !rsf_full && !b_go && !to_go;

    assign m_bready = b_zom || (!rsf_full && !r_active && !to_go);
    assign m_rready = r_zom || (!rsf_full && !b_go && !to_go);

    integer ti;
    always @(posedge m_aclk) begin
        if (mrst) begin
            w_zomb  <= {WOST{1'b0}};
            r_zomb  <= {ROST{1'b0}};
            to_pend <= 1'b0;
        end else begin
            if (TIMEOUT > 0) begin
                for (ti = 0; ti < WOST; ti = ti + 1) begin
                    if (w_busy[ti] && !w_zomb[ti]) begin
                        if (wt_age[ti] == TIMEOUT[TOW-1:0]) begin
                            w_zomb[ti] <= 1'b1;
                            if (!to_pend) begin
                                to_pend <= 1'b1; to_wr <= 1'b1;
                                to_src  <= wt_src[ti]; to_tag <= wt_tag[ti];
                            end
                        end
                        else begin
                            wt_age[ti] <= wt_age[ti] + 1'b1;
                        end
                    end else if (!w_busy[ti]) begin
                        wt_age[ti] <= {TOW{1'b0}};
                    end
                end

                for (ti = 0; ti < ROST; ti = ti + 1) begin
                    if (r_busy[ti] && !r_zomb[ti]) begin
                        if (rt_age[ti] == TIMEOUT[TOW-1:0]) begin
                            r_zomb[ti] <= 1'b1;
                            if (!to_pend) begin
                                to_pend <= 1'b1; to_wr <= 1'b0;
                                to_src  <= rt_src[ti]; to_tag <= rt_tag[ti];
                            end
                        end
                        else begin
                            rt_age[ti] <= rt_age[ti] + 1'b1;
                        end
                    end else if (!r_busy[ti]) begin
                        rt_age[ti] <= {TOW{1'b0}};
                    end
                end
            end

            if (to_go) begin
                to_pend <= 1'b0;
            end
            if (b_zom) begin
                w_zomb[bw_id] <= 1'b0;
            end
            if (r_zom && m_rlast) begin
                r_zomb[rd_id] <= 1'b0;
            end
        end
    end

    always @(posedge m_aclk) begin
        if (mrst) begin
            r_active <= 1'b0;
        end
        else if (r_go) begin
            r_active <= !m_rlast;
        end
    end

    // Which lane of the flit the NEXT word of this id belongs in. Loaded from
    // the request's own offset, then free-running: AXI4 forbids read-data
    // interleaving, so one counter per id is exact.
    reg [SLW-1:0] rd_sl [0:ROST-1];
    always @(posedge m_aclk) begin
        if (start_rd) begin
            rd_sl[r_new] <= (NSLICE <= 1) ? {SLW{1'b0}}
                                          : rq_addr[FBW-1 -: SLW];
        end
        if (r_go) begin
            rd_sl[rd_id] <= rd_sl[rd_id] + 1'b1;
        end
    end

    wire [RSW-1:0] rs_in =
          to_go ? { to_src, to_tag, to_wr, 1'b1, 2'b10, {SLW{1'b0}},
                    {SDW{1'b0}} }
        : b_go  ? { wt_src[bw_id], wt_tag[bw_id], 1'b1, 1'b1, m_bresp,
                    {SLW{1'b0}}, {SDW{1'b0}} }
                : { rt_src[rd_id], rt_tag[rd_id], 1'b0, m_rlast, m_rresp,
                    rd_sl[rd_id], m_rdata };

    wire [SRCW-1:0] o_dst;
    wire [TAGW-1:0] o_tag;
    wire            o_wr, o_last;
    wire [1:0]      o_resp;
    wire [SLW-1:0]  o_sl;
    wire            rsf_pop;

    async_fifo #(.DATA_WIDTH(RSW), .FIFO_DEPTH(RSD),
                 .MEMORY_TYPE(RSP_STY)) u_rspf (
        .wr_clk(m_aclk), .wr_rst(mrst), .wr_en(to_go || b_go || r_go),
        .wr_data(rs_in),
        .wr_full(rsf_full),
        .rd_clk(bus_clk), .rd_en(rsf_pop),
        .rd_data({o_dst, o_tag, o_wr, o_last, o_resp, o_sl, o_rdata}),
        .rd_empty(rsf_empty)
    );

    generate
    if (NSLICE <= 1) begin : g_nopack
        assign rsp_dst  = o_dst;  assign rsp_tag  = o_tag;
        assign rsp_wr   = o_wr;   assign rsp_last = o_last;
        assign rsp_resp = o_resp; assign rsp_data = o_rdata;
        assign rsp_valid = !rsf_empty;
        assign rsf_pop   = rsp_valid && rsp_ready;
    end else begin : g_pack
        // WORDS BECOME A FLIT HERE, so the fabric never carries a sub-width
        // beat and the manager edge sees only full flits.
        reg [FW-1:0]    acc;
        reg             fl_v, fl_wr, fl_last;
        reg [SRCW-1:0]  fl_dst;
        reg [TAGW-1:0]  fl_tag;
        reg [1:0]       fl_resp;

        wire take  = !rsf_empty && (!fl_v || rsp_ready);
        wire ends  = o_wr || o_last || (o_sl == (NSLICE[SLW-1:0] - 1'b1));

        assign rsf_pop = take;

        always @(posedge bus_clk) begin
            if (bus_rst_q) begin
                fl_v <= 1'b0;
            end
            else if (take) begin
                fl_v <= ends;
            end
            else if (rsp_ready) begin
                fl_v <= 1'b0;
            end

            if (take) begin
                acc[o_sl*SDW +: SDW] <= o_rdata;
                fl_dst <= o_dst; fl_tag <= o_tag; fl_wr <= o_wr;
                fl_last <= o_last; fl_resp <= o_resp;
            end
        end

        assign rsp_dst  = fl_dst;  assign rsp_tag  = fl_tag;
        assign rsp_wr   = fl_wr;   assign rsp_last = fl_last;
        assign rsp_resp = fl_resp; assign rsp_data = acc;
        assign rsp_valid = fl_v;
    end endgenerate

`ifndef SYNTHESIS
    // Sub-width requests are legal: the issue stage re-expresses them at port
    // width. A size wider than the flit itself is a malformed header.
    always @(posedge m_aclk) begin
        if (!mrst && (start_wr || start_rd) && ((1 << rq_size) > (FW/8))) begin
            $display("%0t ERROR sb_nsu: size %0d exceeds flit width %0d",
                     $time, rq_size, FW);
        end
    end
`endif
endmodule

`default_nettype wire

// Network subordinate unit: the station onto one external AXI4 subordinate.

// IDs DO NOT CROSS THE FABRIC -- routes do. The flit carries (src, tag), this
// unit issues its own local id, and a table maps back on B/R.

// v1 width rule: SDW <= FW, and one manager beat must fit one subordinate beat
// (1<<size <= SDW/8). Narrow manager to wide subordinate is a narrow transfer.

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
    // SDW, not FW: the response is replicated on the read side, so queuing the
    // wide flit would buy nothing and cost 8 RAMB36 of pure width.
    localparam integer RSW    = SRCW + TAGW + 2 + 2 + SDW;
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

    localparam A_REQ = (lram_lut(RQW, RQD) > LUT_PER_BRAM * bram_tiles(RQW, RQD))
                       ? "block" : "distributed";
    localparam A_RSP = (lram_lut(RSW, RSD) > LUT_PER_BRAM * bram_tiles(RSW, RSD))
                       ? "block" : "distributed";
    localparam A_CHN = (lram_lut(WQW, CHD) > LUT_PER_BRAM * bram_tiles(WQW, CHD))
                       ? "block" : "distributed";

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
    always @(posedge bus_clk) bus_rst_q <= bus_rst;

    wire [RQW-1:0] rq;
    wire           rq_empty, rqf_full, rq_pop;

    reg  [FBW-1:0] w_off;
    reg  [2:0]     w_sz;
    reg            w_body;
    wire [FBW-1:0] w_use   = req_head ? req_addr[FBW-1:0] : w_off;
    wire [SLW-1:0] w_slice = (NSLICE <= 1) ? {SLW{1'b0}}
                                           : w_use[FBW-1 -: SLW];
    wire           w_take  = req_valid && req_ready;

    always @(posedge bus_clk) begin
        if (bus_rst_q) w_body <= 1'b0;
        else if (w_take) begin
            if (req_head) begin
                w_sz   <= req_size;
                w_off  <= req_addr[FBW-1:0] + (1 << req_size);
                w_body <= !req_last;
            end else begin
                w_off <= w_off + (1 << w_sz);
                if (req_last) w_body <= 1'b0;
            end
        end
    end

    async_fifo #(.DATA_WIDTH(RQW), .FIFO_DEPTH(RQD),
                 .MEMORY_TYPE(REQ_STY)) u_reqf (
        .wr_clk(bus_clk), .wr_rst(bus_rst_q), .wr_en(w_take),
        .wr_data({req_src, req_tag, req_wr, req_head, req_last, req_addr,
                  req_len, req_size,
                  req_data[w_slice*SDW     +: SDW],
                  req_strb[w_slice*(SDW/8) +: SDW/8]}),
        .wr_full(rqf_full),
        .rd_clk(m_aclk), .rd_en(rq_pop), .rd_data(rq), .rd_empty(rq_empty)
    );

    assign req_ready = !rqf_full;

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
        for (wi = WOST-1; wi >= 0; wi = wi - 1)
            if (!w_busy[wi]) begin w_new = wi[WIDW-1:0]; w_avail = 1'b1; end
        r_new = {RIDW{1'b0}}; r_avail = 1'b0;
        for (ri = ROST-1; ri >= 0; ri = ri - 1)
            if (!r_busy[ri]) begin r_new = ri[RIDW-1:0]; r_avail = 1'b1; end
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
            if (body_wr && rq_last) in_body <= 1'b0;
            // Free on the zombie path too, or the slot re-arms its timer and
            // fires a SECOND SLVERR for a transaction already answered.
            if (b_go || b_zom)              w_busy[bw_id] <= 1'b0;
            if ((r_go || r_zom) && m_rlast) r_busy[rd_id] <= 1'b0;
        end
    end

    // ====================================================== AXI channel queues
    wire [AQW-1:0] awq_out, arq_out;
    wire           awq_empty, arq_empty;

    sync_fifo #(.DATA_WIDTH(AQW + WIDW), .FIFO_DEPTH(CHD),
                .MEMORY_TYPE(CHN_STY)) u_awq (
        .clk(m_aclk), .rst(mrst),
        .wr_en(start_wr), .wr_data({rq_addr, rq_len, rq_size, w_new}),
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
        .wr_en(start_rd), .wr_data({rq_addr, rq_len, rq_size, r_new}),
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
                for (ti = 0; ti < WOST; ti = ti + 1)
                    if (w_busy[ti] && !w_zomb[ti]) begin
                        if (wt_age[ti] == TIMEOUT[TOW-1:0]) begin
                            w_zomb[ti] <= 1'b1;
                            if (!to_pend) begin
                                to_pend <= 1'b1; to_wr <= 1'b1;
                                to_src  <= wt_src[ti]; to_tag <= wt_tag[ti];
                            end
                        end else wt_age[ti] <= wt_age[ti] + 1'b1;
                    end else if (!w_busy[ti]) wt_age[ti] <= {TOW{1'b0}};

                for (ti = 0; ti < ROST; ti = ti + 1)
                    if (r_busy[ti] && !r_zomb[ti]) begin
                        if (rt_age[ti] == TIMEOUT[TOW-1:0]) begin
                            r_zomb[ti] <= 1'b1;
                            if (!to_pend) begin
                                to_pend <= 1'b1; to_wr <= 1'b0;
                                to_src  <= rt_src[ti]; to_tag <= rt_tag[ti];
                            end
                        end else rt_age[ti] <= rt_age[ti] + 1'b1;
                    end else if (!r_busy[ti]) rt_age[ti] <= {TOW{1'b0}};
            end

            if (to_go)                    to_pend <= 1'b0;
            if (b_zom)                    w_zomb[bw_id] <= 1'b0;
            if (r_zom && m_rlast)         r_zomb[rd_id] <= 1'b0;
        end
    end

    always @(posedge m_aclk)
        if (mrst)      r_active <= 1'b0;
        else if (r_go) r_active <= !m_rlast;

    wire [RSW-1:0] rs_in =
          to_go ? { to_src, to_tag, to_wr, 1'b1, 2'b10, {SDW{1'b0}} }
        : b_go  ? { wt_src[bw_id], wt_tag[bw_id], 1'b1, 1'b1, m_bresp,
                    {SDW{1'b0}} }
                : { rt_src[rd_id], rt_tag[rd_id], 1'b0, m_rlast, m_rresp,
                    m_rdata };

    async_fifo #(.DATA_WIDTH(RSW), .FIFO_DEPTH(RSD),
                 .MEMORY_TYPE(RSP_STY)) u_rspf (
        .wr_clk(m_aclk), .wr_rst(mrst), .wr_en(to_go || b_go || r_go),
        .wr_data(rs_in),
        .wr_full(rsf_full),
        .rd_clk(bus_clk), .rd_en(rsp_valid && rsp_ready),
        .rd_data({rsp_dst, rsp_tag, rsp_wr, rsp_last, rsp_resp, o_rdata}),
        .rd_empty(rsf_empty)
    );

    // REPLICATE, do not place: the NMU lane-picks on the way out, so every
    // slice holding the same data is correct and costs no LUT at all.
    assign rsp_data  = {NSLICE{o_rdata}};
    assign rsp_valid = !rsf_empty;

`ifndef SYNTHESIS
    always @(posedge m_aclk)
        if (!mrst && (start_wr || start_rd) && ((1 << rq_size) > (SDW/8)))
            $display("%0t ERROR sb_nsu: size %0d exceeds SDW %0d, no downsizer",
                     $time, rq_size, SDW);
`endif
endmodule

`default_nettype wire

// N requesters onto ONE AXI4 master, packing SW->MW across a clock crossing.
// docs/mas/dram-port.md. NOT instantiated by mag.v; tests use a COPY of it.

`default_nettype none

module mag_dram_port #(
    parameter integer N       = 5,      // MEM_PORTS + 3
    parameter integer ADDR_W  = 34,
    parameter integer SW      = 256,    // internal beat
    parameter integer MW      = 512,    // memory beat, SW * a power of two
    parameter integer ID_W    = 4,
    parameter integer AWQ     = 16,
    parameter integer WQ      = 64,
    parameter integer BQ      = 16,
    parameter integer ARQ     = 16,
    parameter integer RQ      = 64,
    parameter         WR_MEM  = "block"
)(
    input  wire                  s_aclk,
    input  wire                  s_aresetn,

    input  wire [N-1:0]          q_valid,
    output wire [N-1:0]          q_ready,
    input  wire [N*ADDR_W-1:0]   q_addr,
    input  wire [N*16-1:0]       q_len,     // beats of SW, 0 means one
    input  wire [N-1:0]          q_write,

    input  wire [N-1:0]          w_valid,
    output wire [N-1:0]          w_ready,
    input  wire [N*SW-1:0]       w_data,

    output wire [N-1:0]          r_valid,
    input  wire [N-1:0]          r_ready,
    output wire [N*SW-1:0]       r_data,
    output wire [N-1:0]          r_last,

    output reg  [N-1:0]          b_valid,

    input  wire                  m_aclk,
    input  wire                  m_aresetn,

    output wire [ID_W-1:0]       m_awid,
    output wire [ADDR_W-1:0]     m_awaddr,
    output wire [7:0]            m_awlen,
    output wire [2:0]            m_awsize,
    output wire [1:0]            m_awburst,
    output wire                  m_awvalid,
    input  wire                  m_awready,
    output wire [MW-1:0]         m_wdata,
    output wire [MW/8-1:0]       m_wstrb,
    output wire                  m_wlast,
    output wire                  m_wvalid,
    input  wire                  m_wready,
    input  wire [ID_W-1:0]       m_bid,
    input  wire [1:0]            m_bresp,
    input  wire                  m_bvalid,
    output wire                  m_bready,
    output wire [ID_W-1:0]       m_arid,
    output wire [ADDR_W-1:0]     m_araddr,
    output wire [7:0]            m_arlen,
    output wire [2:0]            m_arsize,
    output wire [1:0]            m_arburst,
    output wire                  m_arvalid,
    input  wire                  m_arready,
    input  wire [ID_W-1:0]       m_rid,
    input  wire [MW-1:0]         m_rdata,
    input  wire [1:0]            m_rresp,
    input  wire                  m_rlast,
    input  wire                  m_rvalid,
    output wire                  m_rready
);
    localparam integer R      = MW / SW;
    localparam integer RLOG   = (R <= 1) ? 1 : $clog2(R);
    localparam integer SBYTES = SW / 8;
    localparam integer SBLOG  = $clog2(SBYTES);
    localparam integer MBYTES = MW / 8;
    localparam integer IDX_W  = (N <= 1) ? 1 : $clog2(N);
    localparam [2:0]   MSIZE  = $clog2(MBYTES);
    localparam [RLOG-1:0] RTOP = R[RLOG-1:0] - 1'b1;
    // At R=1 there is no sub-beat, so the phase is 0 and the memory address is
    // aligned to SBYTES -- not to SBYTES<<RLOG, which would clear a real bit.
    localparam integer ALOG = SBLOG + ((R == 1) ? 0 : RLOG);

    wire srst = !s_aresetn;
    wire mrst = !m_aresetn;

    // ---- every net declared before it is connected ----------------------
    wire ar_empty, aw_empty, wq_empty, bq_empty, rq_empty;
    wire ar_full, aw_full, wq_full, wsel_full, bq_full, rq_full;
    wire [IDX_W-1:0] ar_id, aw_id, bq_id, rq_id;
    wire [MW-1:0]    rq_data;
    wire [IDX_W-1:0] wsel_id;
    wire [15:0]      wsel_n;
    wire [RLOG-1:0]  wsel_ph;
    wire             wsel_empty;

    reg  [N-1:0]      rd_busy;
    reg  [IDX_W-1:0]  rr_rd, rr_wr;
    reg  [MW-1:0]     wacc;
    reg  [MW/8-1:0]   wstrb_acc;
    reg  [RLOG-1:0]   wph;
    reg  [15:0]       wleft;
    reg               wactive;
    reg  [RLOG-1:0]   rph  [0:N-1];
    reg  [15:0]       rleft[0:N-1];

    // ================================================== request arbitration

    // ONE OUTSTANDING READ PER REQUESTER: the id IS the requester, so nothing
    // sizes a scoreboard and N reads still stay concurrent.
    wire [N-1:0] rd_req = q_valid & ~q_write & ~rd_busy;
    wire [N-1:0] wr_req = q_valid &  q_write;

    // REGISTERED REQUESTS: q_valid -> scan -> q_ready was the worst path at
    // -0.110 ns, and AXI holds valid until ready so a cycle late is safe.
    // `wr_gnt` blocks a re-grant for the one cycle the snapshot lags by: reads
    // are covered by rd_busy, writes have no equivalent and double-granted.
    reg [N-1:0] rd_req_r, wr_req_r, wr_gnt;   // driven below, past wr_take

    wire [IDX_W-1:0] rd_sel, wr_sel;
    wire             rd_any, wr_any;

    mag_dram_rr #(.N(N), .IDX_W(IDX_W)) u_rr_rd (
        .req(rd_req_r & ~rd_busy), .base(rr_rd), .sel(rd_sel), .any(rd_any));
    mag_dram_rr #(.N(N), .IDX_W(IDX_W)) u_rr_wr (
        .req(wr_req_r & ~wr_gnt), .base(rr_wr), .sel(wr_sel), .any(wr_any));

    // THE ARBITER'S CHOICE IS CAPTURED, not used the same cycle: scan, mux,
    // arithmetic and ready on one path cost 49 MHz in a 6+2 mesh.
    reg              s1_rv, s1_wv;
    reg [IDX_W-1:0]  s1_rid, s1_wid;
    reg [ADDR_W-1:0] s1_rad, s1_wad;
    reg [15:0]       s1_rln, s1_wln;

    wire rd_push = s1_rv && !ar_full;
    wire wr_push = s1_wv && !aw_full && !wsel_full;
    wire rd_take = rd_any && (!s1_rv || rd_push);
    wire wr_take = wr_any && (!s1_wv || wr_push);

    wire [ADDR_W-1:0] sel_rad = q_addr[rd_sel*ADDR_W +: ADDR_W];
    wire [15:0]       sel_rln = q_len [rd_sel*16     +: 16];
    wire [ADDR_W-1:0] sel_wad = q_addr[wr_sel*ADDR_W +: ADDR_W];
    wire [15:0]       sel_wln = q_len [wr_sel*16     +: 16];

    always @(posedge s_aclk) begin
        if (srst) begin
            rd_req_r <= {N{1'b0}}; wr_req_r <= {N{1'b0}}; wr_gnt <= {N{1'b0}};
        end else begin
            rd_req_r <= rd_req;
            wr_req_r <= wr_req;
            wr_gnt   <= wr_take ? ({{(N-1){1'b0}}, 1'b1} << wr_sel) : {N{1'b0}};
        end
    end

    always @(posedge s_aclk) begin
        if (srst) begin
            s1_rv <= 1'b0; s1_wv <= 1'b0;
        end else begin
            if (rd_take) begin
                s1_rv <= 1'b1; s1_rid <= rd_sel;
                s1_rad <= sel_rad; s1_rln <= sel_rln;
            end else if (rd_push) s1_rv <= 1'b0;
            if (wr_take) begin
                s1_wv <= 1'b1; s1_wid <= wr_sel;
                s1_wad <= sel_wad; s1_wln <= sel_wln;
            end else if (wr_push) s1_wv <= 1'b0;
        end
    end

    wire [ADDR_W-1:0] rd_addr = s1_rad;
    wire [15:0]       rd_len  = s1_rln;
    wire [ADDR_W-1:0] wr_addr = s1_wad;
    wire [15:0]       wr_len  = s1_wln;

    // head_phase is the sub-beat a burst starts on; the memory address is the
    // same address aligned down to a memory beat.
    wire [RLOG-1:0] rd_ph = (R == 1) ? {RLOG{1'b0}} : rd_addr[SBLOG +: RLOG];
    wire [RLOG-1:0] wr_ph = (R == 1) ? {RLOG{1'b0}} : wr_addr[SBLOG +: RLOG];

    wire [17:0] rd_span = {1'b0, rd_len} + 18'd1 + rd_ph;
    wire [17:0] wr_span = {1'b0, wr_len} + 18'd1 + wr_ph;
    // RLOG is 1 even at R=1, so the divide has to be bypassed rather than
    // trusted: one internal beat is one memory beat there.
    wire [15:0] rd_mb = (R == 1) ? rd_len
                       : (rd_span[17:RLOG] + (|rd_span[RLOG-1:0]) - 16'd1);
    wire [15:0] wr_mb = (R == 1) ? wr_len
                       : (wr_span[17:RLOG] + (|wr_span[RLOG-1:0]) - 16'd1);

    wire [ADDR_W-1:0] rd_al = {rd_addr[ADDR_W-1:ALOG], {ALOG{1'b0}}};
    wire [ADDR_W-1:0] wr_al = {wr_addr[ADDR_W-1:ALOG], {ALOG{1'b0}}};

    wire ar_fire = rd_push;
    wire aw_fire = wr_push;

    // ================================================== AR / AW crossings
    async_fifo #(.DATA_WIDTH(ADDR_W+8+IDX_W), .FIFO_DEPTH(ARQ)) u_arq (
        .wr_clk(s_aclk), .wr_rst(srst), .wr_en(ar_fire),
        .wr_data({rd_al, rd_mb[7:0], s1_rid}), .wr_full(ar_full),
        .rd_clk(m_aclk), .rd_en(m_arvalid && m_arready),
        .rd_data({m_araddr, m_arlen, ar_id}), .rd_empty(ar_empty));

    assign m_arvalid = !ar_empty;
    assign m_arsize  = MSIZE;
    assign m_arburst = 2'b01;
    assign m_arid    = {{(ID_W-IDX_W){1'b0}}, ar_id};

    async_fifo #(.DATA_WIDTH(ADDR_W+8+IDX_W), .FIFO_DEPTH(AWQ)) u_awq (
        .wr_clk(s_aclk), .wr_rst(srst), .wr_en(aw_fire),
        .wr_data({wr_al, wr_mb[7:0], s1_wid}), .wr_full(aw_full),
        .rd_clk(m_aclk), .rd_en(m_awvalid && m_awready),
        .rd_data({m_awaddr, m_awlen, aw_id}), .rd_empty(aw_empty));

    assign m_awvalid = !aw_empty;
    assign m_awsize  = MSIZE;
    assign m_awburst = 2'b01;
    assign m_awid    = {{(ID_W-IDX_W){1'b0}}, aw_id};

    // ================================================== write data

    // AXI4 FORBIDS W INTERLEAVING, so W follows AW order: `wsel` names whose
    // data the mux forwards and for how many source beats.
    wire [SW-1:0] w_beat = w_data[wsel_id*SW +: SW];
    wire          w_fire = wactive && w_valid[wsel_id] && !wq_full;
    wire          w_end  = (wleft == 16'd1);
    wire          w_emit = (wph == RTOP) || w_end;
    wire          wsel_pop = w_fire && w_end;

    sync_fifo #(.DATA_WIDTH(IDX_W+16+RLOG), .FIFO_DEPTH(AWQ)) u_wsel (
        .clk(s_aclk), .rst(srst),
        .wr_en(aw_fire), .wr_data({s1_wid, wr_len, wr_ph}), .wr_busy(wsel_full),
        .wr_almost(),
        .rd_en(wsel_pop), .rd_data({wsel_id, wsel_n, wsel_ph}),
        .rd_busy(wsel_empty));

    // Strobes start CLEARED and only written lanes set them, so a partial head
    // and a partial tail both fall out with no special case.
    wire [MW-1:0]   wacc_next  = wacc |
        ({{(MW-SW){1'b0}}, w_beat} << (wph * SW));
    wire [MW/8-1:0] wstrb_next = wstrb_acc |
        ({{(MW/8-SBYTES){1'b0}}, {SBYTES{1'b1}}} << (wph * SBYTES));

    async_fifo #(.DATA_WIDTH(MW + MW/8 + 1), .FIFO_DEPTH(WQ),
                 .MEMORY_TYPE(WR_MEM)) u_wq (
        .wr_clk(s_aclk), .wr_rst(srst),
        .wr_en(w_fire && w_emit),
        .wr_data({wacc_next, wstrb_next, w_end}), .wr_full(wq_full),
        .rd_clk(m_aclk), .rd_en(m_wvalid && m_wready),
        .rd_data({m_wdata, m_wstrb, m_wlast}), .rd_empty(wq_empty));

    assign m_wvalid = !wq_empty;

    always @(posedge s_aclk) begin
        if (srst) begin
            wactive <= 1'b0; wph <= {RLOG{1'b0}}; wleft <= 16'd0;
            wacc <= {MW{1'b0}}; wstrb_acc <= {(MW/8){1'b0}};
        end else if (!wactive) begin
            if (!wsel_empty) begin
                wactive   <= 1'b1;
                wph       <= wsel_ph;
                wleft     <= wsel_n + 16'd1;
                wacc      <= {MW{1'b0}};
                wstrb_acc <= {(MW/8){1'b0}};
            end
        end else if (w_fire) begin
            if (w_emit) begin
                wacc <= {MW{1'b0}}; wstrb_acc <= {(MW/8){1'b0}};
                wph  <= {RLOG{1'b0}};
            end else begin
                wacc <= wacc_next; wstrb_acc <= wstrb_next;
                wph  <= wph + 1'b1;
            end
            if (w_end) wactive <= 1'b0;
            else       wleft   <= wleft - 16'd1;
        end
    end

    genvar g;
    generate for (g = 0; g < N; g = g + 1) begin : g_wrdy
        assign w_ready[g] = wactive && (wsel_id == g[IDX_W-1:0]) && !wq_full;
    end endgenerate

    // ================================================== B

    // xpm_fifo_async with USE_ADV_FEATURES off does not flag an overflow, it
    // DISCARDS the write: a tied-high ready loses data silently.
    assign m_bready = !bq_full;
    async_fifo #(.DATA_WIDTH(IDX_W), .FIFO_DEPTH(BQ)) u_bq (
        .wr_clk(m_aclk), .wr_rst(mrst), .wr_en(m_bvalid && m_bready),
        .wr_data(m_bid[IDX_W-1:0]), .wr_full(bq_full),
        .rd_clk(s_aclk), .rd_en(!bq_empty), .rd_data(bq_id),
        .rd_empty(bq_empty));

    always @(posedge s_aclk) begin
        if (srst) b_valid <= {N{1'b0}};
        else begin
            b_valid <= {N{1'b0}};
            if (!bq_empty) b_valid[bq_id] <= 1'b1;
        end
    end

    // ================================================== read return

    // Each memory beat becomes up to R internal ones; the over-fetched head
    // and tail are DISCARDED rather than avoided.
    assign m_rready = !rq_full;
    async_fifo #(.DATA_WIDTH(MW+IDX_W), .FIFO_DEPTH(RQ),
                 .MEMORY_TYPE(WR_MEM)) u_rq (
        .wr_clk(m_aclk), .wr_rst(mrst), .wr_en(m_rvalid && m_rready),
        .wr_data({m_rdata, m_rid[IDX_W-1:0]}), .wr_full(rq_full),
        .rd_clk(s_aclk), .rd_en(rq_pop), .rd_data({rq_data, rq_id}),
        .rd_empty(rq_empty));

    wire [RLOG-1:0] cur_ph   = rph[rq_id];
    wire [15:0]     cur_left = rleft[rq_id];
    wire            r_emit   = !rq_empty && (cur_left != 16'd0);
    wire [SW-1:0]   r_sub    = rq_data[cur_ph*SW +: SW];
    wire            r_take   = r_emit && r_ready[rq_id];

    // COMBINATIONAL, not registered: async_fifo is show-ahead, so a pop one
    // cycle late re-reads the same beat.
    wire rq_pop = (!rq_empty && cur_left == 16'd0) ||
                  (r_take && ((cur_ph == RTOP) || (cur_left == 16'd1)));

    generate for (g = 0; g < N; g = g + 1) begin : g_rd
        assign r_valid[g] = r_emit && (rq_id == g[IDX_W-1:0]);
        assign r_data[g*SW +: SW] = r_sub;
        assign r_last[g]  = r_valid[g] && (cur_left == 16'd1);
    end endgenerate

    integer k;
    always @(posedge s_aclk) begin
        if (srst) begin
            rd_busy <= {N{1'b0}};
            for (k = 0; k < N; k = k + 1) begin
                rph[k] <= {RLOG{1'b0}}; rleft[k] <= 16'd0;
            end
        end else begin
            // Busy at CAPTURE, not at push: between the two the requester must
            // not be arbitrated again.
            if (rd_take) rd_busy[rd_sel] <= 1'b1;
            if (ar_fire) begin
                rph[s1_rid]   <= rd_ph;
                rleft[s1_rid] <= rd_len + 16'd1;
            end
            if (r_take) begin
                rleft[rq_id] <= cur_left - 16'd1;
                if (cur_left == 16'd1) rd_busy[rq_id] <= 1'b0;
                rph[rq_id] <= (cur_ph == RTOP) ? {RLOG{1'b0}} : cur_ph + 1'b1;
            end
        end
    end

    // ================================================== grant
    generate for (g = 0; g < N; g = g + 1) begin : g_qrdy
        assign q_ready[g] = (rd_take && (rd_sel == g[IDX_W-1:0])) ||
                            (wr_take && (wr_sel == g[IDX_W-1:0]));
    end endgenerate

    always @(posedge s_aclk) begin
        if (srst) begin
            rr_rd <= {IDX_W{1'b0}}; rr_wr <= {IDX_W{1'b0}};
        end else begin
            if (rd_take) rr_rd <= (rd_sel == N[IDX_W-1:0] - 1'b1)
                                  ? {IDX_W{1'b0}} : rd_sel + 1'b1;
            if (wr_take) rr_wr <= (wr_sel == N[IDX_W-1:0] - 1'b1)
                                  ? {IDX_W{1'b0}} : wr_sel + 1'b1;
        end
    end
endmodule

// Lowest set bit at or after `base`, wrapping. Both arbiters need it and
// neither should own it.
module mag_dram_rr #(
    parameter integer N     = 5,
    parameter integer IDX_W = 3
)(
    input  wire [N-1:0]     req,
    input  wire [IDX_W-1:0] base,
    output reg  [IDX_W-1:0] sel,
    output wire             any
);
    assign any = |req;
    integer i, idx;
    reg found;
    always @* begin
        sel = base; found = 1'b0;
        for (i = 0; i < N; i = i + 1) begin
            idx = (i + base) % N;
            if (!found && req[idx]) begin
                sel   = idx[IDX_W-1:0];
                found = 1'b1;
            end
        end
    end
endmodule

`default_nettype wire

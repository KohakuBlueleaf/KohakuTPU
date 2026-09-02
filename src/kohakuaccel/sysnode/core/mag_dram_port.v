// N requesters onto ONE AXI4 master, packing SW->MW across a clock crossing,
// with MAG's staging store served from BEHIND the same arbiter (a staged
// read through a one-word engine into the one return path, a staged write as
// beats of the one W stream), so nothing N-wide exists for staging.
// docs/mas/dram-port.md. Instantiated by mag.v as u_dram.
//
// THE q CONTRACT: a requester's {valid, write, addr, len} HOLDS unchanged
// until q_ready. Both arbiters decide on a REGISTERED request vector and
// sample the bus live, and the grant is one wire -- a presentation that
// switches mid-wait gets the other transaction's address captured and the
// wrong channel popped. The adapter in mag.v holds; the assert below is
// the guard.

`default_nettype none

module mag_dram_port #(
    parameter integer N       = 5,      // MEM_PORTS + 3
    parameter integer ADDR_W  = 40,
    parameter integer SW      = 256,    // internal beat
    parameter integer MW      = 512,    // memory beat, SW * a power of two
    parameter integer ID_W    = 4,
    parameter integer AWQ     = 16,
    parameter integer WQ      = 64,
    parameter integer BQ      = 16,
    parameter integer ARQ     = 16,
    parameter integer RQ      = 64,
    // Reads a requester may have in flight. 4 measures 2,744 -> 8,917 MB/s on
    // 20-word bursts (mag_dram_port_bw_tb, 300 MHz, 106 ns DRAM); verified at
    // 2 and 4 by mag_dram_port_tb's queued reads and mover_chain1/2/4.
    parameter integer RD_OUT  = 1,
    // Memory beats one AR may carry: a longer request goes out as several
    // back-to-back ARs on its id, which AXI answers in order. 0 = the whole
    // request. The Xache's read slot (kx_pxache RB_BEATS) is this bound.
    parameter integer AR_MAX  = 0,
    parameter         WR_MEM  = "block",
    // 1: the five queues cross s_aclk -> m_aclk (async FIFOs). 0: m_aclk IS
    // s_aclk and the queues are synchronous FIFOs on it -- the v8 one-clock
    // memory path, where the fabric behind this port shares the node's clock.
    parameter integer DRAM_CDC = 1,
    // 1: the one return bus is registered before it fans out to the N
    // requesters. Unregistered, each memory port's two-entry skid built the
    // bus's word select and staged 2:1 into BOTH its registers -- 709 LUT per
    // port for 518 FF against 132 on a registered input (sb_skid.v:52). One
    // cycle of return latency; the rate is unchanged.
    parameter integer R_REG    = 1,
    // The staging store on this path. 0 generates none of the staged path;
    // the store itself is mag_stage, wired to stg_* by mag.v.
    parameter integer STAGE    = 0,
    parameter [1:0]   MESH_ID  = 2'd0,
    parameter [3:0]   AP_STAGE = 4'h0
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
    // Byte enables, CARRIED not synthesised: a requester doing a partial write
    // has its untouched bytes clobbered otherwise. Tie to all ones for whole beats.
    input  wire [N*SW/8-1:0]     w_strb,

    output wire [N-1:0]          r_valid,
    input  wire [N-1:0]          r_ready,
    output wire [N*SW-1:0]       r_data,
    output wire [N-1:0]          r_last,

    output wire [N-1:0]          b_valid,

    // ---- the staging store's port B: one word per access (STAGE != 0) ----
    output wire                  stg_req,
    output wire                  stg_we,
    output wire [ADDR_W-1:0]     stg_addr,
    output wire [SW-1:0]         stg_wdata,
    output wire [SW/8-1:0]       stg_wstrb,
    input  wire                  stg_gnt,
    input  wire                  stg_rvalid,
    input  wire [SW-1:0]         stg_rdata,

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
    // SIZED: one internal beat, as a staged burst steps through the store.
    localparam [ADDR_W-1:0] ASTEP = SBYTES;

    wire srst = !s_aresetn;
    wire mrst = !m_aresetn;

    // The staging aperture. The map is absolute at 40 bits -- [39] special,
    // [38] reserved-zero, [37:36] mesh, [35:32] aperture -- written relative to
    // ADDR_W so a narrower STAGE=0 build still elaborates.
    function stg_is;
        input [ADDR_W-1:0] a;
        begin
            stg_is = (STAGE != 0)
                     && a[ADDR_W-1] && !a[ADDR_W-2]
                     && (a[ADDR_W-3 -: 2] == MESH_ID)
                     && (a[ADDR_W-5 -: 4] == AP_STAGE);
        end
    endfunction

    // ---- every net declared before it is connected ----------------------
    wire ar_empty, aw_empty, wq_empty, bq_empty, rq_empty;
    wire ar_full, aw_full, wq_full, wsel_full, bq_full, rq_full;
    wire [IDX_W-1:0] ar_id, aw_id, bq_id, rq_id;
    wire [MW-1:0]    rq_data;
    wire [IDX_W-1:0] wsel_id;
    wire [15:0]      wsel_n;
    wire [RLOG-1:0]  wsel_ph;
    wire             wsel_stg;
    wire [ADDR_W-1:0] wsel_addr;
    wire             wsel_empty;

    // Bursts a requester may hold PENDING behind the active one, and the widths
    // the counters need. RD_OUT = 1 keeps the arrays legal and never uses them.
    localparam integer PD  = (RD_OUT <= 1) ? 1 : (RD_OUT - 1);
    localparam integer PPW = (PD <= 1) ? 1 : $clog2(PD);
    localparam integer RCW = $clog2(RD_OUT + 1);

    reg  [N-1:0]      rd_busy;
    reg  [IDX_W-1:0]  rr_rd, rr_wr;
    reg  [MW-1:0]     wacc;
    reg  [MW/8-1:0]   wstrb_acc;
    reg  [RLOG-1:0]   wph;
    reg  [15:0]       wleft;
    reg               wactive;
    reg               wstg;          // the W burst on the bus lands in staging
    reg  [ADDR_W-1:0] wsaddr;        // ... at this word
    reg  [RLOG-1:0]   rph  [0:N-1];
    reg  [15:0]       rleft[0:N-1];
    reg  [N-1:0]      rleft_z;

    // Bursts issued and not yet returning. The ACTIVE burst stays in rph/rleft
    // so the return path is the same N:1 mux it always was.
    reg  [RLOG-1:0]   pph  [0:N-1][0:PD-1];
    reg  [15:0]       plen [0:N-1][0:PD-1];
    reg  [PPW-1:0]    phd  [0:N-1], ptl [0:N-1];
    reg  [RCW-1:0]    pn   [0:N-1], rd_cnt [0:N-1];

    // ---- the staged read engine: one pending, one active, one word out ----
    // `sq` holds a staged read the arbiter took while the engine was busy, so
    // the capture stage stays free for DRAM reads meanwhile.
    reg               sq_v;
    reg  [IDX_W-1:0]  sq_id;
    reg  [ADDR_W-1:0] sq_addr;
    reg  [15:0]       sq_len;
    reg               sr_v, sr_out, sr_hold, sr_nz;
    wire              stage_free;           // R_REG: the return register can load
    reg  [IDX_W-1:0]  sr_id;
    reg  [ADDR_W-1:0] sr_addr;
    reg  [15:0]       sr_left;       // words not yet requested
    reg  [SW-1:0]     sr_word;

    // ================================================== request arbitration

    // RD_OUT OUTSTANDING READS PER REQUESTER: the id IS the requester and AXI
    // returns same-id responses in order, so a queue needs no reorder buffer.
    //
    // ORDER ACROSS THE TWO STORES, per requester: a staged read waits until the
    // requester's count is zero (its DRAM reads have all returned), a DRAM read
    // waits until its staged read is neither pending, captured nor active. At
    // RD_OUT=1 `rd_busy` already says both; the masks are what keeps a larger
    // RD_OUT honest.
    wire [N-1:0] q_stg, rd_blk;
    reg              s1_rv, s1_wv, s1_rstg, s1_wstg;
    reg [IDX_W-1:0]  s1_rid, s1_wid;
    reg [ADDR_W-1:0] s1_rad, s1_wad;
    reg [15:0]       s1_rln, s1_wln;

    genvar g;
    generate for (g = 0; g < N; g = g + 1) begin : g_blk
        assign q_stg[g]  = stg_is(q_addr[g*ADDR_W +: ADDR_W]);
        assign rd_blk[g] = q_stg[g]
            ? ((rd_cnt[g] != {RCW{1'b0}}) || sq_v)
            : ((sr_v && (sr_id == g[IDX_W-1:0]))
               || (sq_v && (sq_id == g[IDX_W-1:0]))
               || (s1_rv && s1_rstg && (s1_rid == g[IDX_W-1:0])));
    end endgenerate

    wire [N-1:0] rd_req = q_valid & ~q_write & ~rd_busy & ~rd_blk;
    wire [N-1:0] wr_req = q_valid &  q_write;

    // REGISTERED REQUESTS: q_valid -> scan -> q_ready was the worst path at
    // -0.110 ns, and AXI holds valid until ready so a cycle late is safe.

    // BOTH sides need a one-cycle re-grant block for the snapshot's lag. At
    // RD_OUT > 1 rd_busy is no longer that, and a request is taken twice.
    reg [N-1:0] rd_req_r, wr_req_r, wr_gnt, rd_gnt;

    wire [IDX_W-1:0] rd_sel, wr_sel;
    wire             rd_any, wr_any;

    wire [N-1:0] rd_hot, wr_hot;

    mag_dram_rr #(.N(N), .IDX_W(IDX_W)) u_rr_rd (
        .req(rd_req_r & ~rd_busy & ~rd_gnt), .base(rr_rd), .sel(rd_sel),
        .gnt(rd_hot), .any(rd_any));
    mag_dram_rr #(.N(N), .IDX_W(IDX_W)) u_rr_wr (
        .req(wr_req_r & ~wr_gnt), .base(rr_wr), .sel(wr_sel),
        .gnt(wr_hot), .any(wr_any));

    // THE ARBITER'S CHOICE IS CAPTURED, not used the same cycle: scan, mux,
    // arithmetic and ready on one path cost 49 MHz in a 6+2 mesh.
    // A staged read leaves the capture stage into `sq`; a staged write needs
    // only its wsel entry, never the AW queue.
    wire ck_last;                       // the AR going out is its request's last
    wire rd_push = s1_rv && (s1_rstg ? !sq_v : (!ar_full && ck_last));
    wire wr_push = s1_wv && !wsel_full && (s1_wstg || !aw_full);
    wire rd_take = rd_any && (!s1_rv || rd_push);
    wire wr_take = wr_any && (!s1_wv || wr_push);

    wire [ADDR_W-1:0] sel_rad = q_addr[rd_sel*ADDR_W +: ADDR_W];
    wire [15:0]       sel_rln = q_len [rd_sel*16     +: 16];
    wire [ADDR_W-1:0] sel_wad = q_addr[wr_sel*ADDR_W +: ADDR_W];
    wire [15:0]       sel_wln = q_len [wr_sel*16     +: 16];

    always @(posedge s_aclk) begin
        if (srst) begin
            rd_req_r <= {N{1'b0}}; wr_req_r <= {N{1'b0}}; wr_gnt <= {N{1'b0}};
            rd_gnt   <= {N{1'b0}};
        end else begin
            rd_req_r <= rd_req;
            wr_req_r <= wr_req;
            wr_gnt   <= wr_take ? ({{(N-1){1'b0}}, 1'b1} << wr_sel) : {N{1'b0}};
            rd_gnt   <= rd_take ? ({{(N-1){1'b0}}, 1'b1} << rd_sel) : {N{1'b0}};
        end
    end

    always @(posedge s_aclk) begin
        if (srst) begin
            s1_rv <= 1'b0; s1_wv <= 1'b0;
        end else begin
            if (rd_take) begin
                s1_rv <= 1'b1; s1_rid <= rd_sel;
                s1_rad <= sel_rad; s1_rln <= sel_rln;
                s1_rstg <= stg_is(sel_rad);
            end else if (rd_push) begin
                s1_rv <= 1'b0;
            end
            if (wr_take) begin
                s1_wv <= 1'b1; s1_wid <= wr_sel;
                s1_wad <= sel_wad; s1_wln <= sel_wln;
                s1_wstg <= stg_is(sel_wad);
            end else if (wr_push) begin
                s1_wv <= 1'b0;
            end
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

    // The AR split: AMB memory beats a burst, ck_done of the request already
    // out. The return side is told once, at the first AR, and counts the
    // request's beats across its bursts; the address stays memory-aligned.
    localparam integer AMB   = (AR_MAX > 0 && AR_MAX < 256) ? AR_MAX : 256;
    localparam integer MBLOG = $clog2(MBYTES);
    reg  [15:0]       ck_done;
    wire [16:0]       ck_left = {1'b0, rd_mb} + 17'd1 - {1'b0, ck_done};
    assign            ck_last = (ck_left <= AMB);
    wire [7:0]        ck_len  = ck_last ? (ck_left[7:0] - 8'd1) : (AMB[7:0] - 8'd1);
    wire [ADDR_W-1:0] ck_addr = rd_al + ({{(ADDR_W-16){1'b0}}, ck_done} << MBLOG);

    // Only a DRAM request reaches AXI; a staged one is pushed elsewhere.
    wire ar_fire = s1_rv && !s1_rstg && !ar_full;
    wire ar_req  = ar_fire && (ck_done == 16'd0);
    wire aw_fire = wr_push && !s1_wstg;
    wire sq_fire = rd_push &&  s1_rstg;
    wire ws_fire = wr_push;

    always @(posedge s_aclk) begin
        if (srst || rd_take) begin ck_done <= 16'd0; end
        else if (ar_fire)    begin ck_done <= ck_last ? 16'd0 : ck_done + AMB[15:0]; end
    end

    // ================================================== AR / AW crossings
    // Both FIFO kinds are show-ahead with the same flags, so each queue is one
    // generate pair and nothing downstream knows which was built.
    generate if (DRAM_CDC) begin : g_arq
        async_fifo #(.DATA_WIDTH(ADDR_W+8+IDX_W), .FIFO_DEPTH(ARQ)) u_f (
            .wr_clk(s_aclk), .wr_rst(srst), .wr_en(ar_fire),
            .wr_data({ck_addr, ck_len, s1_rid}), .wr_full(ar_full),
            .rd_clk(m_aclk), .rd_en(m_arvalid && m_arready),
            .rd_data({m_araddr, m_arlen, ar_id}), .rd_empty(ar_empty));
    end else begin : g_arq_s
        sync_fifo #(.DATA_WIDTH(ADDR_W+8+IDX_W), .FIFO_DEPTH(ARQ)) u_f (
            .clk(s_aclk), .rst(srst), .wr_en(ar_fire),
            .wr_data({ck_addr, ck_len, s1_rid}), .wr_busy(ar_full), .wr_almost(),
            .rd_en(m_arvalid && m_arready),
            .rd_data({m_araddr, m_arlen, ar_id}), .rd_busy(ar_empty));
    end endgenerate

    assign m_arvalid = !ar_empty;
    assign m_arsize  = MSIZE;
    assign m_arburst = 2'b01;
    assign m_arid    = {{(ID_W-IDX_W){1'b0}}, ar_id};

    generate if (DRAM_CDC) begin : g_awq
        async_fifo #(.DATA_WIDTH(ADDR_W+8+IDX_W), .FIFO_DEPTH(AWQ)) u_f (
            .wr_clk(s_aclk), .wr_rst(srst), .wr_en(aw_fire),
            .wr_data({wr_al, wr_mb[7:0], s1_wid}), .wr_full(aw_full),
            .rd_clk(m_aclk), .rd_en(m_awvalid && m_awready),
            .rd_data({m_awaddr, m_awlen, aw_id}), .rd_empty(aw_empty));
    end else begin : g_awq_s
        sync_fifo #(.DATA_WIDTH(ADDR_W+8+IDX_W), .FIFO_DEPTH(AWQ)) u_f (
            .clk(s_aclk), .rst(srst), .wr_en(aw_fire),
            .wr_data({wr_al, wr_mb[7:0], s1_wid}), .wr_busy(aw_full), .wr_almost(),
            .rd_en(m_awvalid && m_awready),
            .rd_data({m_awaddr, m_awlen, aw_id}), .rd_busy(aw_empty));
    end endgenerate

    assign m_awvalid = !aw_empty;
    assign m_awsize  = MSIZE;
    assign m_awburst = 2'b01;
    assign m_awid    = {{(ID_W-IDX_W){1'b0}}, aw_id};

    // ================================================== write data

    // AXI4 FORBIDS W INTERLEAVING, so W follows AW order: `wsel` names whose
    // data the mux forwards, for how many source beats, and where it lands --
    // a staged burst carries its address here, since it has no AW entry.
    wire [SW-1:0]     w_beat  = w_data[wsel_id*SW +: SW];
    wire [SBYTES-1:0] w_bstrb = w_strb[wsel_id*SBYTES +: SBYTES];
    wire          sr_go;                  // the read engine owns port B this cycle
    wire          w_fire_d = wactive && !wstg && w_valid[wsel_id] && !wq_full;
    wire          w_fire_s = wactive &&  wstg && w_valid[wsel_id] && !sr_go;
    wire          w_fire   = w_fire_d || w_fire_s;
    wire          w_end    = (wleft == 16'd1);
    wire          w_emit   = (R == 1) || (wph == RTOP) || w_end;
    wire          wsel_pop = w_fire && w_end;

    sync_fifo #(.DATA_WIDTH(IDX_W+16+RLOG+1+ADDR_W), .FIFO_DEPTH(AWQ)) u_wsel (
        .clk(s_aclk), .rst(srst),
        .wr_en(ws_fire), .wr_data({s1_wid, wr_len, wr_ph, s1_wstg, wr_addr}),
        .wr_busy(wsel_full), .wr_almost(),
        .rd_en(wsel_pop), .rd_data({wsel_id, wsel_n, wsel_ph, wsel_stg, wsel_addr}),
        .rd_busy(wsel_empty));

    // Strobes start CLEARED and only written lanes set them, so a partial head
    // and a partial tail both fall out with no special case.
    wire [MW-1:0]   wacc_next;
    wire [MW/8-1:0] wstrb_next;

    generate if (R == 1) begin : g_pack1
        // One source beat IS one memory beat, so the packer is the identity.
        // Written out because `wph` reaches here through u_wsel and XPM memory
        // is opaque: the tool cannot fold the shift away, and it measured
        // 288 LUT + 288 FF of accumulator that never accumulates.
        assign wacc_next  = w_beat;
        assign wstrb_next = w_bstrb;
    end else begin : g_packn
        assign wacc_next  = wacc
                          | ({{(MW-SW){1'b0}}, w_beat} << (wph * SW));
        assign wstrb_next = wstrb_acc
                          | ({{(MW/8-SBYTES){1'b0}}, w_bstrb} << (wph * SBYTES));
    end endgenerate

    generate if (DRAM_CDC) begin : g_wq
        async_fifo #(.DATA_WIDTH(MW + MW/8 + 1), .FIFO_DEPTH(WQ),
                     .MEMORY_TYPE(WR_MEM)) u_f (
            .wr_clk(s_aclk), .wr_rst(srst),
            .wr_en(w_fire_d && w_emit),
            .wr_data({wacc_next, wstrb_next, w_end}), .wr_full(wq_full),
            .rd_clk(m_aclk), .rd_en(m_wvalid && m_wready),
            .rd_data({m_wdata, m_wstrb, m_wlast}), .rd_empty(wq_empty));
    end else begin : g_wq_s
        sync_fifo #(.DATA_WIDTH(MW + MW/8 + 1), .FIFO_DEPTH(WQ),
                    .MEMORY_TYPE(WR_MEM)) u_f (
            .clk(s_aclk), .rst(srst),
            .wr_en(w_fire_d && w_emit),
            .wr_data({wacc_next, wstrb_next, w_end}), .wr_busy(wq_full), .wr_almost(),
            .rd_en(m_wvalid && m_wready),
            .rd_data({m_wdata, m_wstrb, m_wlast}), .rd_busy(wq_empty));
    end endgenerate

    assign m_wvalid = !wq_empty;

    always @(posedge s_aclk) begin
        // `wacc`/`wstrb_acc` are not reset: every burst clears them at its
        // start and every emit clears them again, so the reset is a second
        // initialisation. `wsaddr` is a payload `wstg` qualifies.
        if (srst) begin
            wactive <= 1'b0; wph <= {RLOG{1'b0}}; wleft <= 16'd0; wstg <= 1'b0;
        end else if (!wactive) begin
            if (!wsel_empty) begin
                wactive   <= 1'b1;
                wph       <= wsel_ph;
                wleft     <= wsel_n + 16'd1;
                wstg      <= wsel_stg;
                wsaddr    <= wsel_addr;
                wacc      <= {MW{1'b0}};
                wstrb_acc <= {(MW/8){1'b0}};
            end
        end else if (w_fire) begin
            if (wstg) begin
                wsaddr <= wsaddr + ASTEP;
            end else if (w_emit) begin
                wacc <= {MW{1'b0}}; wstrb_acc <= {(MW/8){1'b0}};
                wph  <= {RLOG{1'b0}};
            end else begin
                wacc <= wacc_next; wstrb_acc <= wstrb_next;
                wph  <= wph + 1'b1;
            end
            if (w_end) begin
                wactive <= 1'b0;
            end
            else begin
                wleft   <= wleft - 16'd1;
            end
        end
    end

    // A DRAM burst's beats wait on the write queue; a staged burst's on the
    // store's port, which the read engine takes first. Neither depends on the
    // requester's valid.
    generate for (g = 0; g < N; g = g + 1) begin : g_wrdy
        assign w_ready[g] = wactive && (wsel_id == g[IDX_W-1:0])
                          && (wstg ? !sr_go : !wq_full);
    end endgenerate

    // ================================================== B

    // xpm_fifo_async with USE_ADV_FEATURES off does not flag an overflow, it
    // DISCARDS the write: a tied-high ready loses data silently.
    assign m_bready = !bq_full;
    generate if (DRAM_CDC) begin : g_bq
        async_fifo #(.DATA_WIDTH(IDX_W), .FIFO_DEPTH(BQ)) u_f (
            .wr_clk(m_aclk), .wr_rst(mrst), .wr_en(m_bvalid && m_bready),
            .wr_data(m_bid[IDX_W-1:0]), .wr_full(bq_full),
            .rd_clk(s_aclk), .rd_en(!bq_empty), .rd_data(bq_id),
            .rd_empty(bq_empty));
    end else begin : g_bq_s
        sync_fifo #(.DATA_WIDTH(IDX_W), .FIFO_DEPTH(BQ)) u_f (
            .clk(s_aclk), .rst(srst), .wr_en(m_bvalid && m_bready),
            .wr_data(m_bid[IDX_W-1:0]), .wr_busy(bq_full), .wr_almost(),
            .rd_en(!bq_empty), .rd_data(bq_id), .rd_busy(bq_empty));
    end endgenerate

    // A staged write is complete when its last beat lands; its B is OWED and
    // paid on the first cycle the requester's bit is not carrying a DRAM B, so
    // two responses for one requester never merge into one pulse.
    wire sb_done = w_fire_s && w_end;

    generate for (g = 0; g < N; g = g + 1) begin : g_b
        reg        b_q;
        reg  [4:0] owed;
        wire bq_hit = !bq_empty && (bq_id == g[IDX_W-1:0]);
        wire sb_pay = (owed != 5'd0) && !bq_hit;
        wire sb_inc = sb_done && (wsel_id == g[IDX_W-1:0]);
        always @(posedge s_aclk) begin
            if (srst) begin
                b_q  <= 1'b0;
                owed <= 5'd0;
            end
            else begin
                b_q  <= bq_hit || sb_pay;
                owed <= owed + (sb_inc ? 5'd1 : 5'd0) - (sb_pay ? 5'd1 : 5'd0);
            end
        end
        assign b_valid[g] = b_q;
    end endgenerate

    // ================================================== the staged read engine
    // ONE WORD OUT AT A TIME: the store answers a fixed RTOT cycles after the
    // request and nothing here can stall it, so a word is held until its
    // requester takes it before the next is asked for.
    assign sr_go = sr_v && sr_nz && !sr_out && !sr_hold;
    wire   sr_take = sr_hold && ((R_REG != 0) ? stage_free : r_ready[sr_id]);
    wire   sr_fin  = sr_take && !sr_nz;

    always @(posedge s_aclk) begin
        if (srst) begin
            sq_v <= 1'b0; sr_v <= 1'b0; sr_out <= 1'b0; sr_hold <= 1'b0;
            sr_nz <= 1'b0;
        end
        else begin
            if (sq_fire) begin
                sq_v <= 1'b1; sq_id <= s1_rid; sq_addr <= s1_rad; sq_len <= s1_rln;
            end
            if (!sr_v && sq_v) begin
                sr_v <= 1'b1; sr_id <= sq_id; sr_addr <= sq_addr;
                sr_left <= sq_len + 16'd1; sr_nz <= 1'b1;
                sq_v <= sq_fire;            // a take and a load in one cycle
            end
            if (sr_go && stg_gnt) begin
                sr_out  <= 1'b1;
                sr_addr <= sr_addr + ASTEP;
                sr_left <= sr_left - 16'd1;
                sr_nz   <= (sr_left != 16'd1);
            end
            if (stg_rvalid) begin
                sr_word <= stg_rdata; sr_hold <= 1'b1; sr_out <= 1'b0;
            end
            if (sr_take) begin
                sr_hold <= 1'b0;
                if (!sr_nz) begin
                    sr_v <= 1'b0;
                end
            end
        end
    end

    // Port B: the read engine first, the W stream's staged beats otherwise.
    assign stg_req   = sr_go || w_fire_s;
    assign stg_we    = !sr_go;
    assign stg_addr  = sr_go ? sr_addr : wsaddr;
    assign stg_wdata = w_beat;
    assign stg_wstrb = w_bstrb;

    // ================================================== read return

    // Each memory beat becomes up to R internal ones; the over-fetched head
    // and tail are DISCARDED rather than avoided.
    assign m_rready = !rq_full;
    generate if (DRAM_CDC) begin : g_rq
        async_fifo #(.DATA_WIDTH(MW+IDX_W), .FIFO_DEPTH(RQ),
                     .MEMORY_TYPE(WR_MEM)) u_f (
            .wr_clk(m_aclk), .wr_rst(mrst), .wr_en(m_rvalid && m_rready),
            .wr_data({m_rdata, m_rid[IDX_W-1:0]}), .wr_full(rq_full),
            .rd_clk(s_aclk), .rd_en(rq_pop), .rd_data({rq_data, rq_id}),
            .rd_empty(rq_empty));
    end else begin : g_rq_s
        sync_fifo #(.DATA_WIDTH(MW+IDX_W), .FIFO_DEPTH(RQ),
                    .MEMORY_TYPE(WR_MEM)) u_f (
            .clk(s_aclk), .rst(srst), .wr_en(m_rvalid && m_rready),
            .wr_data({m_rdata, m_rid[IDX_W-1:0]}), .wr_busy(rq_full), .wr_almost(),
            .rd_en(rq_pop), .rd_data({rq_data, rq_id}), .rd_busy(rq_empty));
    end endgenerate

    wire [RLOG-1:0] cur_ph   = rph[rq_id];
    wire [15:0]     cur_left = rleft[rq_id];
    // THE ONE RETURN BUS. A held staged word drives it ahead of the DRAM head,
    // whose beat waits: the same in-order head-of-line the queue already has.
    wire            stg_emit = sr_hold;
    wire            r_emit   = !rq_empty && (cur_left != 16'd0) && !stg_emit;
    wire [SW-1:0]   r_sub    = rq_data[cur_ph*SW +: SW];
    wire [SW-1:0]   r_bus    = stg_emit ? sr_word : r_sub;
    wire            r_take   = r_emit && ((R_REG != 0) ? stage_free : r_ready[rq_id]);

    // COMBINATIONAL, not registered: async_fifo is show-ahead, so a pop one
    // cycle late re-reads the same beat. The discard of an over-fetched beat
    // needs no bus and goes on under a staged word.
    wire rq_pop = (
        (!rq_empty && cur_left == 16'd0)
        || (r_take && ((cur_ph == RTOP) || (cur_left == 16'd1)))
    );

    wire [N-1:0] ri_valid, ri_last;
    generate for (g = 0; g < N; g = g + 1) begin : g_rd
        wire d_here = r_emit   && (rq_id == g[IDX_W-1:0]);
        wire s_here = stg_emit && (sr_id == g[IDX_W-1:0]);
        assign ri_valid[g] = d_here || s_here;
        assign ri_last[g]  = (d_here && (cur_left == 16'd1)) || (s_here && !sr_nz);
    end endgenerate

    // A take moves the beat INTO the register; it drains when its requester is
    // ready, and a drain and a load share a cycle, so the rate is one per cycle.
    generate if (R_REG != 0) begin : g_rreg
        reg [N-1:0]  rq_v;
        reg          rq_last;
        reg [SW-1:0] rq_bus;
        assign stage_free = !(|rq_v) || (|(rq_v & r_ready));
        always @(posedge s_aclk) begin
            if (srst) begin
                rq_v <= {N{1'b0}};
            end
            else if (stage_free) begin
                rq_v <= ri_valid;
            end
        end
        always @(posedge s_aclk) begin
            if (stage_free) begin
                rq_last <= |ri_last;
                rq_bus  <= r_bus;
            end
        end
        assign r_valid = rq_v;
        assign r_last  = rq_v & {N{rq_last}};
        for (g = 0; g < N; g = g + 1) begin : g_rd_q
            assign r_data[g*SW +: SW] = rq_bus;
        end
    end else begin : g_rwire
        assign stage_free = 1'b0;
        assign r_valid = ri_valid;
        assign r_last  = ri_last;
        for (g = 0; g < N; g = g + 1) begin : g_rd_w
            assign r_data[g*SW +: SW] = r_bus;
        end
    end endgenerate

    // A burst ENDS on its last internal beat, and the next must become active
    // in the SAME cycle or a bubble opens between back-to-back returns.
    wire r_fin = r_take && (cur_left == 16'd1);

    integer kp;
    generate for (g = 0; g < N; g = g + 1) begin : g_rdq
        wire mine_ar  = ar_req  && (s1_rid == g[IDX_W-1:0]);
        wire mine_tk  = r_take  && (rq_id  == g[IDX_W-1:0]);
        wire mine_fin = r_fin   && (rq_id  == g[IDX_W-1:0]);
        // A staged read ends on the take of its last word; it never touched
        // rleft/rph, only the count.
        wire mine_sfn = sr_fin  && (sr_id  == g[IDX_W-1:0]);

        // rleft_z tracks rleft[g]==0 as a bit, so slot_free is one OR instead
        // of a 16-bit compare: 215 paths sat at 11 levels through this.
        wire slot_free = rleft_z[g] || mine_fin;
        wire do_pop    = mine_fin && (pn[g] != {RCW{1'b0}});
        wire do_direct = mine_ar && slot_free && !do_pop;
        wire do_push   = mine_ar && !do_direct;

        // Counted at CAPTURE, not at AR: between the two the requester must not
        // be arbitrated again, which is what the old one-bit rd_busy did.
        wire [RCW-1:0] cnt_n =
            rd_cnt[g] + ((rd_take && (rd_sel == g[IDX_W-1:0])) ? 1'b1 : 1'b0)
                      - ((mine_fin || mine_sfn) ? 1'b1 : 1'b0);

        always @(posedge s_aclk) begin
            if (srst) begin
                rleft[g] <= 16'd0; rleft_z[g] <= 1'b1;
                rph[g] <= {RLOG{1'b0}};
                phd[g] <= {PPW{1'b0}}; ptl[g] <= {PPW{1'b0}};
                pn[g] <= {RCW{1'b0}}; rd_cnt[g] <= {RCW{1'b0}};
                rd_busy[g] <= 1'b0;
                for (kp = 0; kp < PD; kp = kp + 1) begin
                    pph[g][kp] <= {RLOG{1'b0}}; plen[g][kp] <= 16'd0;
                end
            end else begin
                rd_cnt[g]  <= cnt_n;
                // REGISTERED, never a combinational compare into q_ready:
                // HANDOFF-mag-dram-port.md s1f cost 49 MHz to that once.
                rd_busy[g] <= (cnt_n >= RD_OUT[RCW-1:0]);

                // Exact: loads write len+1 (len <= 256 by AXI), and mine_tk
                // cannot hit 0 -- mine_fin takes cur_left==1, r_emit bars 0.
                if (do_pop || do_direct) begin
                    rleft_z[g] <= 1'b0;
                end
                else if (mine_fin) begin
                    rleft_z[g] <= 1'b1;
                end
                else if (mine_tk) begin
                    rleft_z[g] <= 1'b0;
                end

                if (do_pop) begin
                    rleft[g] <= plen[g][phd[g]] + 16'd1;
                    rph[g]   <= pph[g][phd[g]];
                end else if (do_direct) begin
                    rleft[g] <= rd_len + 16'd1;
                    rph[g]   <= rd_ph;
                end else if (mine_fin) begin
                    rleft[g] <= 16'd0;
                end else if (mine_tk) begin
                    rleft[g] <= cur_left - 16'd1;
                    rph[g]   <= (cur_ph == RTOP) ? {RLOG{1'b0}} : cur_ph + 1'b1;
                end

                if (do_push) begin
                    pph[g][ptl[g]]  <= rd_ph;
                    plen[g][ptl[g]] <= rd_len;
                    ptl[g] <= (ptl[g] == (PD[PPW-1:0] - 1'b1))
                              ? {PPW{1'b0}} : ptl[g] + 1'b1;
                end
                if (do_pop) begin
                    phd[g] <= (phd[g] == (PD[PPW-1:0] - 1'b1))
                              ? {PPW{1'b0}} : phd[g] + 1'b1;
                end
                pn[g] <= pn[g] + (do_push ? 1'b1 : 1'b0)
                                - (do_pop  ? 1'b1 : 1'b0);
            end
        end
    end endgenerate

    // ================================================== grant
    generate for (g = 0; g < N; g = g + 1) begin : g_qrdy
        // The arbiter's own one-hot, not a decode of its encoded `sel`.
        assign q_ready[g] = (rd_take && rd_hot[g]) || (wr_take && wr_hot[g]);
    end endgenerate

    always @(posedge s_aclk) begin
        if (srst) begin
            rr_rd <= {IDX_W{1'b0}}; rr_wr <= {IDX_W{1'b0}};
        end else begin
            if (rd_take) begin
                rr_rd <= (rd_sel == N[IDX_W-1:0] - 1'b1)
                       ? {IDX_W{1'b0}} : rd_sel + 1'b1;
            end
            if (wr_take) begin
                rr_wr <= (wr_sel == N[IDX_W-1:0] - 1'b1)
                       ? {IDX_W{1'b0}} : wr_sel + 1'b1;
            end
        end
    end

`ifndef SYNTHESIS
    // The header's hold-until-ready contract, enforced: rv_mc4 measured a
    // switching presenter crossing a writeback of line 641 onto line 787.
    reg [N-1:0]        cq_held;
    reg [N-1:0]        cq_write_p;
    reg [N*ADDR_W-1:0] cq_addr_p;
    reg [N*16-1:0]     cq_len_p;
    integer ca;
    always @(posedge s_aclk) begin
        if (srst) begin
            cq_held <= {N{1'b0}};
        end
        else begin
            for (ca = 0; ca < N; ca = ca + 1) begin
                if (
                    cq_held[ca]
                    && q_valid[ca]
                    && (
                        (q_write[ca] != cq_write_p[ca])
                        || (q_addr[ca*ADDR_W +: ADDR_W]
                            != cq_addr_p[ca*ADDR_W +: ADDR_W])
                        || (q_len[ca*16 +: 16] != cq_len_p[ca*16 +: 16])
                    )
                ) begin
                    $display("%0t ERROR mag_dram_port: requester %0d changed its presentation while waiting (write %b->%b addr %h->%h) -- the arbiter will cross transactions",
                             $time, ca, cq_write_p[ca], q_write[ca],
                             cq_addr_p[ca*ADDR_W +: ADDR_W],
                             q_addr[ca*ADDR_W +: ADDR_W]);
                end
                cq_held[ca] <= q_valid[ca] && !q_ready[ca];
                if (q_valid[ca] && !cq_held[ca]) begin
                    cq_write_p[ca] <= q_write[ca];
                    cq_addr_p[ca*ADDR_W +: ADDR_W] <= q_addr[ca*ADDR_W +: ADDR_W];
                    cq_len_p[ca*16 +: 16] <= q_len[ca*16 +: 16];
                end
            end
        end
    end

    // The store must take every access this port routes to it: its decode and
    // this port's are the same bits, and port A is tied off on this path. A
    // refusal here would drop a staged beat or lose a read word silently.
    always @(posedge s_aclk) begin
        if (!srst && stg_req && !stg_gnt) begin
            $display("%0t ERROR mag_dram_port: staging refused %s at %h -- the store's decode and this port's have drifted apart",
                     $time, stg_we ? "a write" : "a read", stg_addr);
        end
    end
`endif
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
    output wire [N-1:0]     gnt,
    output wire             any
);
    assign any = |req;

    // Mask then isolate-lowest-set-bit. The serial scan this replaces was N
    // dependent levels under q_ready: 1,800 paths at 13-15 into u_mover.
    wire [N-1:0] hi   = req & ({N{1'b1}} << base);
    wire [N-1:0] pick = (|hi) ? hi : req;
    assign gnt = pick & (~pick + {{(N-1){1'b0}}, 1'b1});

    integer i;
    always @* begin
        sel = {IDX_W{1'b0}};
        for (i = 0; i < N; i = i + 1) begin
            sel = sel | (gnt[i] ? i[IDX_W-1:0] : {IDX_W{1'b0}});
        end
    end
endmodule

`default_nettype wire

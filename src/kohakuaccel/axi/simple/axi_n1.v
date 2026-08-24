// N AXI4 masters onto ONE slave, across two clock domains.
//
// What a SmartConnect does and this does not: address decode (there is one
// slave, so there is nothing to decode), width conversion (one width
// throughout), protocol conversion, and arbitrary topology. What is left is
// arbitration, response routing and the clock crossing -- and that is the whole
// module. The 4S/1M SmartConnect it replaces measured 20,104 LUT.
//
//   s_aclk domain                                     m_aclk domain
//   N x AW --RR--> [awq] ------------------------------> AW
//          push idx
//   wsel  --------> W mux, head until wlast --> [wq] --> W
//   N x AR --RR--> [arq] ------------------------------> AR
//   N x B  <-- demux by id[hi] <-- [bq] <---------------- B
//   N x R  <-- demux by id[hi] <-- [rq] <---------------- R
//
// ARBITRATE FIRST, THEN CROSS. Crossing per master would need 5N async FIFOs;
// arbitrating in the master domain needs five, whatever N is.
//
// RESPONSE ROUTING IS THE ID, NOT A TABLE. The master index is prepended to
// `awid`/`arid`, so `bid`/`rid` say where to send the response and no
// scoreboard has to be kept or sized. The slave must echo the full ID, which
// AXI4 requires -- and the slave's ID width must therefore be SID_W.
//
// Optional AXI signals (lock, cache, prot, qos, region) are not carried: no
// master in this design drives them and the slave takes its defaults, which is
// the same choice mag.v and axi_ram.v already make.

`default_nettype none

module axi_n1 #(
    parameter integer N        = 4,
    parameter integer ADDR_W   = 40,
    parameter integer DATA_W   = 256,
    parameter integer ID_W     = 4,
    // Address queues need only cover the crossing latency; W and R are sized
    // for burst throughput, so they are the two that want block RAM.
    parameter integer AW_DEPTH = 16,
    parameter integer W_DEPTH  = 64,
    parameter integer B_DEPTH  = 16,
    parameter integer AR_DEPTH = 16,
    parameter integer R_DEPTH  = 64,
    parameter         WR_MEM   = "block",
    // Derived. The port list needs them, which is why they are parameters --
    // do not override.
    parameter integer IDX_W    = (N <= 1) ? 1 : $clog2(N),
    parameter integer SID_W    = ID_W + IDX_W
)(
    // ---- master-side: N AXI4 slave interfaces, flattened ----
    input  wire                    s_aclk,
    input  wire                    s_aresetn,

    input  wire [N*ID_W-1:0]       s_awid,
    input  wire [N*ADDR_W-1:0]     s_awaddr,
    input  wire [N*8-1:0]          s_awlen,
    input  wire [N*3-1:0]          s_awsize,
    input  wire [N*2-1:0]          s_awburst,
    input  wire [N-1:0]            s_awvalid,
    output wire [N-1:0]            s_awready,

    input  wire [N*DATA_W-1:0]     s_wdata,
    input  wire [N*(DATA_W/8)-1:0] s_wstrb,
    input  wire [N-1:0]            s_wlast,
    input  wire [N-1:0]            s_wvalid,
    output wire [N-1:0]            s_wready,

    output wire [N*ID_W-1:0]       s_bid,
    output wire [N*2-1:0]          s_bresp,
    output wire [N-1:0]            s_bvalid,
    input  wire [N-1:0]            s_bready,

    input  wire [N*ID_W-1:0]       s_arid,
    input  wire [N*ADDR_W-1:0]     s_araddr,
    input  wire [N*8-1:0]          s_arlen,
    input  wire [N*3-1:0]          s_arsize,
    input  wire [N*2-1:0]          s_arburst,
    input  wire [N-1:0]            s_arvalid,
    output wire [N-1:0]            s_arready,

    output wire [N*ID_W-1:0]       s_rid,
    output wire [N*DATA_W-1:0]     s_rdata,
    output wire [N*2-1:0]          s_rresp,
    output wire [N-1:0]            s_rlast,
    output wire [N-1:0]            s_rvalid,
    input  wire [N-1:0]            s_rready,

    // ---- slave-side: one AXI4 master interface ----
    input  wire                    m_aclk,
    input  wire                    m_aresetn,

    output wire [SID_W-1:0]        m_awid,
    output wire [ADDR_W-1:0]       m_awaddr,
    output wire [7:0]              m_awlen,
    output wire [2:0]              m_awsize,
    output wire [1:0]              m_awburst,
    output wire                    m_awvalid,
    input  wire                    m_awready,

    output wire [DATA_W-1:0]       m_wdata,
    output wire [DATA_W/8-1:0]     m_wstrb,
    output wire                    m_wlast,
    output wire                    m_wvalid,
    input  wire                    m_wready,

    input  wire [SID_W-1:0]        m_bid,
    input  wire [1:0]              m_bresp,
    input  wire                    m_bvalid,
    output wire                    m_bready,

    output wire [SID_W-1:0]        m_arid,
    output wire [ADDR_W-1:0]       m_araddr,
    output wire [7:0]              m_arlen,
    output wire [2:0]              m_arsize,
    output wire [1:0]              m_arburst,
    output wire                    m_arvalid,
    input  wire                    m_arready,

    input  wire [SID_W-1:0]        m_rid,
    input  wire [DATA_W-1:0]       m_rdata,
    input  wire [1:0]              m_rresp,
    input  wire                    m_rlast,
    input  wire                    m_rvalid,
    output wire                    m_rready
);
    localparam integer AW_W = IDX_W + ID_W + ADDR_W + 8 + 3 + 2;
    localparam integer W_W  = DATA_W + DATA_W/8 + 1;
    localparam integer B_W  = SID_W + 2;
    localparam integer R_W  = SID_W + DATA_W + 2 + 1;

    wire s_rst = !s_aresetn;
    wire m_rst = !m_aresetn;

    // ================================================== AW: arbitrate and push
    // Scan downward from the pointer so the lowest-priority match is overwritten
    // by a higher-priority one -- the same shape mag.v's agent arbiter uses.
    reg  [IDX_W-1:0] aw_rr, aw_sel;
    reg              aw_any;
    integer          ai;
    always @(*) begin
        aw_sel = {IDX_W{1'b0}};
        aw_any = 1'b0;
        for (ai = N-1; ai >= 0; ai = ai - 1) begin
            if (s_awvalid[(ai + aw_rr) % N]) begin
                aw_sel = (ai + aw_rr) % N;
                aw_any = 1'b1;
            end
        end
    end

    wire            awq_full, wsel_full;
    wire            aw_go = aw_any && !awq_full && !wsel_full;

    always @(posedge s_aclk) begin
        if (s_rst) begin
            aw_rr <= {IDX_W{1'b0}};
        end
        else if (aw_go) begin
            aw_rr <= (aw_sel + 1'b1) % N;
        end
    end

    genvar g;
    generate
    for (g = 0; g < N; g = g + 1) begin : g_awrdy
        assign s_awready[g] = aw_go && (aw_sel == g);
    end
    endgenerate

    wire [AW_W-1:0] awq_in = { aw_sel,
                               s_awid   [aw_sel*ID_W   +: ID_W],
                               s_awaddr [aw_sel*ADDR_W +: ADDR_W],
                               s_awlen  [aw_sel*8      +: 8],
                               s_awsize [aw_sel*3      +: 3],
                               s_awburst[aw_sel*2      +: 2] };
    wire [AW_W-1:0] awq_out;
    wire            awq_empty;

    async_fifo #(.DATA_WIDTH(AW_W), .FIFO_DEPTH(AW_DEPTH)) u_awq (
        .wr_clk(s_aclk), .wr_rst(s_rst), .wr_en(aw_go), .wr_data(awq_in),
        .wr_full(awq_full),
        .rd_clk(m_aclk), .rd_en(m_awvalid && m_awready), .rd_data(awq_out),
        .rd_empty(awq_empty)
    );

    assign m_awvalid = !awq_empty;
    assign {m_awid, m_awaddr, m_awlen, m_awsize, m_awburst} = awq_out;

    // ============================================== W: follow the AW order
    // AXI requires a master's W beats to follow its own AW order, and once AW is
    // arbitrated the W channel is committed to that master until `wlast`. The
    // order FIFO is that commitment; getting it wrong DEADLOCKS rather than
    // corrupting, which is why it is a queue and not a register.
    wire [IDX_W-1:0] wsel_idx;
    wire             wsel_empty;
    wire             wq_full;
    wire             w_beat = !wsel_empty && !wq_full && s_wvalid[wsel_idx];
    wire             w_last_beat = w_beat && s_wlast[wsel_idx];

    sync_fifo #(.DATA_WIDTH(IDX_W), .FIFO_DEPTH(AW_DEPTH),
                .MEMORY_TYPE("distributed")) u_wsel (
        .clk(s_aclk), .rst(s_rst),
        .wr_en(aw_go), .wr_data(aw_sel), .wr_busy(), .wr_almost(wsel_full),
        .rd_en(w_last_beat), .rd_data(wsel_idx), .rd_busy(wsel_empty)
    );

    generate
    for (g = 0; g < N; g = g + 1) begin : g_wrdy
        assign s_wready[g] = !wsel_empty && !wq_full && (wsel_idx == g);
    end
    endgenerate

    wire [W_W-1:0] wq_in = { s_wdata[wsel_idx*DATA_W       +: DATA_W],
                             s_wstrb[wsel_idx*(DATA_W/8)   +: DATA_W/8],
                             s_wlast[wsel_idx] };
    wire [W_W-1:0] wq_out;
    wire           wq_empty;

    async_fifo #(.DATA_WIDTH(W_W), .FIFO_DEPTH(W_DEPTH), .MEMORY_TYPE(WR_MEM))
    u_wq (
        .wr_clk(s_aclk), .wr_rst(s_rst), .wr_en(w_beat), .wr_data(wq_in),
        .wr_full(wq_full),
        .rd_clk(m_aclk), .rd_en(m_wvalid && m_wready), .rd_data(wq_out),
        .rd_empty(wq_empty)
    );

    assign m_wvalid = !wq_empty;
    assign {m_wdata, m_wstrb, m_wlast} = wq_out;

    // ================================================== AR: arbitrate and push
    reg  [IDX_W-1:0] ar_rr, ar_sel;
    reg              ar_any;
    integer          ri;
    always @(*) begin
        ar_sel = {IDX_W{1'b0}};
        ar_any = 1'b0;
        for (ri = N-1; ri >= 0; ri = ri - 1) begin
            if (s_arvalid[(ri + ar_rr) % N]) begin
                ar_sel = (ri + ar_rr) % N;
                ar_any = 1'b1;
            end
        end
    end

    wire arq_full;
    wire ar_go = ar_any && !arq_full;

    always @(posedge s_aclk) begin
        if (s_rst) begin
            ar_rr <= {IDX_W{1'b0}};
        end
        else if (ar_go) begin
            ar_rr <= (ar_sel + 1'b1) % N;
        end
    end

    generate
    for (g = 0; g < N; g = g + 1) begin : g_arrdy
        assign s_arready[g] = ar_go && (ar_sel == g);
    end
    endgenerate

    wire [AW_W-1:0] arq_in = { ar_sel,
                               s_arid   [ar_sel*ID_W   +: ID_W],
                               s_araddr [ar_sel*ADDR_W +: ADDR_W],
                               s_arlen  [ar_sel*8      +: 8],
                               s_arsize [ar_sel*3      +: 3],
                               s_arburst[ar_sel*2      +: 2] };
    wire [AW_W-1:0] arq_out;
    wire            arq_empty;

    async_fifo #(.DATA_WIDTH(AW_W), .FIFO_DEPTH(AR_DEPTH)) u_arq (
        .wr_clk(s_aclk), .wr_rst(s_rst), .wr_en(ar_go), .wr_data(arq_in),
        .wr_full(arq_full),
        .rd_clk(m_aclk), .rd_en(m_arvalid && m_arready), .rd_data(arq_out),
        .rd_empty(arq_empty)
    );

    assign m_arvalid = !arq_empty;
    assign {m_arid, m_araddr, m_arlen, m_arsize, m_arburst} = arq_out;

    // ==================================================== B: cross and demux
    wire [B_W-1:0] bq_out;
    wire           bq_empty, bq_full;
    wire [SID_W-1:0] b_id  = bq_out[B_W-1 -: SID_W];
    wire [IDX_W-1:0] b_idx = b_id[SID_W-1 -: IDX_W];

    async_fifo #(.DATA_WIDTH(B_W), .FIFO_DEPTH(B_DEPTH)) u_bq (
        .wr_clk(m_aclk), .wr_rst(m_rst), .wr_en(m_bvalid && m_bready),
        .wr_data({m_bid, m_bresp}), .wr_full(bq_full),
        .rd_clk(s_aclk), .rd_en(!bq_empty && s_bready[b_idx]),
        .rd_data(bq_out), .rd_empty(bq_empty)
    );

    assign m_bready = !bq_full;

    generate
    for (g = 0; g < N; g = g + 1) begin : g_b
        assign s_bvalid[g] = !bq_empty && (b_idx == g);
        assign s_bid[g*ID_W +: ID_W] = b_id[ID_W-1:0];
        assign s_bresp[g*2 +: 2]     = bq_out[1:0];
    end
    endgenerate

    // ==================================================== R: cross and demux
    // HEAD-OF-LINE BY DESIGN: one return queue, so a master that is slow to take
    // its R beats holds up the others. Per-master queues would cost N times the
    // storage to remove a stall that only happens when a master stops reading,
    // which is a condition it controls.
    wire [R_W-1:0] rq_out;
    wire           rq_empty, rq_full;
    wire [SID_W-1:0] r_id  = rq_out[R_W-1 -: SID_W];
    wire [IDX_W-1:0] r_idx = r_id[SID_W-1 -: IDX_W];

    async_fifo #(.DATA_WIDTH(R_W), .FIFO_DEPTH(R_DEPTH), .MEMORY_TYPE(WR_MEM))
    u_rq (
        .wr_clk(m_aclk), .wr_rst(m_rst), .wr_en(m_rvalid && m_rready),
        .wr_data({m_rid, m_rdata, m_rresp, m_rlast}), .wr_full(rq_full),
        .rd_clk(s_aclk), .rd_en(!rq_empty && s_rready[r_idx]),
        .rd_data(rq_out), .rd_empty(rq_empty)
    );

    assign m_rready = !rq_full;

    generate
    for (g = 0; g < N; g = g + 1) begin : g_r
        assign s_rvalid[g] = !rq_empty && (r_idx == g);
        assign s_rid[g*ID_W +: ID_W]     = r_id[ID_W-1:0];
        assign s_rdata[g*DATA_W +: DATA_W] = rq_out[R_W-SID_W-1 -: DATA_W];
        assign s_rresp[g*2 +: 2]         = rq_out[2:1];
        assign s_rlast[g]                = rq_out[0];
    end
    endgenerate

`ifndef SYNTHESIS
    // A response whose index names no master is a slave that did not echo the
    // ID. Silent otherwise: the beat is offered to nobody and the queue wedges.
    always @(posedge s_aclk) begin
        if (s_aresetn && !bq_empty && (b_idx >= N)) begin
            $display("%0t ERROR axi_n1: B id %h routes to master %0d of %0d -- the slave did not echo the ID",
                     $time, b_id, b_idx, N);
        end
        if (s_aresetn && !rq_empty && (r_idx >= N)) begin
            $display("%0t ERROR axi_n1: R id %h routes to master %0d of %0d -- the slave did not echo the ID",
                     $time, r_id, r_idx, N);
        end
    end
`endif

endmodule

`default_nettype wire

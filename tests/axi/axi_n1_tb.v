// axi_n1: N masters onto one slave, across two clocks.
//
// The failure mode being hunted is a HANG, not a wrong answer, so every channel
// carries randomised backpressure and the run is watchdogged. Three properties
// are checked, and each has a way of passing vacuously that is closed here:
//
//   W ORDERING   Each master writes a pattern derived from the ADDRESS, then
//                reads it back, so a burst that lands under another master's AW
//                mismatches -- an ordering bug becomes a data error a bench can
//                see. AW and W are SEPARATE THREADS per master, so several AWs
//                are outstanding and the order queue is exercised rather than
//                being a register that happens to work.
//   ID ROUTING   Every B and R beat is checked to carry the issuing master's
//                own id. Misrouting would otherwise appear only as a hang.
//   BOTH RATIOS  The suite runs twice, master clock faster than slave and then
//                slower -- the classic async-FIFO bug works one way round.

`default_nettype none
`timescale 1ns/1ps

`ifndef NM
`define NM 4
`endif
`ifndef NTXN
`define NTXN 16
`endif

// ---------------------------------------------------------------- one master
module axi_n1_tbm #(
    parameter integer IDX    = 0,
    parameter integer ADDR_W = 40,
    parameter integer DATA_W = 256,
    parameter integer ID_W   = 4,
    parameter integer NTXN   = 16,
    parameter integer WORDS  = 512
)(
    input  wire                  aclk,
    input  wire                  go_wr,
    input  wire                  go_rd,
    output reg                   wr_done,
    output reg                   rd_done,
    output reg  [ID_W-1:0]       awid,
    output reg  [ADDR_W-1:0]     awaddr,
    output reg  [7:0]            awlen,
    output reg  [2:0]            awsize,
    output reg  [1:0]            awburst,
    output reg                   awvalid,
    input  wire                  awready,
    output reg  [DATA_W-1:0]     wdata,
    output reg  [DATA_W/8-1:0]   wstrb,
    output reg                   wlast,
    output reg                   wvalid,
    input  wire                  wready,
    input  wire [ID_W-1:0]       bid,
    input  wire                  bvalid,
    output reg                   bready,
    output reg  [ID_W-1:0]       arid,
    output reg  [ADDR_W-1:0]     araddr,
    output reg  [7:0]            arlen,
    output reg  [2:0]            arsize,
    output reg  [1:0]            arburst,
    output reg                   arvalid,
    input  wire                  arready,
    input  wire [ID_W-1:0]       rid,
    input  wire [DATA_W-1:0]     rdata,
    input  wire                  rlast,
    input  wire                  rvalid,
    output reg                   rready,
    output reg  [31:0]           checks,
    output reg  [31:0]           errors
);
    localparam integer LSB  = 5;              // 256-bit words are 32 bytes
    localparam integer BASE = IDX * WORDS;
    localparam integer SPAN = WORDS - 32;     // leaves room for the longest burst

    integer seed, aseed, wseed, bseed, rseed;
    integer aw_i, w_i, b_i;
    reg aw_done, w_done, b_done;

    // The AW thread hands the W thread each burst's shape. A queue, not a
    // register: the point of the test is that AW runs ahead of W.
    integer q_off [0:255];
    integer q_len [0:255];
    integer q_wr, q_rd;

    // What was written, so the read phase replays it rather than guessing. Each
    // transaction owns a disjoint 32-word slot, so no later write can overwrite
    // an earlier one and make a correct readback look wrong.
    integer log_off [0:255];
    integer log_len [0:255];

    // Pattern from the WORD ADDRESS alone, so writer and reader agree without
    // stored expectations and a burst landing elsewhere is visible.
    function [DATA_W-1:0] pat;
        input integer w;
        begin pat = {8{{IDX[3:0], w[27:0]}}}; end
    endfunction

    initial begin
        awvalid = 0; wvalid = 0; bready = 0; arvalid = 0; rready = 0;
        awid = IDX[ID_W-1:0]; arid = IDX[ID_W-1:0];
        awsize = LSB[2:0]; arsize = LSB[2:0];
        awburst = 2'b01; arburst = 2'b01;
        wstrb = {(DATA_W/8){1'b1}};
        wlast = 0; awlen = 0; arlen = 0; awaddr = 0; araddr = 0; wdata = 0;
        checks = 0; errors = 0;
        q_wr = 0; q_rd = 0;
        aw_done = 1; w_done = 1; b_done = 1; rd_done = 1;
        aseed = 32'h1000_0001 + IDX * 7;
        wseed = 32'h2000_0001 + IDX * 13;
        bseed = 32'h3000_0001 + IDX * 17;
        rseed = 32'h4000_0001 + IDX * 23;
        seed  = 0;
    end

    always @(*) wr_done = aw_done && w_done && b_done;

    // ---- AW ----
    integer a_off, a_len, a_gap, gk;
    initial forever begin
        @(posedge go_wr);
        aw_done = 1'b0;
        q_wr = 0; q_rd = 0;
        for (aw_i = 0; aw_i < NTXN; aw_i = aw_i + 1) begin
            a_len = {$random(aseed)} % 16;
            a_off = aw_i * 32 + ({$random(aseed)} % 16);
            log_off[aw_i] = a_off;
            log_len[aw_i] = a_len;
            q_off[q_wr % 256] = a_off;
            q_len[q_wr % 256] = a_len;
            q_wr = q_wr + 1;
            a_gap = {$random(aseed)} % 4;
            for (gk = 0; gk < a_gap; gk = gk + 1) @(posedge aclk);
            awaddr  <= (BASE + a_off) << LSB;
            awlen   <= a_len[7:0];
            awvalid <= 1'b1;
            @(posedge aclk);
            while (!awready) @(posedge aclk);
            awvalid <= 1'b0;
        end
        aw_done = 1'b1;
    end

    // ---- W ----
    integer w_off, w_len, wk;
    initial forever begin
        @(posedge go_wr);
        w_done = 1'b0;
        for (w_i = 0; w_i < NTXN; w_i = w_i + 1) begin
            while (q_rd == q_wr) @(posedge aclk);
            w_off = q_off[q_rd % 256];
            w_len = q_len[q_rd % 256];
            q_rd = q_rd + 1;
            for (wk = 0; wk <= w_len; wk = wk + 1) begin
                wdata  <= pat(BASE + w_off + wk);
                wlast  <= (wk == w_len);
                wvalid <= 1'b1;
                @(posedge aclk);
                while (!wready) @(posedge aclk);
                wvalid <= 1'b0;
                wlast  <= 1'b0;
            end
        end
        w_done = 1'b1;
    end

    // ---- B ----
    initial forever begin
        @(posedge go_wr);
        b_done = 1'b0;
        for (b_i = 0; b_i < NTXN; b_i = b_i + 1) begin
            bready <= (({$random(bseed)} % 4) != 0);
            @(posedge aclk);
            while (!(bvalid && bready)) begin
                bready <= (({$random(bseed)} % 4) != 0);
                @(posedge aclk);
            end
            checks = checks + 1;
            if (bid !== IDX[ID_W-1:0]) begin
                errors = errors + 1;
                $display("  FAIL m%0d: B id %h want %h", IDX, bid, IDX[ID_W-1:0]);
            end
            bready <= 1'b0;
        end
        b_done = 1'b1;
    end

    // ---- AR + R, once every write of this master has been acknowledged ----
    integer r_off, r_len, rk, r_i;
    initial forever begin
        @(posedge go_rd);
        rd_done = 1'b0;
        for (r_i = 0; r_i < NTXN; r_i = r_i + 1) begin
            r_len = log_len[r_i];
            r_off = log_off[r_i];
            araddr  <= (BASE + r_off) << LSB;
            arlen   <= r_len[7:0];
            arvalid <= 1'b1;
            @(posedge aclk);
            while (!arready) @(posedge aclk);
            arvalid <= 1'b0;
            for (rk = 0; rk <= r_len; rk = rk + 1) begin
                rready <= (({$random(rseed)} % 4) != 0);
                @(posedge aclk);
                while (!(rvalid && rready)) begin
                    rready <= (({$random(rseed)} % 4) != 0);
                    @(posedge aclk);
                end
                checks = checks + 1;
                if (rid !== IDX[ID_W-1:0]) begin
                    errors = errors + 1;
                    $display("  FAIL m%0d: R id %h want %h", IDX, rid, IDX[ID_W-1:0]);
                end
                if (rdata !== pat(BASE + r_off + rk)) begin
                    errors = errors + 1;
                    $display("  FAIL m%0d: word %0d got %h want %h", IDX,
                             BASE + r_off + rk, rdata, pat(BASE + r_off + rk));
                end
                if (rlast !== (rk == r_len)) begin
                    errors = errors + 1;
                    $display("  FAIL m%0d: rlast at beat %0d of %0d", IDX, rk, r_len);
                end
                rready <= 1'b0;
            end
        end
        rd_done = 1'b1;
    end
endmodule

// ------------------------------------------------------------- the tb proper
module axi_n1_tb;
    localparam integer NM     = `NM;
    localparam integer ADDR_W = 40;
    localparam integer DATA_W = 256;
    localparam integer ID_W   = 4;
    localparam integer NTXN   = `NTXN;
    localparam integer WORDS  = 512;
    localparam integer IDX_W  = (NM <= 1) ? 1 : $clog2(NM);
    localparam integer SID_W  = ID_W + IDX_W;

    // Periods are variables so one run covers both clock ratios.
    real s_hp = 2.0, m_hp = 3.0;
    reg  s_aclk = 0, m_aclk = 0;
    always #(s_hp) s_aclk = ~s_aclk;
    always #(m_hp) m_aclk = ~m_aclk;

    reg s_aresetn = 0, m_aresetn = 0;
    reg go_wr = 0, go_rd = 0;

    wire [NM*ID_W-1:0]       s_awid, s_arid, s_bid, s_rid;
    wire [NM*ADDR_W-1:0]     s_awaddr, s_araddr;
    wire [NM*8-1:0]          s_awlen, s_arlen;
    wire [NM*3-1:0]          s_awsize, s_arsize;
    wire [NM*2-1:0]          s_awburst, s_arburst, s_bresp, s_rresp;
    wire [NM-1:0]            s_awvalid, s_awready, s_arvalid, s_arready;
    wire [NM*DATA_W-1:0]     s_wdata, s_rdata;
    wire [NM*(DATA_W/8)-1:0] s_wstrb;
    wire [NM-1:0]            s_wlast, s_wvalid, s_wready;
    wire [NM-1:0]            s_bvalid, s_bready, s_rvalid, s_rready, s_rlast;
    wire [NM-1:0]            wr_done_v, rd_done_v;

    wire [SID_W-1:0]    m_awid, m_arid;
    wire [ADDR_W-1:0]   m_awaddr, m_araddr;
    wire [7:0]          m_awlen, m_arlen;
    wire [2:0]          m_awsize, m_arsize;
    wire [1:0]          m_awburst, m_arburst;
    wire                m_awvalid, m_wvalid, m_wlast, m_arvalid;
    wire                m_awready, m_wready, m_arready;
    wire                m_bready, m_rready;
    wire [DATA_W-1:0]   m_wdata;
    wire [DATA_W/8-1:0] m_wstrb;

    wire [31:0] mchecks [0:NM-1];
    wire [31:0] merrors [0:NM-1];

    axi_n1 #(.N(NM), .ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(ID_W),
             .WR_MEM("distributed")) dut (
        .s_aclk(s_aclk), .s_aresetn(s_aresetn),
        .s_awid(s_awid), .s_awaddr(s_awaddr), .s_awlen(s_awlen),
        .s_awsize(s_awsize), .s_awburst(s_awburst),
        .s_awvalid(s_awvalid), .s_awready(s_awready),
        .s_wdata(s_wdata), .s_wstrb(s_wstrb), .s_wlast(s_wlast),
        .s_wvalid(s_wvalid), .s_wready(s_wready),
        .s_bid(s_bid), .s_bresp(s_bresp), .s_bvalid(s_bvalid), .s_bready(s_bready),
        .s_arid(s_arid), .s_araddr(s_araddr), .s_arlen(s_arlen),
        .s_arsize(s_arsize), .s_arburst(s_arburst),
        .s_arvalid(s_arvalid), .s_arready(s_arready),
        .s_rid(s_rid), .s_rdata(s_rdata), .s_rresp(s_rresp), .s_rlast(s_rlast),
        .s_rvalid(s_rvalid), .s_rready(s_rready),
        .m_aclk(m_aclk), .m_aresetn(m_aresetn),
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen),
        .m_awsize(m_awsize), .m_awburst(m_awburst),
        .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(sl_bid), .m_bresp(2'b00), .m_bvalid(sl_bvalid), .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen),
        .m_arsize(m_arsize), .m_arburst(m_arburst),
        .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rid(sl_rid), .m_rdata(sl_rdata), .m_rresp(2'b00), .m_rlast(sl_rlast),
        .m_rvalid(sl_rvalid), .m_rready(m_rready)
    );

    genvar g;
    generate
    for (g = 0; g < NM; g = g + 1) begin : mst
        axi_n1_tbm #(.IDX(g), .ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(ID_W),
                     .NTXN(NTXN), .WORDS(WORDS)) u (
            .aclk(s_aclk), .go_wr(go_wr), .go_rd(go_rd),
            .wr_done(wr_done_v[g]), .rd_done(rd_done_v[g]),
            .awid(s_awid[g*ID_W +: ID_W]), .awaddr(s_awaddr[g*ADDR_W +: ADDR_W]),
            .awlen(s_awlen[g*8 +: 8]), .awsize(s_awsize[g*3 +: 3]),
            .awburst(s_awburst[g*2 +: 2]),
            .awvalid(s_awvalid[g]), .awready(s_awready[g]),
            .wdata(s_wdata[g*DATA_W +: DATA_W]),
            .wstrb(s_wstrb[g*(DATA_W/8) +: DATA_W/8]),
            .wlast(s_wlast[g]), .wvalid(s_wvalid[g]), .wready(s_wready[g]),
            .bid(s_bid[g*ID_W +: ID_W]),
            .bvalid(s_bvalid[g]), .bready(s_bready[g]),
            .arid(s_arid[g*ID_W +: ID_W]), .araddr(s_araddr[g*ADDR_W +: ADDR_W]),
            .arlen(s_arlen[g*8 +: 8]), .arsize(s_arsize[g*3 +: 3]),
            .arburst(s_arburst[g*2 +: 2]),
            .arvalid(s_arvalid[g]), .arready(s_arready[g]),
            .rid(s_rid[g*ID_W +: ID_W]), .rdata(s_rdata[g*DATA_W +: DATA_W]),
            .rlast(s_rlast[g]), .rvalid(s_rvalid[g]), .rready(s_rready[g]),
            .checks(mchecks[g]), .errors(merrors[g])
        );
    end
    endgenerate

    // ------------------------------------------------ the slave, in m_aclk
    // Randomised ready on every channel: a crossing tested only against an
    // always-ready slave is a crossing that has not been tested.
    localparam integer MEMW = NM * WORDS;
    reg [DATA_W-1:0] mem [0:MEMW-1];
    integer sseed = 32'h0BEE_F001;

    reg              sl_awr, sl_wr, sl_arr;
    reg [SID_W-1:0]  wq_id, bq_id, sl_rid, rq_id;
    reg [ADDR_W-1:0] wq_addr, rq_addr;
    reg              wq_act, sl_bvalid, rq_act, sl_rvalid, sl_rlast;
    reg [SID_W-1:0]  sl_bid;
    reg [8:0]        rq_left;
    reg [DATA_W-1:0] sl_rdata;

    // A new AW is refused while a B is still unclaimed: overwriting `bq_id`
    // would drop a response, which presents as the master hanging on B.
    assign m_awready = sl_awr && !wq_act && !sl_bvalid;
    assign m_wready  = sl_wr  && wq_act;
    assign m_arready = sl_arr && !rq_act && !sl_rvalid;

    always @(posedge m_aclk) begin
        if (!m_aresetn) begin
            wq_act <= 0; sl_bvalid <= 0; rq_act <= 0; sl_rvalid <= 0;
            sl_awr <= 0; sl_wr <= 0; sl_arr <= 0; rq_left <= 0;
        end else begin
            sl_awr <= (({$random(sseed)} % 4) != 0);
            sl_wr  <= (({$random(sseed)} % 4) != 0);
            sl_arr <= (({$random(sseed)} % 4) != 0);

            if (m_awvalid && m_awready) begin
                wq_id <= m_awid; wq_addr <= m_awaddr; wq_act <= 1'b1;
            end
            if (m_wvalid && m_wready && wq_act) begin
                mem[wq_addr[ADDR_W-1:5]] <= m_wdata;
                wq_addr <= wq_addr + 34'd32;
                if (m_wlast) begin
                    wq_act    <= 1'b0;
                    sl_bid    <= wq_id;
                    sl_bvalid <= 1'b1;
                end
            end
            if (sl_bvalid && m_bready) sl_bvalid <= 1'b0;

            if (m_arvalid && m_arready) begin
                rq_id <= m_arid; rq_addr <= m_araddr;
                rq_left <= {1'b0, m_arlen} + 9'd1;
                rq_act  <= 1'b1;
            end
            if (rq_act && (!sl_rvalid || m_rready)) begin
                sl_rdata <= mem[rq_addr[ADDR_W-1:5]];
                sl_rid   <= rq_id;
                sl_rlast <= (rq_left == 9'd1);
                sl_rvalid <= 1'b1;
                rq_addr  <= rq_addr + 34'd32;
                rq_left  <= rq_left - 9'd1;
                if (rq_left == 9'd1) rq_act <= 1'b0;
            end else if (sl_rvalid && m_rready) begin
                sl_rvalid <= 1'b0;
            end
        end
    end

    // ------------------------------------------------------------- sequencing
    integer p, i;
    integer tot_checks, tot_errors;
    integer wd;

    initial begin
        wd = 0;
        forever begin
            @(posedge s_aclk);
            wd = wd + 1;
            if (wd > 2000000) begin
                $display("  FAIL WATCHDOG -- a channel is stuck");
                $display("========================================");
                $display("  FAIL -- watchdog");
                $display("========================================");
                $finish;
            end
        end
    end

    initial begin
        for (i = 0; i < MEMW; i = i + 1) mem[i] = {DATA_W{1'b0}};

        for (p = 0; p < 2; p = p + 1) begin
            if (p == 0) begin
                s_hp = 2.0; m_hp = 3.0;
                $display("--- %0d masters, master clock FASTER than slave ---", NM);
            end else begin
                s_hp = 3.0; m_hp = 2.0;
                $display("--- %0d masters, master clock SLOWER than slave ---", NM);
            end

            s_aresetn = 0; m_aresetn = 0;
            repeat (32) @(posedge s_aclk);
            s_aresetn = 1; m_aresetn = 1;
            repeat (32) @(posedge s_aclk);

            go_wr = 1;
            repeat (4) @(posedge s_aclk);
            wait (&wr_done_v);
            go_wr = 0;
            repeat (8) @(posedge s_aclk);

            go_rd = 1;
            repeat (4) @(posedge s_aclk);
            wait (&rd_done_v);
            go_rd = 0;
            repeat (8) @(posedge s_aclk);

            tot_checks = 0; tot_errors = 0;
            for (i = 0; i < NM; i = i + 1) begin
                tot_checks = tot_checks + mchecks[i];
                tot_errors = tot_errors + merrors[i];
            end
            $display("    %0d checks, %0d errors", tot_checks, tot_errors);
        end

        $display("========================================");
        if (tot_errors == 0)
            $display("  PASS -- %0d checks, 0 errors", tot_checks);
        else
            $display("  FAIL -- %0d checks, %0d errors", tot_checks, tot_errors);
        $display("========================================");
        $finish;
    end
endmodule

`default_nettype wire

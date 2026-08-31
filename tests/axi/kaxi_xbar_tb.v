// KohakuAXI M x N crossbar: routing + W-ordering + data-integrity test.
//
// Each master spreads its NTXN transactions across all homes (home = (IDX+txn) %
// N_HOME). The write pattern encodes {home, master, word}, so a burst routed to
// the wrong home, or a read misrouted, is a DATA error the bench sees -- not just
// a hang. AW and W are separate threads per master, so several AWs (to different
// homes) are outstanding and the per-master W-home FIFO is exercised. Reads are
// single-outstanding per master, so response ordering is not stressed here (the
// same-ID ordering stall + its dedicated test are the next milestone).
//
// Milestone 1: single clock. Watchdogged; the failure mode is a hang.

`default_nettype none
`timescale 1ns/1ps

`ifndef M
`define M 4
`endif
`ifndef NHOME
`define NHOME 4
`endif
`ifndef NTXN
`define NTXN 24
`endif

// ---------------------------------------------------------------- one master
module kaxi_tbm #(
    parameter integer IDX      = 0,
    parameter integer M        = 4,
    parameter integer N_HOME   = 4,
    parameter integer ADDR_W   = 40,
    parameter integer DATA_W   = 256,
    parameter integer ID_W     = 4,
    parameter integer HOME_LSB = 32,
    parameter integer NTXN     = 24,
    parameter integer WORDS    = 512      // words per (master,home) region
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

    integer aseed, bseed, rseed;
    integer aw_i, w_i, b_i;
    reg aw_done, w_done, b_done;

    integer q_home [0:255];
    integer q_off  [0:255];
    integer q_len  [0:255];
    integer q_wr, q_rd;

    integer log_home [0:255];
    integer log_off  [0:255];
    integer log_len  [0:255];

    // Address of the word within its home region.
    function [ADDR_W-1:0] wa;
        input integer home;
        input integer word;   // word index within the home
        begin wa = (home << HOME_LSB) | (word << LSB); end
    endfunction

    // Pattern encodes {home, master, word}: a misroute is a data mismatch.
    function [DATA_W-1:0] pat;
        input integer home;
        input integer word;
        reg [31:0] u;
        begin
            u = {home[3:0], IDX[3:0], word[23:0]};
            pat = {8{u}};
        end
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
        bseed = 32'h3000_0001 + IDX * 17;
        rseed = 32'h4000_0001 + IDX * 23;
    end

    always @(*) begin
        wr_done = aw_done && w_done && b_done;
    end

    // ---- AW ----
    integer a_home, a_word, a_len, a_gap, gk;
    initial forever begin
        @(posedge go_wr);
        aw_done = 1'b0;
        q_wr = 0; q_rd = 0;
        for (aw_i = 0; aw_i < NTXN; aw_i = aw_i + 1) begin
            a_home = (IDX + aw_i) % N_HOME;
            a_len  = {$random(aseed)} % 16;
            a_word = BASE + aw_i * 32 + ({$random(aseed)} % 16);
            log_home[aw_i] = a_home;
            log_off [aw_i] = a_word;
            log_len [aw_i] = a_len;
            q_home[q_wr % 256] = a_home;
            q_off [q_wr % 256] = a_word;
            q_len [q_wr % 256] = a_len;
            q_wr = q_wr + 1;
            a_gap = {$random(aseed)} % 4;
            for (gk = 0; gk < a_gap; gk = gk + 1) begin
                @(posedge aclk);
            end
            awaddr  <= wa(a_home, a_word);
            awlen   <= a_len[7:0];
            awvalid <= 1'b1;
            @(posedge aclk);
            while (!awready) begin
                @(posedge aclk);
            end
            awvalid <= 1'b0;
        end
        aw_done = 1'b1;
    end

    // ---- W (follows AW order via the queue) ----
    integer w_home, w_word, w_len, wk;
    initial forever begin
        @(posedge go_wr);
        w_done = 1'b0;
        for (w_i = 0; w_i < NTXN; w_i = w_i + 1) begin
            while (q_rd == q_wr) begin
                @(posedge aclk);
            end
            w_home = q_home[q_rd % 256];
            w_word = q_off [q_rd % 256];
            w_len  = q_len [q_rd % 256];
            q_rd = q_rd + 1;
            for (wk = 0; wk <= w_len; wk = wk + 1) begin
                wdata  <= pat(w_home, w_word + wk);
                wlast  <= (wk == w_len);
                wvalid <= 1'b1;
                @(posedge aclk);
                while (!wready) begin
                    @(posedge aclk);
                end
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
`ifdef TRACE
            if (IDX == 0) begin
                $display("%0t TBCNT  m0 B#%0d", $time, b_i);
            end
`endif
            if (bid !== IDX[ID_W-1:0]) begin
                errors = errors + 1;
                $display("  FAIL m%0d: B id %h want %h", IDX, bid, IDX[ID_W-1:0]);
            end
            bready <= 1'b0;
        end
        b_done = 1'b1;
    end

    // ---- AR + R (single-outstanding), after every write acknowledged ----
    integer r_home, r_word, r_len, rk, r_i;
    initial forever begin
        @(posedge go_rd);
        rd_done = 1'b0;
        for (r_i = 0; r_i < NTXN; r_i = r_i + 1) begin
            r_home = log_home[r_i];
            r_word = log_off [r_i];
            r_len  = log_len [r_i];
            araddr  <= wa(r_home, r_word);
            arlen   <= r_len[7:0];
            arvalid <= 1'b1;
            @(posedge aclk);
            while (!arready) begin
                @(posedge aclk);
            end
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
                if (rdata !== pat(r_home, r_word + rk)) begin
                    errors = errors + 1;
                    $display("  FAIL m%0d: home %0d word %0d got %h want %h",
                             IDX, r_home, r_word + rk, rdata, pat(r_home, r_word + rk));
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

// ------------------------------------------------------- one home slave (RAM)
module kaxi_tbslv #(
    parameter integer ADDR_W = 40,
    parameter integer DATA_W = 256,
    parameter integer SID_W  = 6,
    parameter integer WORDS  = 4096,
    parameter integer SEED   = 32'h0BEE_F001
)(
    input  wire                  aclk,
    input  wire                  aresetn,
    input  wire [SID_W-1:0]      awid,
    input  wire [ADDR_W-1:0]     awaddr,
    input  wire [7:0]            awlen,
    input  wire                  awvalid,
    output wire                  awready,
    input  wire [DATA_W-1:0]     wdata,
    input  wire                  wlast,
    input  wire                  wvalid,
    output wire                  wready,
    output reg  [SID_W-1:0]      bid,
    output reg                   bvalid,
    input  wire                  bready,
    input  wire [SID_W-1:0]      arid,
    input  wire [ADDR_W-1:0]     araddr,
    input  wire [7:0]            arlen,
    input  wire                  arvalid,
    output wire                  arready,
    output reg  [SID_W-1:0]      rid,
    output reg  [DATA_W-1:0]     rdata,
    output reg                   rlast,
    output reg                   rvalid,
    input  wire                  rready
);
    reg [DATA_W-1:0] mem [0:WORDS-1];
    integer sseed;
    reg              sl_awr, sl_wr, sl_arr;
    reg [SID_W-1:0]  wq_id, rq_id;
    reg [ADDR_W-1:0] wq_addr, rq_addr;
    reg              wq_act, rq_act;
    reg [8:0]        rq_left;
    integer i;

    initial begin
        sseed = SEED;
        for (i = 0; i < WORDS; i = i + 1) begin
            mem[i] = {DATA_W{1'b0}};
        end
    end

    assign awready = sl_awr && !wq_act && !bvalid;
    assign wready  = sl_wr  && wq_act;
    assign arready = sl_arr && !rq_act && !rvalid;

    always @(posedge aclk) begin
        if (!aresetn) begin
            wq_act <= 0; bvalid <= 0; rq_act <= 0; rvalid <= 0;
            sl_awr <= 0; sl_wr <= 0; sl_arr <= 0; rq_left <= 0; rlast <= 0;
        end else begin
            sl_awr <= (({$random(sseed)} % 4) != 0);
            sl_wr  <= (({$random(sseed)} % 4) != 0);
            sl_arr <= (({$random(sseed)} % 4) != 0);

            if (awvalid && awready) begin
                wq_id <= awid; wq_addr <= awaddr; wq_act <= 1'b1;
            end
            if (wvalid && wready && wq_act) begin
                mem[wq_addr[ADDR_W-1:5] % WORDS] <= wdata;
                wq_addr <= wq_addr + 34'd32;
                if (wlast) begin
                    wq_act <= 1'b0; bid <= wq_id; bvalid <= 1'b1;
                end
            end
            if (bvalid && bready) begin
                bvalid <= 1'b0;
            end

            if (arvalid && arready) begin
                rq_id <= arid; rq_addr <= araddr;
                rq_left <= {1'b0, arlen} + 9'd1; rq_act <= 1'b1;
            end
            if (rq_act && (!rvalid || rready)) begin
                rdata <= mem[rq_addr[ADDR_W-1:5] % WORDS];
                rid   <= rq_id;
                rlast <= (rq_left == 9'd1);
                rvalid <= 1'b1;
                rq_addr <= rq_addr + 34'd32;
                rq_left <= rq_left - 9'd1;
                if (rq_left == 9'd1) begin
                    rq_act <= 1'b0;
                end
            end else if (rvalid && rready) begin
                rvalid <= 1'b0;
            end
        end
    end
endmodule

// ------------------------------------------------------------- the tb proper
module kaxi_xbar_tb;
    localparam integer M        = `M;
    localparam integer N_HOME   = `NHOME;
    localparam integer ADDR_W   = 40;
    localparam integer DATA_W   = 256;
    localparam integer ID_W     = 4;
    localparam integer HOME_LSB = 32;
    localparam integer NTXN     = `NTXN;
    localparam integer WORDS    = 512;
    localparam integer MIDX_W   = (M <= 1) ? 1 : $clog2(M);
    localparam integer SID_W    = ID_W + MIDX_W;
    localparam integer SLV_WORDS = M * WORDS;

    reg clk = 0;
    always begin
        #2 clk = ~clk;
    end
    reg resetn = 0;
    reg go_wr = 0, go_rd = 0;

    wire [M*ID_W-1:0]       s_awid, s_arid, s_bid, s_rid;
    wire [M*ADDR_W-1:0]     s_awaddr, s_araddr;
    wire [M*8-1:0]          s_awlen, s_arlen;
    wire [M*3-1:0]          s_awsize, s_arsize;
    wire [M*2-1:0]          s_awburst, s_arburst, s_bresp, s_rresp;
    wire [M-1:0]            s_awvalid, s_awready, s_arvalid, s_arready;
    wire [M*DATA_W-1:0]     s_wdata, s_rdata;
    wire [M*(DATA_W/8)-1:0] s_wstrb;
    wire [M-1:0]            s_wlast, s_wvalid, s_wready;
    wire [M-1:0]            s_bvalid, s_bready, s_rvalid, s_rready, s_rlast;
    wire [M-1:0]            wr_done_v, rd_done_v;

    wire [N_HOME*SID_W-1:0]     m_awid, m_arid;
    wire [N_HOME*ADDR_W-1:0]    m_awaddr, m_araddr;
    wire [N_HOME*8-1:0]         m_awlen, m_arlen;
    wire [N_HOME*3-1:0]         m_awsize, m_arsize;
    wire [N_HOME*2-1:0]         m_awburst, m_arburst;
    wire [N_HOME-1:0]           m_awvalid, m_awready, m_arvalid, m_arready;
    wire [N_HOME*DATA_W-1:0]    m_wdata, m_rdata;
    wire [N_HOME*(DATA_W/8)-1:0] m_wstrb;
    wire [N_HOME-1:0]           m_wlast, m_wvalid, m_wready;
    wire [N_HOME*SID_W-1:0]     m_bid, m_rid;
    wire [N_HOME*2-1:0]         m_bresp, m_rresp;
    wire [N_HOME-1:0]           m_bvalid, m_bready, m_rvalid, m_rready, m_rlast;

    wire [31:0] mchecks [0:M-1];
    wire [31:0] merrors [0:M-1];

`ifdef KAXI5
    // configurable: -d KX_WR=0/1 -d KX_RD=0/1 (0=SASD, 1=SAMD), default full SAMD
`ifndef KX_WR
 `define KX_WR 1
`endif
`ifndef KX_RD
 `define KX_RD 1
`endif
    kaxi_xbar5 #(.M(M), .N_HOME(N_HOME), .ADDR_W(ADDR_W), .DATA_W(DATA_W),
                 .ID_W(ID_W), .HOME_LSB(HOME_LSB),
                 .WR_MODE(`KX_WR), .RD_MODE(`KX_RD)) dut (
`elsif KAXI4B
    kaxi_xbar4b #(.M(M), .N_HOME(N_HOME), .ADDR_W(ADDR_W), .DATA_W(DATA_W),
                 .ID_W(ID_W), .HOME_LSB(HOME_LSB)) dut (
`elsif KAXI4
    kaxi_xbar4 #(.M(M), .N_HOME(N_HOME), .ADDR_W(ADDR_W), .DATA_W(DATA_W),
                 .ID_W(ID_W), .HOME_LSB(HOME_LSB)) dut (
`elsif KAXI3
    kaxi_xbar3 #(.M(M), .N_HOME(N_HOME), .ADDR_W(ADDR_W), .DATA_W(DATA_W),
                 .ID_W(ID_W), .HOME_LSB(HOME_LSB)) dut (
`elsif KAXI2
    kaxi_xbar2 #(.M(M), .N_HOME(N_HOME), .ADDR_W(ADDR_W), .DATA_W(DATA_W),
                 .ID_W(ID_W), .HOME_LSB(HOME_LSB)) dut (
`else
    kaxi_xbar #(.M(M), .N_HOME(N_HOME), .ADDR_W(ADDR_W), .DATA_W(DATA_W),
                .ID_W(ID_W), .HOME_LSB(HOME_LSB), .WR_MEM("distributed")) dut (
`endif
        .clk(clk), .resetn(resetn),
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
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen),
        .m_awsize(m_awsize), .m_awburst(m_awburst),
        .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(m_bvalid), .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen),
        .m_arsize(m_arsize), .m_arburst(m_arburst),
        .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_rvalid(m_rvalid), .m_rready(m_rready)
    );

    genvar g;
    generate
    for (g = 0; g < M; g = g + 1) begin : mst
        kaxi_tbm #(.IDX(g), .M(M), .N_HOME(N_HOME), .ADDR_W(ADDR_W),
                   .DATA_W(DATA_W), .ID_W(ID_W), .HOME_LSB(HOME_LSB),
                   .NTXN(NTXN), .WORDS(WORDS)) u (
            .aclk(clk), .go_wr(go_wr), .go_rd(go_rd),
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
    for (g = 0; g < N_HOME; g = g + 1) begin : hslv
        kaxi_tbslv #(.ADDR_W(ADDR_W), .DATA_W(DATA_W), .SID_W(SID_W),
                     .WORDS(SLV_WORDS), .SEED(32'h0BEE_F001 + g * 101)) s (
            .aclk(clk), .aresetn(resetn),
            .awid(m_awid[g*SID_W +: SID_W]), .awaddr(m_awaddr[g*ADDR_W +: ADDR_W]),
            .awlen(m_awlen[g*8 +: 8]), .awvalid(m_awvalid[g]), .awready(m_awready[g]),
            .wdata(m_wdata[g*DATA_W +: DATA_W]), .wlast(m_wlast[g]),
            .wvalid(m_wvalid[g]), .wready(m_wready[g]),
            .bid(m_bid[g*SID_W +: SID_W]), .bvalid(m_bvalid[g]), .bready(m_bready[g]),
            .arid(m_arid[g*SID_W +: SID_W]), .araddr(m_araddr[g*ADDR_W +: ADDR_W]),
            .arlen(m_arlen[g*8 +: 8]), .arvalid(m_arvalid[g]), .arready(m_arready[g]),
            .rid(m_rid[g*SID_W +: SID_W]), .rdata(m_rdata[g*DATA_W +: DATA_W]),
            .rlast(m_rlast[g]), .rvalid(m_rvalid[g]), .rready(m_rready[g])
        );
        assign m_bresp[g*2 +: 2] = 2'b00;
        assign m_rresp[g*2 +: 2] = 2'b00;
    end
    endgenerate

    integer p, i;
    integer tot_checks, tot_errors;
    integer wd;

`ifdef TRACE
    initial begin
        $dumpfile("kaxi.vcd");
        $dumpvars(0, kaxi_xbar_tb);
    end
`endif

    initial begin
        wd = 0;
        forever begin
            @(posedge clk);
            wd = wd + 1;
            if (wd > 150000) begin
                $display("  FAIL WATCHDOG -- stuck. go_wr=%b go_rd=%b", go_wr, go_rd);
                $display("    wr_done_v=%b rd_done_v=%b", wr_done_v, rd_done_v);
                for (i = 0; i < M; i = i + 1) begin
                    $display("    m%0d checks=%0d errors=%0d awv=%b awr=%b wv=%b wr=%b bv=%b br=%b arv=%b arr=%b rv=%b rr=%b",
                             i, mchecks[i], merrors[i],
                             s_awvalid[i], s_awready[i], s_wvalid[i], s_wready[i],
                             s_bvalid[i], s_bready[i], s_arvalid[i], s_arready[i],
                             s_rvalid[i], s_rready[i]);
                end
                $display("    m_awvalid=%b m_awready=%b m_wvalid=%b m_wready=%b m_bvalid=%b m_arvalid=%b m_arready=%b m_rvalid=%b",
                         m_awvalid, m_awready, m_wvalid, m_wready, m_bvalid, m_arvalid, m_arready, m_rvalid);
                $display("  FAIL -- watchdog");
                $finish;
            end
        end
    end

    initial begin
        resetn = 0;
        repeat (32) @(posedge clk);
        resetn = 1;
        repeat (32) @(posedge clk);

        go_wr = 1;
        repeat (4) @(posedge clk);
        wait (&wr_done_v);
        go_wr = 0;
        repeat (8) @(posedge clk);

        go_rd = 1;
        repeat (4) @(posedge clk);
        wait (&rd_done_v);
        go_rd = 0;
        repeat (8) @(posedge clk);

        tot_checks = 0; tot_errors = 0;
        for (i = 0; i < M; i = i + 1) begin
            tot_checks = tot_checks + mchecks[i];
            tot_errors = tot_errors + merrors[i];
        end

        $display("========================================");
        if (tot_errors == 0) begin
            $display("  PASS -- %0d masters x %0d homes, %0d checks, 0 errors",
                     M, N_HOME, tot_checks);
        end else begin
            $display("  FAIL -- %0d checks, %0d errors", tot_checks, tot_errors);
        end
        $display("========================================");
        $finish;
    end
endmodule

`default_nettype wire

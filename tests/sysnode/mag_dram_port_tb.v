// mag_dram_port against axi_ram: N requesters, 256->512 packing, one master.
// THE PARTIAL-BEAT MATRIX IS THE POINT -- head_phase x (len%R), every cell.

`timescale 1ns / 1ps
`default_nettype none

// MW is a parameter so xelab -generic_top can also run R=1, where the packing
// is an identity and the burst-length divide must be bypassed.
`ifndef TB_ARMAX
`define TB_ARMAX 0
`endif
// The shipped depth (mag.v KOHAKU_DRAM_RD_OUT); -d TB_RD_OUT=1 is the one-in-flight port.
`ifndef TB_RD_OUT
  `define TB_RD_OUT 4
`endif
// 0: the one-clock port -- m_aclk is s_aclk and the queues are synchronous.
`ifndef TB_DRAM_CDC
  `define TB_DRAM_CDC 1
`endif
// 0: the return bus unregistered (the pre-R_REG port).
`ifndef TB_RREG
  `define TB_RREG 1
`endif

module mag_dram_port_tb #(
    parameter integer MW = 512,
    parameter integer RD_OUT = `TB_RD_OUT );
    localparam integer N      = 5;
    localparam integer ADDR_W = 40;
    localparam integer SW     = 256;
    localparam integer ID_W   = 4;
    localparam integer SBYTES = SW / 8;

    reg s_aclk = 1'b0, m_aclk = 1'b0, resetn = 1'b0;
    // Deliberately different periods: the crossing is the thing under test.
    always begin
        #2.0 s_aclk = ~s_aclk;
    end
    always begin
        #1.7 m_aclk = ~m_aclk;
    end
    wire mclk = (`TB_DRAM_CDC != 0) ? m_aclk : s_aclk;

    reg  [N-1:0]        q_valid, q_write;
    wire [N-1:0]        q_ready;
    reg  [N*ADDR_W-1:0] q_addr;
    reg  [N*16-1:0]     q_len;
    reg  [N-1:0]        w_valid;
    wire [N-1:0]        w_ready;
    reg  [N*SW-1:0]     w_data;
    wire [N-1:0]        r_valid;
    reg  [N-1:0]        r_ready;
    wire [N*SW-1:0]     r_data;
    wire [N-1:0]        r_last;
    wire [N-1:0]        b_valid;

    wire [ID_W-1:0]  awid, arid, bid, rid;
    wire [ADDR_W-1:0] awaddr, araddr;
    wire [7:0]       awlen, arlen;
    wire [2:0]       awsize, arsize;
    wire [1:0]       awburst, arburst, bresp, rresp;
    wire             awvalid, awready, arvalid, arready;
    wire [MW-1:0]    wdata, rdata;
    wire [MW/8-1:0]  wstrb;
    wire             wlast, wvalid, wready, bvalid, bready, rlast, rvalid, rready;

    mag_dram_port #(.N(N), .ADDR_W(ADDR_W), .SW(SW), .MW(MW), .ID_W(ID_W),
                    .WR_MEM("distributed"), .RD_OUT(RD_OUT), .AR_MAX(`TB_ARMAX),
                    .DRAM_CDC(`TB_DRAM_CDC), .R_REG(`TB_RREG)) u_dut (
        .s_aclk(s_aclk), .s_aresetn(resetn),
        .q_valid(q_valid), .q_ready(q_ready), .q_addr(q_addr),
        .q_len(q_len), .q_write(q_write),
        .w_valid(w_valid), .w_ready(w_ready), .w_data(w_data),
        .w_strb({(N*SW/8){1'b1}}),
        .r_valid(r_valid), .r_ready(r_ready), .r_data(r_data), .r_last(r_last),
        .b_valid(b_valid),
        .m_aclk(mclk), .m_aresetn(resetn),
        .m_awid(awid), .m_awaddr(awaddr), .m_awlen(awlen), .m_awsize(awsize),
        .m_awburst(awburst), .m_awvalid(awvalid), .m_awready(awready),
        .m_wdata(wdata), .m_wstrb(wstrb), .m_wlast(wlast), .m_wvalid(wvalid),
        .m_wready(wready),
        .m_bid(bid), .m_bresp(bresp), .m_bvalid(bvalid), .m_bready(bready),
        .m_arid(arid), .m_araddr(araddr), .m_arlen(arlen), .m_arsize(arsize),
        .m_arburst(arburst), .m_arvalid(arvalid), .m_arready(arready),
        .m_rid(rid), .m_rdata(rdata), .m_rresp(rresp), .m_rlast(rlast),
        .m_rvalid(rvalid), .m_rready(rready)
    );

    axi_ram #(.DATA_W(MW), .ADDR_W(ADDR_W), .ID_W(ID_W), .WORDS(4096),
              .PORTS(1)) u_ram (
        .clk(mclk), .resetn(resetn),
        .s_awid(awid), .s_awaddr(awaddr), .s_awlen(awlen), .s_awsize(awsize),
        .s_awburst(awburst), .s_awvalid(awvalid), .s_awready(awready),
        .s_wdata(wdata), .s_wstrb(wstrb), .s_wlast(wlast), .s_wvalid(wvalid),
        .s_wready(wready),
        .s_bid(bid), .s_bresp(bresp), .s_bvalid(bvalid), .s_bready(bready),
        .s_arid(arid), .s_araddr(araddr), .s_arlen(arlen), .s_arsize(arsize),
        .s_arburst(arburst), .s_arvalid(arvalid), .s_arready(arready),
        .s_rid(rid), .s_rdata(rdata), .s_rresp(rresp), .s_rlast(rlast),
        .s_rvalid(rvalid), .s_rready(rready),
        .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({MW{1'b0}})
    );

    integer errors = 0;
    integer checks = 0;
    // HANDSHAKES ARE COUNTED AT THE EDGE and the tasks drive at negedge, so no
    // task reads a ready/valid that the same edge already updated.
    integer q_cnt [0:N-1], w_cnt [0:N-1], r_cnt [0:N-1], b_cnt [0:N-1];
    reg [SW-1:0] r_cap [0:N-1];
    reg          r_lcap [0:N-1];
    integer mi;
    initial begin
        for (mi = 0; mi < N; mi = mi + 1) begin
            q_cnt[mi] = 0; w_cnt[mi] = 0; r_cnt[mi] = 0; b_cnt[mi] = 0;
        end
    end
    always @(posedge s_aclk) begin
        for (mi = 0; mi < N; mi = mi + 1) begin
            if (q_valid[mi] && q_ready[mi]) q_cnt[mi] <= q_cnt[mi] + 1;
            if (w_valid[mi] && w_ready[mi]) w_cnt[mi] <= w_cnt[mi] + 1;
            if (b_valid[mi]) b_cnt[mi] <= b_cnt[mi] + 1;
            if (r_valid[mi] && r_ready[mi]) begin
                r_cnt[mi]  <= r_cnt[mi] + 1;
                r_cap[mi]  <= r_data[mi*SW +: SW];
                r_lcap[mi] <= r_last[mi];
            end
        end
    end

    task automatic q_put(input integer p, input integer word, input integer beats,
                         input wr);
        integer c0;
        begin
            @(negedge s_aclk);
            q_addr[p*ADDR_W +: ADDR_W] = word * SBYTES;
            q_len [p*16     +: 16]     = beats - 1;
            q_write[p] = wr;
            q_valid[p] = 1'b1;
            c0 = q_cnt[p];
            while (q_cnt[p] == c0) @(negedge s_aclk);
            q_valid[p] = 1'b0;
        end
    endtask

    // Takes `beats` words from requester `p` and checks them against golden.
    task automatic r_take(input integer p, input integer word, input integer beats,
                          input [255:0] what);
        integer i, c0;
        begin
            @(negedge s_aclk);
            r_ready[p] = 1'b1;
            for (i = 0; i < beats; i = i + 1) begin
                c0 = r_cnt[p];
                while (r_cnt[p] == c0) @(negedge s_aclk);
                if (i == beats - 1) r_ready[p] = 1'b0;
                checks = checks + 1;
                if (r_cap[p] !== golden[word + i]) fail(what);
                if (r_lcap[p] !== (i == beats - 1)) fail("rlast misplaced");
            end
        end
    endtask

    task fail(input [255:0] why);
        begin
            $display("%0t FAIL %0s", $time, why);
            errors = errors + 1;
        end
    endtask

    // Golden copy of what we wrote, indexed by SW-word.
    reg [SW-1:0] golden [0:8191];

    // ---- one write burst from requester `p` ------------------------------
    task automatic do_write(input integer p, input integer word, input integer beats);
        integer i, c0, b0;
        reg [SW-1:0] d;
        begin
            b0 = b_cnt[p];
            q_put(p, word, beats, 1'b1);
            for (i = 0; i < beats; i = i + 1) begin
                d = {$random, $random, $random, $random,
                     $random, $random, $random, $random};
                golden[word + i] = d;
                w_data[p*SW +: SW] = d;
                w_valid[p] = 1'b1;
                c0 = w_cnt[p];
                while (w_cnt[p] == c0) @(negedge s_aclk);
            end
            w_valid[p] = 1'b0;
            while (b_cnt[p] == b0) @(negedge s_aclk);
        end
    endtask

    // ---- one read burst from requester `p`, checked against golden -------
    task automatic do_read(input integer p, input integer word, input integer beats);
        begin
            q_put(p, word, beats, 1'b0);
            r_take(p, word, beats, "read data mismatch");
        end
    endtask

    // ---- the read split in two, so a requester can hold several in flight ---
    task automatic rd_issue(input integer p, input integer word, input integer beats);
        begin
            q_put(p, word, beats, 1'b0);
            $display("  %0t issued p%0d word %0d beats %0d", $time, p, word, beats);
        end
    endtask
    task automatic rd_collect(input integer p, input integer word, input integer beats);
        begin
            r_take(p, word, beats, "queued read data mismatch");
            $display("  %0t collected p%0d word %0d beats %0d", $time, p, word, beats);
        end
    endtask
    // `n` bursts from requester `p`, the issuer running ahead of the collector
    // as far as RD_OUT lets it: every head phase and length parity, in order
    task automatic rd_queue(input integer p, input integer word, input integer n);
        integer k, ki, kc;
        begin
            for (k = 0; k < n; k = k + 1) begin
                do_write(p, word + k*40 + (k % 2), 5 + (k % 3));
            end
            fork
                for (ki = 0; ki < n; ki = ki + 1) begin
                    rd_issue(p, word + ki*40 + (ki % 2), 5 + (ki % 3));
                end
                for (kc = 0; kc < n; kc = kc + 1) begin
                    rd_collect(p, word + kc*40 + (kc % 2), 5 + (kc % 3));
                end
            join
        end
    endtask

    integer head, tail, base, qq;

    initial begin
        q_valid = 0; q_write = 0; q_addr = 0; q_len = 0;
        w_valid = 0; w_data = 0; r_ready = 0;
        repeat (20) @(posedge s_aclk);
        resetn = 1'b1;
        repeat (20) @(posedge s_aclk);

        // THE MATRIX: start on an even or odd SW word, run an even or odd
        // number of beats. At R=2 that is every partial-beat combination.
        base = 64;
        for (head = 0; head < 2; head = head + 1) begin
            for (tail = 0; tail < 2; tail = tail + 1) begin
                do_write(0, base + head, 4 + tail);
                do_read (0, base + head, 4 + tail);
                base = base + 32;
            end
        end

        // Single-beat bursts on both phases -- the shortest burst is where an
        // off-by-one in the emit condition shows up.
        do_write(1, 300, 1);  do_read(1, 300, 1);
        do_write(1, 301, 1);  do_read(1, 301, 1);

        // Long burst, crosses many memory beats.
        do_write(2, 512, 33); do_read(2, 512, 33);

        // Every requester, so arbitration and the id demux both move.
        do_write(3, 700, 5);
        do_write(4, 800, 6);
        do_read (3, 700, 5);
        do_read (4, 800, 6);

        $display("  %0t matrix, singles, long and every-requester done: %0d checks", $time, checks);
        // RD_OUT reads in flight from one requester, then from two at once
        if (RD_OUT > 1) begin
            rd_queue(0, 1024, RD_OUT);
            rd_queue(0, 1300, RD_OUT + 2);
            fork
                rd_queue(1, 1600, RD_OUT);
                rd_queue(2, 2000, RD_OUT);
            join
            // a write from a third requester in the middle of a queued stream
            fork
                rd_queue(3, 2400, RD_OUT);
                do_write(4, 2800, 9);
            join
            do_read(4, 2800, 9);
        end

        repeat (50) @(posedge s_aclk);
        if (errors == 0) begin
            $display("PASS mag_dram_port_tb: %0d checks (RD_OUT=%0d)", checks, RD_OUT);
        end
        else begin
            $display("FAIL mag_dram_port_tb: %0d errors over %0d checks",
                     errors, checks);
        end
        $finish;
    end

    initial begin
        #500000;
        $display("FAIL mag_dram_port_tb: timeout");
        $finish;
    end
endmodule

`default_nettype wire

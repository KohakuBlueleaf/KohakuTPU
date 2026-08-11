// mag_dram_port against axi_ram: N requesters, 256->512 packing, one master.
// THE PARTIAL-BEAT MATRIX IS THE POINT -- head_phase x (len%R), every cell.

`timescale 1ns / 1ps
`default_nettype none

// MW is a parameter so xelab -generic_top can also run R=1, where the packing
// is an identity and the burst-length divide must be bypassed.
module mag_dram_port_tb #(
    parameter integer MW = 512
);
    localparam integer N      = 5;
    localparam integer ADDR_W = 34;
    localparam integer SW     = 256;
    localparam integer ID_W   = 4;
    localparam integer SBYTES = SW / 8;

    reg s_aclk = 1'b0, m_aclk = 1'b0, resetn = 1'b0;
    // Deliberately different periods: the crossing is the thing under test.
    always #2.0 s_aclk = ~s_aclk;
    always #1.7 m_aclk = ~m_aclk;

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
                    .WR_MEM("distributed")) u_dut (
        .s_aclk(s_aclk), .s_aresetn(resetn),
        .q_valid(q_valid), .q_ready(q_ready), .q_addr(q_addr),
        .q_len(q_len), .q_write(q_write),
        .w_valid(w_valid), .w_ready(w_ready), .w_data(w_data),
        .r_valid(r_valid), .r_ready(r_ready), .r_data(r_data), .r_last(r_last),
        .b_valid(b_valid),
        .m_aclk(m_aclk), .m_aresetn(resetn),
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
        .clk(m_aclk), .resetn(resetn),
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

    task fail(input [255:0] why);
        begin
            $display("%0t FAIL %0s", $time, why);
            errors = errors + 1;
        end
    endtask

    // Golden copy of what we wrote, indexed by SW-word.
    reg [SW-1:0] golden [0:8191];

    // ---- one write burst from requester `p` ------------------------------
    task do_write(input integer p, input integer word, input integer beats);
        integer i;
        reg [SW-1:0] d;
        begin
            @(posedge s_aclk);
            q_addr[p*ADDR_W +: ADDR_W] = word * SBYTES;
            q_len [p*16     +: 16]     = beats - 1;
            q_write[p] = 1'b1;
            q_valid[p] = 1'b1;
            @(posedge s_aclk);
            while (!q_ready[p]) @(posedge s_aclk);
            q_valid[p] = 1'b0;

            for (i = 0; i < beats; i = i + 1) begin
                d = {$random, $random, $random, $random,
                     $random, $random, $random, $random};
                golden[word + i] = d;
                w_data[p*SW +: SW] = d;
                w_valid[p] = 1'b1;
                @(posedge s_aclk);
                while (!w_ready[p]) @(posedge s_aclk);
            end
            w_valid[p] = 1'b0;
            while (!b_valid[p]) @(posedge s_aclk);
        end
    endtask

    // ---- one read burst from requester `p`, checked against golden -------
    task do_read(input integer p, input integer word, input integer beats);
        integer i;
        begin
            @(posedge s_aclk);
            q_addr[p*ADDR_W +: ADDR_W] = word * SBYTES;
            q_len [p*16     +: 16]     = beats - 1;
            q_write[p] = 1'b0;
            q_valid[p] = 1'b1;
            @(posedge s_aclk);
            while (!q_ready[p]) @(posedge s_aclk);
            q_valid[p] = 1'b0;

            for (i = 0; i < beats; i = i + 1) begin
                r_ready[p] = 1'b1;
                @(posedge s_aclk);
                while (!r_valid[p]) @(posedge s_aclk);
                checks = checks + 1;
                if (r_data[p*SW +: SW] !== golden[word + i])
                    fail("read data mismatch");
                if ((i == beats - 1) && !r_last[p]) fail("rlast not on last beat");
                if ((i != beats - 1) && r_last[p])  fail("rlast early");
            end
            r_ready[p] = 1'b0;
        end
    endtask

    integer head, tail, base;

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

        repeat (50) @(posedge s_aclk);
        if (errors == 0)
            $display("PASS mag_dram_port_tb: %0d checks", checks);
        else
            $display("FAIL mag_dram_port_tb: %0d errors over %0d checks",
                     errors, checks);
        $finish;
    end

    initial begin
        #500000;
        $display("FAIL mag_dram_port_tb: timeout");
        $finish;
    end
endmodule

`default_nettype wire

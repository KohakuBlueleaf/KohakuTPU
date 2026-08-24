// mag_mem_port write-path component bench: fairness, ordering, integrity.
//
// Reproduces the two defects the RV32 PE system bench surfaced, each as a
// DETERMINISTIC named failure, so a fix is judged here and not through a
// four-thousand-word DRAM diff:
//
//   starve   under a saturating stream the freed low slots recycle faster
//            than the engine drains, so a slot that went ready during the
//            table-filling ramp is never again the lowest ready. The check
//            is a per-slot bound: every slot that becomes ready must issue
//            within ACK_BOUND cycles.
//
//   reorder  one source's W1 lands in a high slot, its W2 in a lower slot
//            freed in between; lowest-ready issue lands W2 before W1, so two
//            writes to one address end holding the FIRST value.
//
// The driver is one flit per cycle -- the send-task handshake of the first
// revision fed the port slower than it drains, and the table never filled.
// Phase 3 is mixed-traffic integrity so a fix cannot pass by breaking the
// ordinary path. All addresses stay inside the RAM: an out-of-range peek
// returns X and an equality against X silently passes a bad check.

`default_nettype none
`timescale 1ns/1ps

module mag_mem_port_tb;
    localparam integer FW = 288;
    localparam integer PW = 4;
    localparam integer DW = 256;
    localparam integer AW = 40;

    // Fair service is at most WR_SLOTS-1 write+read rounds ahead of a ready
    // slot, ~22 cycles each (~350 total); 1,200 is generous. A starved ramp
    // slot instead waits out the whole stream, measured 4,600+ cycles at
    // 300 rounds.
    localparam integer ACK_BOUND = 1200;

    localparam [3:0] T_RD_REQ = 4'h0, T_WR_REQ = 4'h1, T_WR_DATA = 4'h4;
    localparam [3:0] T_WR_ACK = 4'h3;

    reg clk = 1'b0;
    always begin
        #2 clk = ~clk;
    end
    reg rstn = 1'b0;

    integer checks = 0, errors = 0;
    task chk(input cond, input [255:0] what);
        begin
            checks = checks + 1;
            if (cond !== 1'b1) begin
                errors = errors + 1;
                $display("  FAIL %0s", what);
            end
        end
    endtask

    // ---- DUT + RAM ------------------------------------------------------
    reg  [FW-1:0] in_data;
    reg           in_valid;
    wire          in_busy;
    wire [FW-1:0] out_data;
    wire          out_valid;
    // Response backpressure: the manual knob ORed with a 3-in-4 duty cycle
    // during phase 1 -- ack stalls widen S_IDLE, which is what lets a freed
    // slot refill and go ready BEFORE the engine picks; without it the pick
    // wins the race by one cycle and the high slots drain by timing luck.
    reg           out_busy_m;
    reg           p1bp = 1'b0;
    reg [31:0]    cyc = 32'd0;
    always @(posedge clk) begin
        cyc <= cyc + 32'd1;
    end
    wire out_busy = out_busy_m || (p1bp && (cyc[1:0] != 2'd0));

    wire [3:0]  m_awid, m_arid, m_bid, m_rid;
    wire [AW-1:0] m_awaddr, m_araddr;
    wire [7:0]  m_awlen, m_arlen;
    wire [2:0]  m_awsize, m_arsize;
    wire [1:0]  m_awburst, m_arburst, m_bresp, m_rresp;
    wire        m_awvalid, m_awready, m_wlast, m_wvalid, m_wready;
    wire        m_bvalid, m_bready, m_arvalid, m_arready, m_rlast;
    wire        m_rvalid, m_rready;
    wire [DW-1:0]   m_wdata, m_rdata;
    wire [DW/8-1:0] m_wstrb;

    mag_mem_port #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DATA_W(DW), .ADDR_W(AW),
                   .MEM_X(0), .MEM_Y(1)) dut (
        .clk(clk), .resetn(rstn),
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen),
        .m_awsize(m_awsize), .m_awburst(m_awburst),
        .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(m_bvalid),
        .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen),
        .m_arsize(m_arsize), .m_arburst(m_arburst),
        .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp),
        .m_rlast(m_rlast), .m_rvalid(m_rvalid), .m_rready(m_rready),
        .mem_in_data(in_data), .mem_in_valid(in_valid), .mem_in_busy(in_busy),
        .mem_out_data(out_data), .mem_out_valid(out_valid),
        .mem_out_busy(out_busy),
        .mem_rd_count(), .mem_wr_count()
    );

    axi_ram #(.DATA_W(DW), .ADDR_W(AW), .WORDS(4096)) u_ram (
        .clk(clk), .resetn(rstn),
        .s_awid(m_awid), .s_awaddr(m_awaddr), .s_awlen(m_awlen),
        .s_awsize(m_awsize), .s_awburst(m_awburst),
        .s_awvalid(m_awvalid), .s_awready(m_awready),
        .s_wdata(m_wdata), .s_wstrb(m_wstrb), .s_wlast(m_wlast),
        .s_wvalid(m_wvalid), .s_wready(m_wready),
        .s_bid(m_bid), .s_bresp(m_bresp), .s_bvalid(m_bvalid),
        .s_bready(m_bready),
        .s_arid(m_arid), .s_araddr(m_araddr), .s_arlen(m_arlen),
        .s_arsize(m_arsize), .s_arburst(m_arburst),
        .s_arvalid(m_arvalid), .s_arready(m_arready),
        .s_rid(m_rid), .s_rdata(m_rdata), .s_rresp(m_rresp),
        .s_rlast(m_rlast), .s_rvalid(m_rvalid), .s_rready(m_rready)
    );

    // ---- flit builders --------------------------------------------------
    function [FW-1:0] f_hdr(input [PW-1:0] sx, input [PW-1:0] sy,
                            input [3:0] ty, input [7:0] txn);
        begin
            f_hdr = {FW{1'b0}};
            f_hdr[FW-2*PW-1 -: PW] = sx;
            f_hdr[FW-3*PW-1 -: PW] = sy;
            f_hdr[FW-4*PW-1 -: 4]  = ty;
            f_hdr[FW-4*PW-5 -: 8]  = txn;
        end
    endfunction

    function [FW-1:0] f_wreq(input [PW-1:0] sx, input [PW-1:0] sy,
                             input [7:0] txn, input [AW-1:0] addr,
                             input [7:0] len);
        begin
            f_wreq = f_hdr(sx, sy, T_WR_REQ, txn);
            f_wreq[255 -: 40] = addr;
            f_wreq[215 -: 8]  = len;
        end
    endfunction

    function [FW-1:0] f_wdat(input [PW-1:0] sx, input [PW-1:0] sy,
                             input [DW-1:0] data);
        begin
            f_wdat = f_hdr(sx, sy, T_WR_DATA, 8'd0);
            f_wdat[DW-1:0] = data;
        end
    endfunction

    function [FW-1:0] f_rreq(input [PW-1:0] sx, input [PW-1:0] sy,
                             input [7:0] txn, input [AW-1:0] addr,
                             input [7:0] len);
        begin
            f_rreq = f_hdr(sx, sy, T_RD_REQ, txn);
            f_rreq[255 -: 40] = addr;
            f_rreq[215 -: 8]  = len;
            f_rreq[207 -: 8]  = 8'd0;             // plain read, no flags
        end
    endfunction

    // ---- one-flit-per-cycle driver --------------------------------------
    // The port drains roughly a burst per 14 cycles; a driver slower than
    // that never fills the slot table and proves nothing.
    reg [FW-1:0] txq [0:8191];
    integer txw = 0, txr = 0;

    always @(posedge clk) begin
        if (!rstn) begin
            in_valid <= 1'b0;
        end else begin
            if (in_valid && !in_busy) begin
                txr <= txr + 1;
                if (txr + 1 < txw) begin
                    in_data <= txq[txr + 1];
                end
                else begin
                    in_valid <= 1'b0;
                end
            end else if (!in_valid && (txr < txw)) begin
                in_data  <= txq[txr];
                in_valid <= 1'b1;
            end
        end
    end

    task nq(input [FW-1:0] f);
        begin
            txq[txw] = f;
            txw = txw + 1;
        end
    endtask

    task nq_burst(input [PW-1:0] sx, input [PW-1:0] sy, input [7:0] txn,
                  input [AW-1:0] addr, input integer beats,
                  input [31:0] seed);
        integer b;
        begin
            nq(f_wreq(sx, sy, txn, addr, beats[7:0] - 8'd1));
            for (b = 0; b < beats; b = b + 1) begin
                nq(f_wdat(sx, sy, {8{seed + b[31:0]}}));
            end
        end
    endtask

    integer spin;
    task drain_tx;
        begin
            spin = 0;
            while (txr < txw) begin
                @(posedge clk);
                spin = spin + 1;
                if (spin > 50000) begin
                    $display("  FAIL driver spun out");
                    errors = errors + 1;
                    $finish;
                end
            end
        end
    endtask

    // ---- ack collector, per source coordinate ---------------------------
    integer acks [0:15];
    wire [PW-1:0] o_dx = out_data[FW-1 -: PW];
    wire [PW-1:0] o_dy = out_data[FW-PW-1 -: PW];
    wire [3:0]    o_ty = out_data[FW-4*PW-1 -: 4];
    wire [3:0]    o_ix = {o_dy[0], o_dx[2:0]};
    always @(posedge clk) begin
        if (rstn && out_valid && !out_busy && (o_ty == T_WR_ACK)) begin
            acks[o_ix] = acks[o_ix] + 1;
        end
    end

    // ---- ready-to-issue latency, per slot -------------------------------
    // THE starvation observable: whatever the slot and whoever owns it, a
    // ready slot must reach AXI within a bound.
    integer t_rdy [0:15];
    integer lat, max_lat;
    reg [15:0] rdy_q;
    integer s;
    always @(posedge clk) begin
        if (rstn) begin
            for (s = 0; s < 16; s = s + 1) begin
                rdy_q[s] <= dut.ws_rdy[s];
                if (dut.ws_rdy[s] && !rdy_q[s]) begin
                    t_rdy[s] <= $time;
                end
            end
            if (dut.ws_issue) begin
                lat = ($time - t_rdy[dut.ws_pick]) / 4;
                if (lat > max_lat) begin
                    max_lat = lat;
                    $display("  slot %0d ready->issue %0d cycles", dut.ws_pick, lat);
                end
            end
        end
    end

    reg p2mon = 1'b0;
    always @(posedge clk) begin
        if (p2mon && dut.ws_issue) begin
            $display("  P2 %0t ISSUE slot %0d addr %h",
                     $time, dut.ws_pick, dut.ws_addr[dut.ws_pick]);
        end
    end

    integer maxslot = -1, curslot;
    always @(posedge clk) begin
        curslot = dut.ws_free;
        if (rstn && dut.take_wr_req && (curslot > maxslot)) begin
            maxslot = curslot;
            $display("  %0t slot high-water %0d (wq %0d)",
                     $time, maxslot, dut.wq_cnt);
        end
    end

    function [255:0] peek(input [AW-1:0] a);
        peek = u_ram.mem[a >> 5];
    endfunction

    function [3:0] src_ix(input [PW-1:0] sx, input [PW-1:0] sy);
        src_ix = {sy[0], sx[2:0]};
    endfunction

    integer i;
    initial begin
        $timeformat(-9, 0, " ns", 8);
        out_busy_m = 1'b0;
        in_data = {FW{1'b0}};
        max_lat = 0;
        rdy_q = 16'd0;
        for (i = 0; i < 16; i = i + 1) begin acks[i] = 0; t_rdy[i] = 0; end
        repeat (8) @(posedge clk);
        rstn = 1'b1;
        repeat (8) @(posedge clk);

        // ---- phase 1: saturating stream fills the table ------------------
        // The L1's read-modify-write shape, which is what found the bug:
        // single-beat writebacks interleaved with plain reads. Reads win
        // S_IDLE, and during each read the just-freed slot refills and goes
        // ready, so the pick that follows always elects the refilled LOW
        // slot -- the sixteen ramp slots sit ready for the whole stream.
        $display("--- phase 1: saturating stream, per-slot service bound");
        p1bp = 1'b1;
        for (i = 0; i < 100; i = i + 1) begin
            nq_burst(4'd1, 4'd0, i[7:0], 40'h0000_1000 + i * 32, 1,
                     32'h0000_A000 + i * 32'h11);
            nq(f_rreq(4'd1, 4'd0, i[7:0], 40'h0000_1000 + i * 32, 8'd0));
        end
        nq_burst(4'd2, 4'd0, 8'hB0, 40'h0001_F000, 1, 32'h0000_BEEF);
        for (i = 100; i < 300; i = i + 1) begin
            nq_burst(4'd1, 4'd0, i[7:0], 40'h0000_1000 + i * 32, 1,
                     32'h0000_A000 + i * 32'h11);
            nq(f_rreq(4'd1, 4'd0, i[7:0], 40'h0000_1000 + i * 32, 8'd0));
        end
        drain_tx;
        spin = 0;
        while ((
            (acks[src_ix(4'd1, 4'd0)] < 300)
            || (acks[src_ix(4'd2, 4'd0)] < 1)
        ) && (spin < 50000)) begin
            @(posedge clk); spin = spin + 1;
        end
        p1bp = 1'b0;
        chk(acks[src_ix(4'd1, 4'd0)] == 300, "every A burst acked");
        chk(acks[src_ix(4'd2, 4'd0)] == 1, "B acked");
        chk(max_lat < ACK_BOUND, "every ready slot issued within the bound");
        chk(peek(40'h0001_F000) == {8{32'h0000_BEEF}}, "B data exact");
        chk(peek(40'h0000_1000 + 299 * 32)
                == {8{32'h0000_A000 + 299 * 32'h11}},
            "A last write exact");

        // ---- phase 2: one source's writes apply in program order --------
        // D's burst occupies the engine while C's W1 opens slot 1 above it;
        // after D's ack frees slot 0, queued reads keep the engine off the
        // write path (reads win S_IDLE) while out_busy stalls them, so C's
        // W2 lands in freed slot 0 BELOW its own W1 with both ready.
        $display("--- phase 2: same-source same-address order");
        p2mon = 1'b1;
        nq_burst(4'd4, 4'd0, 8'hD0, 40'h0001_D000, 8, 32'h0000_D0D0);
        nq_burst(4'd3, 4'd0, 8'hC1, 40'h0001_E000, 1, 32'h0000_0AAA);
        nq(f_rreq(4'd5, 4'd0, 8'hE0, 40'h0000_1000, 8'd1));
        nq(f_rreq(4'd5, 4'd0, 8'hE1, 40'h0000_1000, 8'd7));
        spin = 0;
        while ((acks[src_ix(4'd4, 4'd0)] < 1) && (spin < 30000)) begin
            @(posedge clk); spin = spin + 1;
        end
        chk(acks[src_ix(4'd4, 4'd0)] == 1, "D acked, slot 0 free");
        out_busy_m = 1'b1;
        nq_burst(4'd3, 4'd0, 8'hC2, 40'h0001_E000, 1, 32'h0000_0BBB);
        drain_tx;
        repeat (8) @(posedge clk);
        out_busy_m = 1'b0;
        spin = 0;
        while ((acks[src_ix(4'd3, 4'd0)] < 2) && (spin < 30000)) begin
            @(posedge clk); spin = spin + 1;
        end
        chk(acks[src_ix(4'd3, 4'd0)] == 2, "both C writes acked");
        chk(peek(40'h0001_E000) == {8{32'h0000_0BBB}},
            "same-address order: W2 value is final");
        p2mon = 1'b0;

        // ---- phase 3: mixed integrity -----------------------------------
        $display("--- phase 3: mixed traffic integrity");
        for (i = 0; i < 6; i = i + 1) begin
            nq_burst(4'd6, 4'd1, i[7:0], 40'h0000_C000 + i * 256, 8,
                     32'h0000_C000 + i * 17);
        end
        drain_tx;
        spin = 0;
        while ((acks[src_ix(4'd6, 4'd1)] < 6) && (spin < 30000)) begin
            @(posedge clk); spin = spin + 1;
        end
        chk(acks[src_ix(4'd6, 4'd1)] == 6, "phase-3 writes acked");
        for (i = 0; i < 6; i = i + 1) begin
            chk(peek(40'h0000_C000 + i * 256 + 7 * 32)
                    == {8{32'h0000_C000 + i * 17 + 32'd7}},
                "phase-3 last beat exact");
        end

        if (errors == 0) begin
            $display("PASS  %0d checks", checks);
        end
        else begin
            $display("FAIL  %0d errors in %0d checks", errors, checks);
        end
        $finish;
    end

    initial begin
        #2_000_000;
        $display("  FAIL watchdog: bench did not finish");
        $finish;
    end

endmodule

`default_nettype wire

// rv_front_tb -- the PE memory frontend against a scripted memory agent.
//
// Level 2: does ordinary RV32 memory behaviour map correctly onto the Kohaku
// protocol? The core is not here. The bench drives the internal L1's CPU port
// with the exact timing rv_mem uses -- address one cycle, data the next -- and
// plays the memory agent on the other side, so a hit, a miss, an eviction and a
// refill can be asked for directly instead of coaxed out of a program.
//
// The stub is HOSTILE where the framework permits it to be:
//   - `send_ready` drops at random, because the port contract says a sender
//     holds a flit until it is taken and nothing else here checks that;
//   - a MEM_RD_RESP with the wrong tag arrives while a fill is outstanding,
//     because a requestor that accepts any response is indistinguishable from
//     a correct one until two transactions exist;
//   - write acknowledgements are delayed by tens of cycles, because flush-all's
//     whole contract is that it waits for them rather than for the last beat.
//
// The external windows are covered here for byte enables and port
// independence. Their NoC-write path is proved at level 3, where the program
// image arrives as a CU_DATA burst and is then EXECUTED.

`default_nettype none
`timescale 1ns/1ps

`ifndef RV_WR_MAX
 `define RV_WR_MAX 1
`endif

module rv_front_tb;
    localparam integer FW    = 288;
    localparam integer PW    = 4;
    localparam integer PX    = 2, PY = 2;
    localparam integer MX    = 0, MY = 1;
    localparam integer LINES = 16;                 // small, so evictions are easy
    localparam integer SW    = 512;
    localparam [39:0] DBASE  = 40'h00_8000_0000;

    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1;
    localparam [3:0] T_MEM_WR_DATA = 4'h4, T_CU_DATA = 4'h8;

    reg clk = 1'b0, resetn = 1'b0;
    always begin
        #2 clk = ~clk;
    end

    integer errors = 0, checks = 0;
    task chk(input [255:0] got, input [255:0] want, input [255:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                $display("  FAIL %0s: got %h want %h", what, got, want);
                errors = errors + 1;
            end
        end
    endtask

    // The 32-byte line a software address names in the agent's flat memory.
    // The physical address is DBASE OR the software address, and DBASE's low
    // bits are zero, so the line index is the same either way.
    function [9:0] dl;
        input [31:0] a;
        dl = a[14:5];
    endfunction

    // ---- CPU side of the internal L1 ---------------------------------------
    reg  [31:0] probe = 32'd0, addr = 32'd0, wdata = 32'd0;
    reg         req = 1'b0, we = 1'b0;
    reg  [3:0]  be = 4'd0;
    reg         flush = 1'b0, inval = 1'b0;
    wire [31:0] rdata;
    wire        stall, flush_busy;

    wire         fill_valid, fill_ready, resp_valid;
    wire [30:0]  fill_addr;
    wire [255:0] resp_data;
    wire         wb_valid, wb_ready;
    wire [30:0]  wb_addr;
    wire [255:0] wb_data;
    wire [15:0]  wr_out;
    wire         req_idle;

    rv_l1 #(.LINES(LINES), .MEM_PRIM("block")) u_l1 (
        .clk(clk), .resetn(resetn),
        .probe_addr(probe), .req(req), .we(we), .be(be), .addr(addr),
        .wdata(wdata), .rdata(rdata), .stall(stall),
        .flush(flush), .inval(inval), .flush_busy(flush_busy),
        .fill_valid(fill_valid), .fill_ready(fill_ready), .fill_addr(fill_addr),
        .resp_valid(resp_valid), .resp_data(resp_data),
        .wb_valid(wb_valid), .wb_ready(wb_ready), .wb_addr(wb_addr),
        .wb_data(wb_data),
        .wr_idle(wr_out == 16'd0)
    );

    reg                 push_valid = 1'b0, push_win = 1'b0;
    wire                push_ready;
    reg  [PW-1:0]       push_dx = 4'd1, push_dy = 4'd1;
    reg  [13:0]         push_gran = 14'd0;
    reg  [2:0]          push_sel = 3'd0;
    reg  [3:0]          push_be = 4'hF;
    reg  [31:0]         push_data = 32'd0;

    wire [FW-1:0]       send_flit;
    wire                send_valid;
    reg                 send_ready = 1'b1;
    reg                 rx_rd_resp = 1'b0, rx_wr_ack = 1'b0;
    reg  [7:0]          rx_txn = 8'd0;
    reg  [255:0]        rx_data = 256'd0;

    // Swept by -d RV_WR_MAX=n. Only 1 is SAFE against the real agent
    // (docs/arch/pe/performance.md); this stub has no slot table, so it can price it.
    rv_noc_req #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X(PX), .POS_Y(PY),
                 .MEM_X(MX), .MEM_Y(MY), .DRAM_BASE(DBASE),
                 .WR_MAX(`RV_WR_MAX)) u_req (
        .clk(clk), .resetn(resetn),
        .fill_valid(fill_valid), .fill_ready(fill_ready), .fill_addr(fill_addr),
        .resp_valid(resp_valid), .resp_data(resp_data),
        .wb_valid(wb_valid), .wb_ready(wb_ready), .wb_addr(wb_addr),
        .wb_data(wb_data),
        .push_valid(push_valid), .push_ready(push_ready),
        .push_dx(push_dx), .push_dy(push_dy), .push_win(push_win),
        .push_gran(push_gran), .push_sel(push_sel), .push_be(push_be),
        .push_data(push_data),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .rx_rd_resp(rx_rd_resp), .rx_txn(rx_txn), .rx_data(rx_data),
        .rx_wr_ack(rx_wr_ack),
        .wr_out(wr_out), .idle(req_idle)
    );

    // ---- the scripted memory agent -----------------------------------------
    reg [255:0] dram [0:1023];
    integer dz;
    initial for (dz = 0; dz < 1024; dz = dz + 1)
        dram[dz] = {8{32'hA000_0000 + dz[31:0]}};

    wire [3:0]  s_ty    = send_flit[FW-4*PW-1 -: 4];
    wire [7:0]  s_id    = send_flit[FW-4*PW-5 -: 8];
    wire        s_ls    = send_flit[FW-4*PW-13];
    wire [39:0] s_addr  = send_flit[255 -: 40];
    wire [7:0]  s_flags = send_flit[207 -: 8];
    wire [7:0]  s_count = send_flit[199 -: 8];
    wire [7:0]  s_ew    = send_flit[165 -: 8];
    wire [7:0]  s_buf   = send_flit[255 -: 8];
    wire [15:0] s_off   = send_flit[247 -: 16];
    wire        take    = send_valid && send_ready;

    integer n_rdreq = 0, n_wrreq = 0, n_wrdata = 0, n_cudesc = 0, n_cudata = 0;
    reg [39:0]  last_rd_addr, last_wr_addr;
    reg [39:0]  wr_addr_log [0:63];
    reg [255:0] wr_data_log [0:63];
    reg [255:0] push_log    [0:63];
    reg [15:0]  push_off_log[0:63];
    reg [7:0]   push_buf_log[0:63];
    integer     n_push = 0;

    reg [7:0]  s_ew_seen = 8'd0, s_flags_seen = 8'd0, s_count_seen = 8'd0;

    reg         pend_rd = 1'b0;
    integer     rd_delay = 0;
    reg [7:0]   pend_txn;
    reg [255:0] pend_data;

    reg [39:0]  open_wr_addr;
    integer     acks_owed = 0, ack_timer = 0, ack_hold = 0;
    reg         inject_bad_txn = 1'b0, bad_sent = 1'b0;

    integer     rand_bp = 0;
    integer     seed = 32'h1234_5678;

    wire wrdata_now = take && (s_ty == T_MEM_WR_DATA);
    wire ack_fire   = (ack_timer == 0) && (acks_owed > 0);

    always @(posedge clk) begin
        if (!resetn) begin
            pend_rd <= 1'b0; acks_owed <= 0; ack_timer <= 0;
            rx_rd_resp <= 1'b0; rx_wr_ack <= 1'b0; bad_sent <= 1'b0;
            send_ready <= 1'b1;
        end else begin
            rx_rd_resp <= 1'b0;
            rx_wr_ack  <= 1'b0;

            send_ready <= (rand_bp == 0) ? 1'b1
                                         : (({$random(seed)} % 100) >= rand_bp);

            if (take) begin
                case (s_ty)
                    T_MEM_RD_REQ: begin
                        n_rdreq      <= n_rdreq + 1;
                        last_rd_addr <= s_addr;
                        s_ew_seen    <= s_ew;
                        s_flags_seen <= s_flags;
                        s_count_seen <= s_count;
                        pend_txn     <= s_id;
                        pend_data    <= dram[s_addr[14:5]];
                        pend_rd      <= 1'b1;
                        rd_delay     <= 7;
                    end
                    T_MEM_WR_REQ: begin
                        n_wrreq      <= n_wrreq + 1;
                        open_wr_addr <= s_addr;
                        last_wr_addr <= s_addr;
                    end
                    T_MEM_WR_DATA: begin
                        n_wrdata <= n_wrdata + 1;
                        dram[open_wr_addr[14:5]] <= send_flit[255:0];
                        wr_addr_log[n_wrreq - 1] <= open_wr_addr;
                        wr_data_log[n_wrreq - 1] <= send_flit[255:0];
                    end
                    T_CU_DATA: if (!s_ls) begin
                        n_cudesc <= n_cudesc + 1;
                        push_off_log[n_push] <= s_off;
                        push_buf_log[n_push] <= s_buf;
                    end else begin
                        n_cudata <= n_cudata + 1;
                        push_log[n_push] <= send_flit[255:0];
                        n_push <= n_push + 1;
                    end
                    default: ;
                endcase
            end

            // Acknowledgements are COUNTED, not held one at a time: the
            // requestor does not wait for one before issuing the next write, so
            // a single pending slot here would lose them and wedge flush-all.
            acks_owed <= acks_owed + (wrdata_now ? 1 : 0) - (ack_fire ? 1 : 0);
            if (ack_fire) begin
                rx_wr_ack <= 1'b1;
                ack_timer <= 3 + ack_hold;
            end else if (ack_timer > 0) begin
                ack_timer <= ack_timer - 1;
            end

            if (pend_rd) begin
                if (rd_delay > 0) begin
                    rd_delay <= rd_delay - 1;
                    if (inject_bad_txn && !bad_sent && (rd_delay == 3)) begin
                        bad_sent   <= 1'b1;
                        rx_rd_resp <= 1'b1;
                        rx_txn     <= pend_txn ^ 8'h0F;
                        rx_data    <= 256'hDEAD;
                    end
                end else begin
                    pend_rd    <= 1'b0;
                    rx_rd_resp <= 1'b1;
                    rx_txn     <= pend_txn;
                    rx_data    <= pend_data;
                end
            end
        end
    end

    // A write burst and a peer push are each a descriptor and exactly one data
    // flit with `last` set, and nothing may come between them.
    reg     open_burst = 1'b0;
    reg [3:0] open_ty;
    integer order_err = 0;
    always @(posedge clk) if (resetn && take) begin
        if (!open_burst) begin
            if (s_ty == T_MEM_WR_REQ) begin
                open_burst <= 1'b1; open_ty <= T_MEM_WR_DATA;
            end else if ((s_ty == T_CU_DATA) && !s_ls) begin
                open_burst <= 1'b1; open_ty <= T_CU_DATA;
            end
        end else begin
            if ((s_ty != open_ty) || !s_ls) begin
                $display("  FAIL a %h flit interrupted an open burst expecting %h",
                         s_ty, open_ty);
                order_err = order_err + 1;
            end
            open_burst <= 1'b0;
        end
    end

    // ---- the external window: two ports, byte enables ------------------------
    reg         sp_a_en = 1'b0;
    reg [3:0]   sp_a_we = 4'd0;
    reg [8:0]   sp_a_a  = 9'd0;
    reg [31:0]  sp_a_wd = 32'd0;
    wire [31:0] sp_a_rd;
    reg [8:0]   sp_b_a  = 9'd0;
    reg [3:0]   sp_b_we = 4'd0;
    reg [31:0]  sp_b_wd = 32'd0;
    wire [31:0] sp_b_rd;

    rv_spad #(.WORDS(SW), .MEM_PRIM("block")) u_spad (
        .clk(clk),
        .a_en(sp_a_en), .a_we(sp_a_we), .a_addr(sp_a_a), .a_wdata(sp_a_wd),
        .a_rdata(sp_a_rd),
        .b_addr(sp_b_a), .b_we(sp_b_we), .b_wdata(sp_b_wd), .b_rdata(sp_b_rd)
    );

    // ---- the CPU driver, at rv_mem's timing --------------------------------
    reg [31:0] rd_last;
    integer    acc_cycles;

    // EVERY DRIVE IS NON-BLOCKING, and that is not style. The DUT samples this
    // port at the same edge the bench would change it with a blocking
    // assignment, and which one wins is undefined -- the first version of this
    // task lost a store's write enable that way, and the symptom was one clean
    // line at the end of a flush rather than an obviously wrong value.
    task do_acc(input [31:0] a, input w, input [3:0] bb, input [31:0] d);
        begin
            @(posedge clk); probe <= a;
            @(posedge clk); req <= 1; addr <= a; we <= w; be <= bb; wdata <= d;
            acc_cycles = 1;
            @(negedge clk);
            while (stall && (acc_cycles <= 4000)) begin
                @(negedge clk);
                acc_cycles = acc_cycles + 1;
            end
            if (acc_cycles > 4000) begin
                $display("  FAIL access to %h never completed", a);
                errors = errors + 1;
            end
            @(posedge clk); req <= 0; we <= 0; be <= 4'd0;
            @(negedge clk);
            rd_last = rdata;
            @(posedge clk);
        end
    endtask

    task ld(input [31:0] a);  begin do_acc(a, 1'b0, 4'd0, 32'd0); end endtask
    task st(input [31:0] a, input [3:0] bb, input [31:0] d);
        begin do_acc(a, 1'b1, bb, d); end
    endtask

    integer pg;
    task do_push(input [3:0] dx, input [3:0] dy, input w,
                 input [13:0] g, input [2:0] s, input [3:0] bb,
                 input [31:0] d);
        begin
            @(posedge clk);
            push_dx <= dx; push_dy <= dy; push_win <= w; push_gran <= g;
            push_sel <= s; push_be <= bb; push_data <= d; push_valid <= 1;
            pg = 0;
            @(negedge clk);
            while (!push_ready && (pg <= 500)) begin
                @(negedge clk); pg = pg + 1;
            end
            if (pg > 500) begin
                $display("  FAIL push never accepted");
                errors = errors + 1;
            end
            @(posedge clk); push_valid <= 0;
        end
    endtask

    task settle(input integer n);
        begin repeat (n) @(posedge clk); end
    endtask

    // WAIT FOR flush_busy TO RISE FIRST. It comes up a cycle after the pulse,
    // so a bench that only waits for it to fall sees zero and lets an access
    // into MEM while the sweep is running -- which rv_l1 reports, because the
    // sweep and the access share one tag read port. rv_mem's own flush state
    // machine waits for the same two edges.
    integer fk;
    integer flush_cycles;
    time    rmw_t0;
    task run_flush;
        begin
            @(posedge clk); flush <= 1;
            @(posedge clk); flush <= 0;
            fk = 0;
            while (!flush_busy && (fk < 200)) begin @(posedge clk); fk = fk + 1; end
            fk = 0;
            while (flush_busy && (fk < 8000)) begin @(posedge clk); fk = fk + 1; end
            flush_cycles = fk;
            if (fk >= 8000) begin
                $display("  FAIL flush-all never finished");
                errors = errors + 1;
            end
        end
    endtask

    integer i, k, n0, n1;
    reg [255:0] want_line;
    reg [31:0]  base_a, base_b;

    initial begin
        settle(8);
        resetn = 1'b1;
        settle(4);

        $display("--- 1. cold miss: one entry read, right descriptor ---");
        base_a = 32'h8000_0100;
        n0 = n_rdreq;
        ld(base_a);
        chk(n_rdreq - n0, 1, "read requests for one miss");
        chk(last_rd_addr, DBASE | {9'd0, base_a[30:0] & ~32'd31}, "fill address");
        chk(s_ew_seen, 8'd1, "entry_words on the fill");
        chk(s_flags_seen, 8'h40, "STREAM set and nothing else");
        chk(s_count_seen, 8'd1, "count = one entry");
        chk(rd_last, dram[dl(base_a)][31:0], "word 0 of the refilled line");

        $display("--- 2. hits: no further traffic, every word of the line ---");
        n0 = n_rdreq;
        for (i = 0; i < 8; i = i + 1) begin
            ld(base_a + i * 4);
            chk(rd_last, dram[dl(base_a)][i*32 +: 32], "word from a hit");
        end
        chk(n_rdreq - n0, 0, "read requests while hitting");

        $display("--- 3. byte, half and word stores into a cached line ---");
        st(base_a + 0,  4'b0001, 32'h0000_00AB);
        st(base_a + 5,  4'b0010, 32'h0000_CD00);
        st(base_a + 10, 4'b1100, 32'hEF01_0000);
        st(base_a + 12, 4'b1111, 32'h1122_3344);
        ld(base_a + 0);
        chk(rd_last[7:0], 8'hAB, "sb landed in byte 0");
        ld(base_a + 4);
        chk(rd_last[15:8], 8'hCD, "sb landed in byte 1 of word 1");
        ld(base_a + 8);
        chk(rd_last[31:16], 16'hEF01, "sh landed in the upper half");
        ld(base_a + 12);
        chk(rd_last, 32'h1122_3344, "sw landed whole");

        $display("--- 4. dirty eviction writes the modified line back ---");
        want_line = dram[dl(base_a)];
        want_line[7:0]    = 8'hAB;
        want_line[47:40]  = 8'hCD;
        want_line[95:80]  = 16'hEF01;
        want_line[127:96] = 32'h1122_3344;
        base_b = base_a + LINES * 32;          // same index, different tag
        n0 = n_wrreq;
        n1 = n_rdreq;
        ld(base_b);
        settle(60);
        chk(n_wrreq - n0, 1, "write requests for one eviction");
        chk(n_wrdata, n_wrreq, "one data flit per descriptor");
        chk(last_wr_addr, DBASE | {9'd0, base_a[30:0] & ~32'd31},
            "writeback address");
        chk(wr_data_log[n_wrreq - 1], want_line,
            "the evicted line, byte for byte");
        chk(n_rdreq - n1, 1, "the fill that replaced it");

        $display("--- 5. the evicted line reads back with its stores intact ---");
        ld(base_a);
        chk(rd_last[7:0], 8'hAB, "byte survived the round trip to memory");
        ld(base_a + 12);
        chk(rd_last, 32'h1122_3344, "word survived the round trip to memory");

        $display("--- 6. a wrong-tagged response is ignored ---");
        @(posedge clk); inval <= 1; @(posedge clk); inval <= 0; settle(2);
        inject_bad_txn = 1'b1;
        n0 = n_rdreq;
        ld(base_a);
        chk(n_rdreq - n0, 1, "one request despite the stray response");
        chk(rd_last[7:0], 8'hAB, "the right response was the one used");
        inject_bad_txn = 1'b0;

        $display("--- 7. the same traffic under random link backpressure ---");
        rand_bp = 55;
        @(posedge clk); inval <= 1; @(posedge clk); inval <= 0; settle(2);
        for (i = 0; i < 6; i = i + 1) begin
            st(base_a + i * 32 + 4, 4'b1111, 32'h5500_0000 + i);
            ld(base_a + i * 32 + 4);
            chk(rd_last, 32'h5500_0000 + i, "store then load under backpressure");
        end
        for (i = 0; i < 6; i = i + 1) begin
            ld(base_b + i * 32);
            chk(rd_last, dram[dl(base_b + i * 32)][31:0],
                "refill under backpressure");
        end
        rand_bp = 0;
        settle(80);
        chk(order_err, 0, "burst framing errors");

        $display("--- 8. flush-all waits for acknowledgements ---");
        @(posedge clk); inval <= 1; @(posedge clk); inval <= 0; settle(2);
        for (i = 0; i < 4; i = i + 1) begin
            st(base_a + i * 32, 4'b1111, 32'h7700_0000 + i);
        end
        n0 = n_wrreq;
        ack_hold = 40;
        run_flush;
        chk(n_wrreq - n0, 4, "one writeback per dirty line");
        chk(wr_out, 16'd0, "no writes outstanding when the flush ended");
        for (i = 0; i < 4; i = i + 1) begin
            chk(wr_data_log[n0 + i][31:0], 32'h7700_0000 + i,
                "the flushed line carries what was stored into it");
        end
        ack_hold = 0;
        settle(20);

        n0 = n_wrreq;
        run_flush;
        chk(n_wrreq - n0, 0, "a clean flush writes nothing");

        $display("--- 9. invalidate-all forces a refill ---");
        ld(base_a);
        n0 = n_rdreq;
        ld(base_a);
        chk(n_rdreq - n0, 0, "still cached before the invalidate");
        @(posedge clk); inval <= 1;
        @(posedge clk); inval <= 0;
        settle(2);
        n0 = n_rdreq;
        ld(base_a);
        chk(n_rdreq - n0, 1, "refilled after the invalidate");

        $display("--- 10. uncached peer stores: program order is arrival order ---");
        settle(20);
        n_push = 0;
        rand_bp = 40;
        for (i = 0; i < 8; i = i + 1) begin
            do_push(4'd3, 4'd1, 1'b0, 14'd7, i[2:0], 4'hF, 32'h1000_0000 + i);
        end
        do_push(4'd3, 4'd1, 1'b0, 14'd9, 3'd0, 4'hF, 32'hD00D_BE11);
        settle(120);
        rand_bp = 0;
        chk(n_push, 9, "pushes seen by the agent");
        chk(n_cudesc, n_cudata, "one descriptor per data flit");
        for (i = 0; i < 8; i = i + 1) begin
            chk(push_log[i][31:0], 32'h1000_0000 + i, "push payload, in order");
            chk(push_log[i][38:36], i[2:0], "push word select");
            chk(push_off_log[i], 16'd7, "push granule");
            chk(push_buf_log[i], 8'd4, "buf_id for a scratchpad word push");
        end
        chk(push_log[8][31:0], 32'hD00D_BE11, "the doorbell arrived last");
        chk(order_err, 0, "burst framing errors after the pushes");

        n_push = 0;
        do_push(4'd3, 4'd1, 1'b1, 14'd2, 3'd5, 4'hF, 32'h0BAD_C0DE);
        settle(30);
        chk(push_buf_log[0], 8'd5, "buf_id for an instruction-window push");
        chk(push_log[0][38:36], 3'd5, "word select on the instruction push");

        // Reproduced down from the level-3 `thrash` program, which lost ONE
        // word of ONE line out of ninety-six. More lines than indices, so every
        // access evicts, and each is a read-modify-write: the store must land
        // in a line the fill has only just brought in.
        $display("--- 12. read-modify-write over more lines than indices ---");
        @(posedge clk); inval <= 1; @(posedge clk); inval <= 0; settle(2);
        base_a = 32'h8000_1000;
        // Every access evicts and refills: the steady state WR_MAX prices. Flush
        // does not, since it waits for every ack by definition.
        rmw_t0 = $time;
        for (k = 0; k < 4; k = k + 1) begin
            for (i = 0; i < 24; i = i + 1) begin
                ld(base_a + i * 32);
                st(base_a + i * 32, 4'b1111, rd_last + 32'd1);
            end
        end
        $display("    WR_MAX %0d: 96 evict+refill pairs in %0d cycles, %0d each",
                 `RV_WR_MAX, ($time - rmw_t0) / 4, ($time - rmw_t0) / 4 / 96);
        run_flush;
        for (i = 0; i < 24; i = i + 1) begin
            chk(dram[dl(base_a + i * 32)][31:0],
                (32'hA000_0000 + dl(base_a + i * 32)) + 32'd4,
                "four increments reached memory");
        end

        $display("--- 11. the external window: two ports, byte enables ---");
        @(posedge clk);
        sp_a_en <= 1; sp_a_we <= 4'hF; sp_a_a <= 9'd5; sp_a_wd <= 32'h1122_3344;
        sp_b_a <= 9'd9; sp_b_we <= 4'hF; sp_b_wd <= 32'hAABB_CCDD;
        @(posedge clk);
        sp_a_we <= 4'b0010; sp_a_a <= 9'd5; sp_a_wd <= 32'h0000_9900;
        sp_b_we <= 4'b1000; sp_b_a <= 9'd9; sp_b_wd <= 32'h5500_0000;
        @(posedge clk);
        sp_a_we <= 4'd0; sp_b_we <= 4'd0;
        sp_a_a <= 9'd9; sp_b_a <= 9'd5;
        @(posedge clk);
        @(negedge clk);
        chk(sp_b_rd, 32'h1122_9944, "port A byte write, read through port B");
        chk(sp_a_rd, 32'h55BB_CCDD, "port B byte write, read through port A");

        // The sweep yields one line per ~11 cycles and the real agent's
        // open-to-free is ~28, so a bound of one binds only at the slow ack.
        $display("--- 13. flush throughput at WR_MAX %0d ---", `RV_WR_MAX);
        for (k = 0; k < 2; k = k + 1) begin
            ack_hold = (k == 0) ? 0 : 40;
            @(posedge clk); inval <= 1; @(posedge clk); inval <= 0; settle(2);
            base_a = 32'h8000_2000;
            for (i = 0; i < LINES; i = i + 1) begin
                st(base_a + i * 32, 4'b1111, 32'h9900_0000 + i);
            end
            n0 = n_wrreq;
            run_flush;
            chk(n_wrreq - n0, LINES, "one writeback per dirty line");
            $display("    WR_MAX %0d ack_hold %2d: %0d lines in %4d cycles, %0d per writeback",
                     `RV_WR_MAX, ack_hold, LINES, flush_cycles,
                     flush_cycles / LINES);
        end
        ack_hold = 0;

        settle(20);
        $display("========================================");
        if (checks == 0) begin
            $display("  FAIL -- the bench made no checks");
        end
        else if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors",
                     checks, errors);
        end
        $display("========================================");
        $finish;
    end

    initial begin
        #6000000;
        $display("  FAIL WATCHDOG -- the frontend bench never finished");
        $display("========================================");
        $finish;
    end

endmodule

`default_nettype wire

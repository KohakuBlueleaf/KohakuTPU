// kht_lsu_tb -- the load/store unit against a byte-array reference applied in
// issue order. Random gathers and scatters of every width over 32 lines into an
// 8-line L1 (four lines per index), fills answered out of order after random
// latency, writes applied to memory only at their acknowledgement, and random
// back-pressure on every port. Every return is compared; memory is compared
// after each flush. Directed checks witness coalescing, flush keeping lines and
// invalidate dropping them.
//
// THE MEMORY READS AT ACCEPT, WRITES AT ACK, and refuses only a read of the line
// its un-acknowledged write carries (kht_req's rule). A read the LSU lets past
// its own pending write therefore returns stale data here.

`timescale 1ns/1ps
`default_nettype none

`ifndef TB_COAL
 `define TB_COAL 1
`endif
`ifndef TB_MSHRS
 `define TB_MSHRS 4
`endif
`ifndef TB_WB
 `define TB_WB 1
`endif
`ifndef TB_DEPTH
 `define TB_DEPTH 4
`endif
`ifndef TB_N
 `define TB_N 3000
`endif
`ifndef TB_SCAN
 `define TB_SCAN 2
`endif
`ifndef TB_SKIP
 `define TB_SKIP 0
`endif
`ifndef TB_HOLD
 `define TB_HOLD 1
`endif

module kht_lsu_tb;
    localparam integer LANES = 8, WAVES = 4, WID = 2, NL = 32, LINES = 8;
    localparam integer VW = 32 * LANES;

    reg clk = 1'b0, resetn = 1'b0;
    always #2 clk = ~clk;

    reg              req_v = 1'b0;
    wire             req_ready;
    reg  [WID-1:0]   req_wave;
    reg  [4:0]       req_rd;
    reg              req_st, req_sx;
    reg  [1:0]       req_w;
    reg  [LANES-1:0] req_mask;
    reg  [VW-1:0]    req_addr, req_data;
    wire             pop;
    wire             ret_v, ret_ld;
    reg              ret_ready = 1'b0;
    wire [WID-1:0]   ret_wave;
    wire [4:0]       ret_rd;
    wire [LANES-1:0] ret_mask;
    wire [VW-1:0]    ret_data;
    reg              flush = 1'b0, inval = 1'b0;
    wire             busy;
    wire             rd_v, wr_v;
    wire [25:0]      rd_line, wr_line;
    wire [7:0]       rd_tag;
    wire [255:0]     wr_data;
    reg              rsp_v = 1'b0;
    reg  [7:0]       rsp_tag;
    reg  [255:0]     rsp_data;
    wire [31:0]      acc_ctr, fill_ctr;

    reg              rd_rand = 1'b0, wr_rand = 1'b0;
    reg              w_busy = 1'b0;
    reg  [25:0]      w_line;
    reg  [255:0]     w_data;
    integer          w_t;
    wire rd_ready = rd_rand && !(w_busy && (rd_line == w_line));
    wire wr_ready = wr_rand && !w_busy;

    kht_lsu #(.LANES(LANES), .WAVES(WAVES), .DEPTH(`TB_DEPTH), .LINES(LINES),
              .MSHRS(`TB_MSHRS), .COALESCE(`TB_COAL), .WB(`TB_WB),
              .TAG_BASE(8'h40), .MEM_PRIM("block"),
              .SCAN(`TB_SCAN), .SKIP(`TB_SKIP), .HOLD(`TB_HOLD)) dut (
        .clk(clk), .resetn(resetn),
        .req_v(req_v), .req_ready(req_ready), .req_wave(req_wave), .req_rd(req_rd),
        .req_st(req_st), .req_w(req_w), .req_sx(req_sx), .req_mask(req_mask),
        .req_addr(req_addr), .req_data(req_data), .pop(pop),
        .ret_v(ret_v), .ret_ready(ret_ready), .ret_wave(ret_wave), .ret_rd(ret_rd),
        .ret_ld(ret_ld), .ret_mask(ret_mask), .ret_data(ret_data),
        .flush(flush), .inval(inval), .busy(busy),
        .rd_v(rd_v), .rd_ready(rd_ready), .rd_line(rd_line), .rd_tag(rd_tag),
        .rsp_v(rsp_v), .rsp_tag(rsp_tag), .rsp_data(rsp_data),
        .wr_v(wr_v), .wr_ready(wr_ready), .wr_line(wr_line), .wr_data(wr_data),
        .wr_idle(!w_busy),
        .acc_ctr(acc_ctr), .fill_ctr(fill_ctr)
    );

    integer errors = 0, checks = 0;
    reg [255:0] dmem [0:NL-1];
    reg [7:0]   refb [0:NL*32-1];

    // ---- memory: reads at accept, answered out of order --------------------
    localparam integer PS = 16;
    reg         p_v [0:PS-1];
    reg [7:0]   p_tag [0:PS-1];
    reg [255:0] p_dat [0:PS-1];
    integer     p_t [0:PS-1];
    integer     pi, pj, pk;
    reg         sent;
    // `freeze` holds every answer, so the scanner's reach shows as fills asked.
    reg         freeze = 1'b0;
    reg     counting = 1'b0;
    integer n_alloc_h = 0;
    always @(posedge clk) if (resetn && counting && dut.alloc_h) n_alloc_h = n_alloc_h + 1;

    // The scanned entry is a queued one: the scanner's slot is `sc_off` places
    // behind the head, and that place is inside the queue (or 0 when empty).
    always @(negedge clk) if (resetn) begin
        checks = checks + 1;
        if ((dut.sc_ptr !== ((dut.q_hd + dut.sc_off) % `TB_DEPTH))
            || ((dut.sc_off != 0) && (dut.sc_off >= dut.q_n))) begin
            if (errors < 10) begin
                $display("  FAIL scanner at slot %0d offset %0d, head %0d, %0d queued",
                         dut.sc_ptr, dut.sc_off, dut.q_hd, dut.q_n);
            end
            errors = errors + 1;
        end
    end

    // Fill buffers allocated, timed at allocation (not at the port's accept).
    integer     rd_n = 0, rd_t0 = 0, rd_t1 = 0;
    reg [31:0]  fill_seen = 0;
    reg [25:0]  fa_line [0:7];
    integer     ph;
    always @(posedge clk) if (resetn) begin
        if (fill_ctr != fill_seen) begin
            rd_n = rd_n + 1;
            if (rd_n == 1) rd_t0 = $time / 4;
            if (rd_n == 2) rd_t1 = $time / 4;
        end
        fill_seen = fill_ctr;
        if ((dut.alloc_h || dut.alloc_s) && (rd_n < 8)) begin
            fa_line[rd_n] = dut.alloc_h ? dut.cur_line : dut.c_line;
        end
    end
    always @(posedge clk) begin
        rsp_v <= 1'b0;
        sent = 1'b0;
        if (!resetn) begin
            for (pi = 0; pi < PS; pi = pi + 1) p_v[pi] <= 1'b0;
            w_busy <= 1'b0;
        end else begin
            for (pi = 0; pi < PS; pi = pi + 1) begin
                if (p_v[pi] && (p_t[pi] > 0)) p_t[pi] <= p_t[pi] - 1;
            end
            pk = {$random} % PS;
            for (pj = 0; pj < PS; pj = pj + 1) begin
                pi = (pk + pj) % PS;
                if (p_v[pi] && (p_t[pi] == 0) && !sent && !freeze) begin
                    sent = 1'b1;
                    rsp_v       <= 1'b1;
                    rsp_tag     <= p_tag[pi];
                    rsp_data    <= p_dat[pi];
                    p_v[pi]     <= 1'b0;
                    pj = PS;
                end
            end
            if (rd_v && rd_ready) begin
                if (rd_line >= NL) begin
                    $display("  FAIL read of line %0d outside memory", rd_line);
                    errors = errors + 1;
                end
                pk = -1;
                for (pi = PS - 1; pi >= 0; pi = pi - 1) if (!p_v[pi]) pk = pi;
                p_v[pk]   <= 1'b1;
                p_tag[pk] <= rd_tag;
                p_dat[pk] <= dmem[rd_line % NL];
                p_t[pk]   <= 3 + {$random} % 30;
            end
            if (wr_v && wr_ready) begin
                w_busy <= 1'b1;
                w_line <= wr_line;
                w_data <= wr_data;
                w_t    <= 2 + {$random} % 20;
            end else if (w_busy) begin
                if (w_t == 0) begin
                    dmem[w_line % NL] <= w_data;
                    w_busy <= 1'b0;
                end else begin
                    w_t <= w_t - 1;
                end
            end
        end
    end

    // ---- back-pressure ------------------------------------------------------
    // The return also stalls 30 cycles in every 400, so the queue fills.
    integer cyc = 0;
    always @(negedge clk) begin
        cyc = cyc + 1;
        rd_rand   <= ({$random} % 5) < 3;
        wr_rand   <= ({$random} % 2) == 0;
        ret_ready <= ((cyc % 400) >= 30) && (({$random} % 10) < 7);
    end

    // ---- expected returns ---------------------------------------------------
    localparam integer ED = 64;
    reg [WID-1:0]   e_wave [0:ED-1];
    reg [4:0]       e_rd   [0:ED-1];
    reg             e_ld   [0:ED-1];
    reg [LANES-1:0] e_mask [0:ED-1];
    reg [VW-1:0]    e_data [0:ED-1];
    integer ewr = 0, erd = 0, nret = 0;

    always @(posedge clk) if (resetn && ret_v && ret_ready) begin
        checks = checks + 1;
        nret = nret + 1;
        if (erd == ewr) begin
            $display("  FAIL a return with nothing issued");
            errors = errors + 1;
        end else begin
            if ((ret_wave !== e_wave[erd % ED]) || (ret_rd !== e_rd[erd % ED])
                || (ret_ld !== e_ld[erd % ED]) || (ret_mask !== e_mask[erd % ED])
                || (ret_data !== e_data[erd % ED])) begin
                if (errors < 10) begin
                    $display("  FAIL return %0d: wave %0d/%0d rd %0d/%0d ld %b/%b mask %h/%h",
                             erd, ret_wave, e_wave[erd % ED], ret_rd, e_rd[erd % ED],
                             ret_ld, e_ld[erd % ED], ret_mask, e_mask[erd % ED]);
                    $display("        data %h", ret_data);
                    $display("        want %h", e_data[erd % ED]);
                end
                errors = errors + 1;
            end
            erd = erd + 1;
        end
    end

    // ---- one instruction ----------------------------------------------------
    reg [31:0] a, v;
    reg [VW-1:0] ed;
    integer l, b, nb, mode, ln, lnl, off, base;
    task issue(input integer force_mode, input integer force_st, input [LANES-1:0] force_mask);
        begin
            req_st   = (force_st >= 0) ? force_st[0] : (({$random} % 5) < 2);
            req_w    = (force_mode >= 0) ? 2'd2 : ({$random} % 3);
            nb       = 1 << req_w;
            req_sx   = $random;
            req_wave = $random;
            req_rd   = $random;
            req_mask = (force_mode >= 0) ? force_mask
                     : (({$random} % 4) == 0) ? {LANES{1'b1}}
                     : (({$random} % 40) == 0) ? {LANES{1'b0}} : $random;
            mode = (force_mode >= 0) ? force_mode : ({$random} % 3);
            ln   = (force_mode >= 0) ? 5 : ({$random} % NL);
            base = ({$random} % 32) & ~(nb - 1);
            for (l = 0; l < LANES; l = l + 1) begin
                case (mode)
                    0: begin lnl = ln; off = (base + l * nb) % 32; end
                    1: begin lnl = {$random} % NL; off = ({$random} % 32) & ~(nb - 1); end
                    default: begin
                        lnl = (ln + (l & 1) * LINES) % NL;
                        off = ({$random} % 32) & ~(nb - 1);
                    end
                endcase
                a = 32'h8000_0000 | (lnl << 5) | off;
                req_addr[32*l +: 32] = a;
                req_data[32*l +: 32] = $random;
            end
            ed = {VW{1'b0}};
            for (l = 0; l < LANES; l = l + 1) begin
                if (req_mask[l]) begin
                    a = req_addr[32*l +: 32] & 32'h7FFF_FFFF;
                    if (req_st) begin
                        for (b = 0; b < nb; b = b + 1) refb[a + b] = req_data[32*l + 8*b +: 8];
                    end else begin
                        v = 32'd0;
                        for (b = 0; b < nb; b = b + 1) v = v | (refb[a + b] << (8 * b));
                        if (req_sx && (nb == 1)) v = {{24{v[7]}}, v[7:0]};
                        if (req_sx && (nb == 2)) v = {{16{v[15]}}, v[15:0]};
                        ed[32*l +: 32] = v;
                    end
                end
            end
            e_wave[ewr % ED] = req_wave;
            e_rd[ewr % ED]   = req_rd;
            e_ld[ewr % ED]   = !req_st;
            e_mask[ewr % ED] = req_mask;
            e_data[ewr % ED] = ed;
            ewr = ewr + 1;
            @(negedge clk);
            while (!req_ready || (ewr - erd > ED - 2)) @(negedge clk);
            req_v = 1'b1;
            @(negedge clk);
            req_v = 1'b0;
        end
    endtask

    // A word load, lanes below `split` on line `la`, the rest on line `lb`.
    task issue_at(input integer la, input integer lb, input integer split);
        begin
            req_st = 1'b0; req_w = 2'd2; req_sx = 1'b0;
            req_wave = 0; req_rd = 1; req_mask = {LANES{1'b1}};
            ed = {VW{1'b0}};
            for (l = 0; l < LANES; l = l + 1) begin
                a = 32'h8000_0000 | (((l < split) ? la : lb) << 5) | (l * 4);
                req_addr[32*l +: 32] = a;
                req_data[32*l +: 32] = 32'd0;
                a = a & 32'h7FFF_FFFF;
                v = 32'd0;
                for (b = 0; b < 4; b = b + 1) v = v | (refb[a + b] << (8 * b));
                ed[32*l +: 32] = v;
            end
            e_wave[ewr % ED] = req_wave;
            e_rd[ewr % ED]   = req_rd;
            e_ld[ewr % ED]   = 1'b1;
            e_mask[ewr % ED] = req_mask;
            e_data[ewr % ED] = ed;
            ewr = ewr + 1;
            @(negedge clk);
            while (!req_ready) @(negedge clk);
            req_v = 1'b1;
            @(negedge clk);
            req_v = 1'b0;
        end
    endtask

    task drain;
        integer t;
        begin
            t = 0;
            while ((erd != ewr) && (t < 200000)) begin @(negedge clk); t = t + 1; end
            while (busy && (t < 200000)) begin @(negedge clk); t = t + 1; end
            if (t >= 200000) begin
                $display("  FAIL the unit never went idle (%0d of %0d returned)", erd, ewr);
                errors = errors + 1;
            end
        end
    endtask

    task pulse(input integer which);
        begin
            @(negedge clk);
            if (which) inval = 1'b1; else flush = 1'b1;
            @(negedge clk);
            flush = 1'b0;
            inval = 1'b0;
        end
    endtask

    task compare_mem(input [255:0] what);
        integer m, y;
        reg [7:0] got;
        begin
            for (m = 0; m < NL * 32; m = m + 1) begin
                got = dmem[m / 32][8 * (m % 32) +: 8];
                checks = checks + 1;
                if (got !== refb[m]) begin
                    if (errors < 10) begin
                        $display("  FAIL %0s: memory byte %0d (line %0d) %h want %h",
                                 what, m, m / 32, got, refb[m]);
                    end
                    errors = errors + 1;
                end
            end
        end
    endtask

`ifdef TB_TRACE
    always @(posedge clk) if (resetn && (cyc < `TB_TRACE)) begin
        $display("  T%0d h%0d i%0d q%0d todo %b cur %0d fb %0d%0d%0d%0d rd %b/%b rsp %b wr %b/%b busy %b",
                 cyc, dut.hst, dut.ist, dut.q_n, dut.todo, dut.cur_line,
                 dut.fb_st[0], dut.fb_st[1], dut.fb_st[2], dut.fb_st[3],
                 rd_v, rd_ready, rsp_v, wr_v, wr_ready, busy);
    end
`endif

    integer n, acc0, fill0;
    initial begin
        for (n = 0; n < NL; n = n + 1) begin
            dmem[n] = {$random, $random, $random, $random, $random, $random, $random, $random};
        end
        for (n = 0; n < NL * 32; n = n + 1) refb[n] = dmem[n / 32][8 * (n % 32) +: 8];
        repeat (4) @(posedge clk);
        resetn = 1'b1;
        repeat (LINES + 4) @(posedge clk);

        // Coalescing: eight lanes, one line, one access (eight without it).
        issue(0, 1, 8'hFF);
        drain;
        acc0 = acc_ctr;
        issue(0, 0, 8'hFF);
        drain;
        checks = checks + 1;
        if ((acc_ctr - acc0) !== ((`TB_COAL != 0) ? 1 : 8)) begin
            $display("  FAIL a lane-linear load took %0d accesses", acc_ctr - acc0);
            errors = errors + 1;
        end

        // A flush keeps the line resident; an invalidate drops it.
        pulse(0);
        drain;
        compare_mem("after a flush");
        fill0 = fill_ctr;
        issue(0, 0, 8'hFF);
        drain;
        checks = checks + 1;
        if (fill_ctr !== fill0) begin
            $display("  FAIL a flushed line was refetched");
            errors = errors + 1;
        end
        pulse(1);
        drain;
        compare_mem("after an invalidate");
        fill0 = fill_ctr;
        issue(0, 0, 8'hFF);
        drain;
        checks = checks + 1;
        if (fill_ctr === fill0) begin
            $display("  FAIL an invalidated line still hit");
            errors = errors + 1;
        end

        // The scanner's reach: four queued loads of four missing lines, answers
        // held; it must ask for min(SCAN, DEPTH, MSHRS) of them.
        pulse(1);
        drain;
        freeze = 1'b1;
        fill0 = fill_ctr;
        // As many loads as the queue holds, up to four.
        for (n = 0; n < ((`TB_DEPTH < 4) ? `TB_DEPTH : 4); n = n + 1) issue_at(10 + n, 10 + n, 8);
        repeat (80) @(negedge clk);
        checks = checks + 1;
        b = (`TB_SCAN < 1) ? 1 : (`TB_SCAN > `TB_DEPTH) ? `TB_DEPTH : `TB_SCAN;
        if (`TB_DEPTH < 2) b = 1;
        if (b > `TB_MSHRS) b = `TB_MSHRS;
        if (b > 4) b = 4;
        if ((fill_ctr - fill0) !== b) begin
            $display("  FAIL the scanner asked %0d fills with 4 loads queued, want %0d", fill_ctr - fill0, b);
            errors = errors + 1;
        end
        freeze = 1'b0;
        drain;

        // SKIP: lanes 0-6 on one missing line, lane 7 on another; the second
        // fill follows the first within 4 cycles with SKIP, not before 7 without.
        pulse(1);
        drain;
        freeze = 1'b1;
        rd_n = 0;
        issue_at(20, 21, 7);
        repeat (40) @(negedge clk);
        checks = checks + 1;
        if (`TB_MSHRS < 2) begin
            // one buffer: the second line waits for the first, by design
        end else if (rd_n < 2) begin
            $display("  FAIL the second line of one load was never asked for");
            errors = errors + 1;
        end else if ((`TB_SKIP != 0) ? (rd_t1 - rd_t0 > 4) : (rd_t1 - rd_t0 < 7)) begin
            $display("  FAIL second fill %0d cycles after the first (SKIP %0d)", rd_t1 - rd_t0, `TB_SKIP);
            errors = errors + 1;
        end
        freeze = 1'b0;
        drain;

        // A stream of loads to missing lines: after the first, the scanner asks
        // for every line before its entry reaches the head, so the head never
        // allocates for itself again.
        pulse(1);
        drain;
        n_alloc_h = 0;
        counting = 1'b1;
        for (n = 0; n < 12; n = n + 1) issue_at(16 + n, 16 + n, 8);
        drain;
        counting = 1'b0;
        checks = checks + 1;
        if ((`TB_DEPTH >= 2) && (`TB_MSHRS >= 2) && (n_alloc_h > 1)) begin
            $display("  FAIL the head allocated its own fill %0d times in a stream", n_alloc_h);
            errors = errors + 1;
        end

        // HOLD: MSHRS + 2 missing lines queued, answers held, released at eight
        // phases of the walk; a dropped candidate lets a later line take a buffer first.
        if ((`TB_HOLD != 0) && (`TB_DEPTH >= `TB_MSHRS + 2) && (`TB_SCAN >= `TB_MSHRS + 2)) begin
            for (ph = 0; ph < 8; ph = ph + 1) begin
                pulse(1);
                drain;
                freeze = 1'b1;
                rd_n = 0;
                for (n = 0; n < `TB_MSHRS + 2; n = n + 1) issue_at(12 + n, 12 + n, 8);
                repeat (40 + ph) @(negedge clk);
                freeze = 1'b0;
                drain;
                checks = checks + 1;
                for (n = 0; n < `TB_MSHRS + 2; n = n + 1) begin
                    if ((rd_n != `TB_MSHRS + 2) || (fa_line[n] !== 12 + n)) begin
                        if (errors < 10) begin
                            $display("  FAIL hold: fill %0d of %0d was line %0d, want %0d (release %0d)",
                                     n, rd_n, fa_line[n], 12 + n, 40 + ph);
                        end
                        errors = errors + 1;
                        n = `TB_MSHRS + 2;
                    end
                end
            end
        end

        // A flush raised while a scatter's stores still sit in fill buffers --
        // the core's halt, which flushes behind its last stores.
        for (n = 0; n < 300; n = n + 1) begin
            issue(1, 1, 8'hFF);
            if (n % 2) issue(1, 1, 8'hFF);
            pulse(0);
            drain;
            compare_mem("a flush raised under a scatter");
        end

        // Random traffic, a flush partway, invalidates pulsed into it.
        for (n = 0; n < `TB_N; n = n + 1) begin
            issue(-1, -1, 0);
            if ((n % 700) == 350) pulse(1);
        end
        drain;
        pulse(0);
        drain;
        compare_mem("after the random run");
        checks = checks + 1;
        if (nret !== ewr) begin
            $display("  FAIL %0d instructions issued, %0d returned", ewr, nret);
            errors = errors + 1;
        end

        $display("  kht_lsu: COALESCE %0d MSHRS %0d WB %0d DEPTH %0d -- %0d instructions, %0d accesses, %0d fills",
                 `TB_COAL, `TB_MSHRS, `TB_WB, `TB_DEPTH, ewr, acc_ctr, fill_ctr);
        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end

    initial begin
        #100000000;
        $display("  FAIL WATCHDOG -- the bench never finished");
        $finish;
    end

endmodule

`default_nettype wire

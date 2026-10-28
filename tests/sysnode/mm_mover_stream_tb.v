// mm_mover under streaming traffic: back-to-back gathers, scatters and pitched
// copies against an AXI memory with read-latency jitter and back-pressure.
// Pitched rows (256 B) make every word its own write burst, chained off the
// last beat of the one before while read data lands a word at a time.
// Stimulus is an xorshift stream: xsim and Verilator see the same traffic.

`default_nettype none
`timescale 1ns/1ps

module mm_mover_stream_tb;
    localparam integer DW = 256, AW = 40, IDW = 4;
    localparam integer MW = 15;                     // 2^15 words, 1 MB
    localparam integer WORDS = 1 << MW;

    localparam [AW-1:0] TBL  = 40'h00000;           // 4096 table words
    localparam [AW-1:0] IDXS = 40'h20000;           // 16 slots x 128 B of short index lists
    localparam [AW-1:0] IDXL = 40'h21000;           // one long index list
    localparam [AW-1:0] SRCC = 40'h30000;           // 2048 contiguous source words
    localparam [AW-1:0] DSTS = 40'h40000;           // 16 slots x 21 rows x 256 B
    localparam [AW-1:0] DSTL = 40'h60000;           // 1024 rows x 256 B
    localparam [AW-1:0] DSTC = 40'hA0000;           // 2048 contiguous destination words
    localparam [AW-1:0] SRCP = 40'hC0000;           // 1024 rows x 256 B, pitched source
    localparam integer  PITCH = 256;
    localparam integer  NTBL  = 4096;
    localparam integer  SROWS = 21;                 // rows of a short gather
    localparam integer  NBIG  = 1024;

    reg clk = 1'b0, resetn = 1'b0;
    always #2 clk = ~clk;
    integer cyc = 0;
    always @(posedge clk) cyc <= cyc + 1;

    // ---------------------------------------------------------------- DUT
    reg         cfg_en = 1'b0;
    reg  [7:0]  cfg_addr = 8'd0;
    reg  [63:0] cfg_data = 64'd0;
    wire        stat_busy, cfg_room;
    wire [3:0]  stat_fault;
    wire [31:0] stat_done;

    wire [IDW-1:0]  awid, arid;
    wire [AW-1:0]   awaddr, araddr;
    wire [7:0]      awlen, arlen;
    wire [2:0]      awsize, arsize;
    wire [1:0]      awburst, arburst;
    wire            awvalid, arvalid, wvalid, wlast, bready, rready;
    wire [DW-1:0]   wdata;
    wire [DW/8-1:0] wstrb;
    reg             awready = 1'b0, arready = 1'b0, wready = 1'b0;
    reg             bvalid = 1'b0, rvalid = 1'b0, rlast = 1'b0;
    reg  [DW-1:0]   rdata = {DW{1'b0}};

    mm_mover #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW)) dut (
        .clk(clk), .resetn(resetn),
        .cfg_en(cfg_en), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
        .stat_busy(stat_busy), .stat_fault(stat_fault), .stat_done(stat_done),
        .cfg_room(cfg_room),
        .m_awid(awid), .m_awaddr(awaddr), .m_awlen(awlen), .m_awsize(awsize),
        .m_awburst(awburst), .m_awvalid(awvalid), .m_awready(awready),
        .m_wdata(wdata), .m_wstrb(wstrb), .m_wlast(wlast), .m_wvalid(wvalid),
        .m_wready(wready),
        .m_bid({IDW{1'b0}}), .m_bresp(2'b00), .m_bvalid(bvalid), .m_bready(bready),
        .m_arid(arid), .m_araddr(araddr), .m_arlen(arlen), .m_arsize(arsize),
        .m_arburst(arburst), .m_arvalid(arvalid), .m_arready(arready),
        .m_rid({IDW{1'b0}}), .m_rdata(rdata), .m_rresp(2'b00), .m_rlast(rlast),
        .m_rvalid(rvalid), .m_rready(rready),
        .x_req(), .x_gnt(1'b0), .x_start(), .x_id(), .x_mode(), .x_beat(),
        .x_beat_valid(), .x_done(1'b0),
        .x_w0({DW{1'b0}}), .x_w1({DW{1'b0}}), .x_w2({DW{1'b0}}), .x_w3({DW{1'b0}}),
        .x_gid(), .x_geo_beats(4'd1), .x_geo_depth(3'd1), .x_fault(4'd0)
    );

    // ---------------------------------------------------------------- memory
    reg [DW-1:0] mem   [0:WORDS-1];
    reg [DW-1:0] want  [0:WORDS-1];                 // what the running moves must write
    reg          wantv [0:WORDS-1];

    function [MW-1:0] widx(input [AW-1:0] a);
        begin widx = a[MW+4:5]; end
    endfunction

    function [31:0] xs(input [31:0] s);
        reg [31:0] t;
        begin
            t = s ^ (s << 13);
            t = t ^ (t >> 17);
            xs = t ^ (t << 5);
        end
    endfunction

    function [DW-1:0] pat(input integer w, input [31:0] salt);
        reg [31:0] v;
        begin
            v = salt ^ w;
            pat = {v, ~v, v * 3, v ^ 32'h0F0F_0F0F, v + 32'd7, ~(v * 5), v << 4, v};
        end
    endfunction

    // The memory's behaviour: a profile per 2,048 cycles, or `prof_force`.
    // Probabilities are out of 256 (256 = always).
    integer     prof_force = -1;
    integer     prof;
    reg [7:0]   rl_min, rl_span, bl_min, bl_span;
    reg [8:0]   p_rgap, p_arrdy, p_awrdy, p_wrdy;
    integer     maxr;
    reg         wfirst;                             // W taken before its AW
    always @(*) begin
        prof = (prof_force >= 0) ? prof_force : ((cyc >> 11) % 6);
        wfirst = ((prof % 2) == 0);
        case (prof)
            0: begin                                // fast, always ready
                rl_min = 8'd2;  rl_span = 8'd2;  p_rgap = 9'd0;
                p_arrdy = 9'd256; p_awrdy = 9'd256; p_wrdy = 9'd256;
                bl_min = 8'd1;  bl_span = 8'd3;  maxr = 16;
            end
            1: begin                                // jitter
                rl_min = 8'd4;  rl_span = 8'd36; p_rgap = 9'd64;
                p_arrdy = 9'd192; p_awrdy = 9'd200; p_wrdy = 9'd220;
                bl_min = 8'd8;  bl_span = 8'd52; maxr = 8;
            end
            2: begin                                // DRAM-like
                rl_min = 8'd18; rl_span = 8'd8;  p_rgap = 9'd16;
                p_arrdy = 9'd256; p_awrdy = 9'd256; p_wrdy = 9'd240;
                bl_min = 8'd40; bl_span = 8'd20; maxr = 16;
            end
            3: begin                                // choppy
                rl_min = 8'd1;  rl_span = 8'd2;  p_rgap = 9'd128;
                p_arrdy = 9'd128; p_awrdy = 9'd128; p_wrdy = 9'd128;
                bl_min = 8'd1;  bl_span = 8'd19; maxr = 4;
            end
            4: begin                                // jittered reads, writes always taken
                rl_min = 8'd3;  rl_span = 8'd30; p_rgap = 9'd64;
                p_arrdy = 9'd224; p_awrdy = 9'd256; p_wrdy = 9'd256;
                bl_min = 8'd2;  bl_span = 8'd10; maxr = 16;
            end
            default: begin                          // DRAM-like reads, writes always taken
                rl_min = 8'd18; rl_span = 8'd8;  p_rgap = 9'd32;
                p_arrdy = 9'd256; p_awrdy = 9'd256; p_wrdy = 9'd256;
                bl_min = 8'd30; bl_span = 8'd30; maxr = 16;
            end
        endcase
    end

    localparam integer QD = 64;
    reg [AW-1:0] arq_a [0:QD-1];
    reg [7:0]    arq_l [0:QD-1];
    integer      arq_t [0:QD-1];
    reg [AW-1:0] awq_a [0:QD-1];
    reg [7:0]    awq_l [0:QD-1];
    integer      bq_t  [0:QD-1];
    integer      arq_h = 0, arq_n = 0, rbeat = 0;
    integer      awq_h = 0, awq_n = 0, wbeat = 0;
    integer      bq_h = 0, bq_n = 0;
    reg [DW-1:0] wq_d [0:QD-1];
    reg          wq_l [0:QD-1];
    integer      wq_h = 0, wq_n = 0;
    reg [31:0]   rs = 32'h1234_5678;
    integer      k, a;

    integer n_werr = 0, n_wbeat = 0, n_rbeat = 0, n_proto = 0;

    always @(posedge clk) begin
        if (!resetn) begin
            arready <= 1'b0; awready <= 1'b0; wready <= 1'b0;
            rvalid <= 1'b0; rlast <= 1'b0; bvalid <= 1'b0;
            arq_h = 0; arq_n = 0; rbeat = 0;
            awq_h = 0; awq_n = 0; wbeat = 0;
            bq_h = 0; bq_n = 0; wq_h = 0; wq_n = 0;
        end
        else begin
            // ---- AR ----
            if (arvalid && arready) begin
                k = (arq_h + arq_n) % QD;
                arq_a[k] = araddr;
                arq_l[k] = arlen;
                rs = xs(rs);
                arq_t[k] = cyc + rl_min + (rs % (rl_span + 1));
                arq_n = arq_n + 1;
            end
            rs = xs(rs);
            arready <= (arq_n < maxr) && ({1'b0, rs[7:0]} < p_arrdy);

            // ---- R: retire the beat taken, then offer the next ----
            if (rvalid && rready) begin
                n_rbeat = n_rbeat + 1;
                if (rlast) begin
                    arq_h = (arq_h + 1) % QD;
                    arq_n = arq_n - 1;
                    rbeat = 0;
                end
                else begin
                    rbeat = rbeat + 1;
                end
            end
            if (!(rvalid && !rready)) begin
                rs = xs(rs);
                if ((arq_n != 0) && (cyc >= arq_t[arq_h]) && ({1'b0, rs[7:0]} >= p_rgap)) begin
                    rvalid <= 1'b1;
                    rdata  <= mem[widx(arq_a[arq_h]) + rbeat];
                    rlast  <= (rbeat == arq_l[arq_h]);
                end
                else begin
                    rvalid <= 1'b0;
                    rlast  <= 1'b0;
                end
            end

            // ---- AW ----
            if (awvalid && awready) begin
                k = (awq_h + awq_n) % QD;
                awq_a[k] = awaddr;
                awq_l[k] = awlen;
                awq_n = awq_n + 1;
            end
            rs = xs(rs);
            awready <= (awq_n < QD - 2) && ({1'b0, rs[7:0]} < p_awrdy);

            // ---- W: beats queue in order and pair with AWs as they arrive; a
            // `wfirst` profile takes them ahead of their AW, as AXI allows ----
            if (wvalid && wready) begin
                k = (wq_h + wq_n) % QD;
                wq_d[k] = wdata;
                wq_l[k] = wlast;
                wq_n = wq_n + 1;
            end
            while ((wq_n != 0) && (awq_n != 0)) begin
                n_wbeat = n_wbeat + 1;
                a = widx(awq_a[awq_h]) + wbeat;
                if (wq_l[wq_h] !== (wbeat == awq_l[awq_h])) begin
                    n_proto = n_proto + 1;
                    if (n_proto <= 8) begin
                        $display("  ERROR wlast %b on beat %0d of a %0d-beat burst at cycle %0d",
                                 wq_l[wq_h], wbeat, awq_l[awq_h] + 1, cyc);
                    end
                end
                if (wantv[a] && (wq_d[wq_h] !== want[a])) begin
                    n_werr = n_werr + 1;
                    if (n_werr <= 8) begin
                        $display("  ERROR W cycle %0d addr %h: wrote %h want %h",
                                 cyc, a * 32, wq_d[wq_h][31:0], want[a][31:0]);
                    end
                end
                mem[a] = wq_d[wq_h];
                wq_h = (wq_h + 1) % QD;
                wq_n = wq_n - 1;
                if (wbeat == awq_l[awq_h]) begin
                    awq_h = (awq_h + 1) % QD;
                    awq_n = awq_n - 1;
                    wbeat = 0;
                    k = (bq_h + bq_n) % QD;
                    rs = xs(rs);
                    bq_t[k] = cyc + bl_min + (rs % (bl_span + 1));
                    bq_n = bq_n + 1;
                end
                else begin
                    wbeat = wbeat + 1;
                end
            end
            rs = xs(rs);
            wready <= (wfirst || (awq_n != 0)) && (wq_n < QD - 2) && ({1'b0, rs[7:0]} < p_wrdy);

            // ---- B ----
            if (bvalid && bready) begin
                bq_h = (bq_h + 1) % QD;
                bq_n = bq_n - 1;
            end
            if (!(bvalid && !bready)) begin
                bvalid <= (bq_n != 0) && (cyc >= bq_t[bq_h]);
            end
        end
    end

    // ---------------------------------------------------------------- monitors
    reg          pa_aw = 1'b0, pa_ar = 1'b0, pa_w = 1'b0;
    reg [AW-1:0] pa_awaddr, pa_araddr;
    reg [7:0]    pa_awlen, pa_arlen;
    reg [DW-1:0] pa_wdata;
    reg          pa_wlast;
    integer      n_uf = 0, n_bub = 0, n_offer = 0;
    // The last eight cycles of the write path, printed at the first bad pop.
    integer      h_cyc [0:7];
    reg [7:0]    h_sig [0:7];
    integer      h_cnt [0:7], h_wl [0:7], hk;
    reg [31:0]   h_wd [0:7], h_wa [0:7];
    reg          w_open = 1'b0;                     // a burst begun, not yet ended

    always @(posedge clk) begin
        if (resetn) begin
            if (pa_aw && !(awvalid && (awaddr == pa_awaddr) && (awlen == pa_awlen))) begin
                n_proto = n_proto + 1;
                if (n_proto <= 8) $display("  ERROR AW changed before acceptance at cycle %0d", cyc);
            end
            if (pa_ar && !(arvalid && (araddr == pa_araddr) && (arlen == pa_arlen))) begin
                n_proto = n_proto + 1;
                if (n_proto <= 8) $display("  ERROR AR changed before acceptance at cycle %0d", cyc);
            end
            if (pa_w && !(wvalid && (wdata === pa_wdata) && (wlast == pa_wlast))) begin
                n_proto = n_proto + 1;
                if (n_proto <= 8) begin
                    $display("  ERROR W changed before acceptance at cycle %0d: valid %b data %h -> %h",
                             cyc, wvalid, pa_wdata[31:0], wdata[31:0]);
                end
            end
            // A W beat carries its word from the cycle valid rises, taken or not.
            if (wvalid && wantv[widx(dut.w_addr)] && (wdata !== want[widx(dut.w_addr)])) begin
                n_offer = n_offer + 1;
                if (n_offer <= 8) begin
                    $display("  ERROR W offered cycle %0d addr %h: data %h want %h | ready %b fcnt %0d occ %0d f_empty %b f_rd %b f_wr %b wst %0d w_left %0d",
                             cyc, dut.w_addr, wdata[31:0], want[widx(dut.w_addr)][31:0], wready,
                             dut.fcnt, dut.occ, dut.u_dfifo.rd_busy, dut.u_dfifo.rd_en, dut.f_wr,
                             dut.wst, dut.w_left);
                end
            end
            if (awvalid && awready
                && (({1'b0, awaddr[11:5]} + {1'b0, awlen} + 9'd1) > 9'd128)) begin
                n_proto = n_proto + 1;
                if (n_proto <= 8) $display("  ERROR AW crosses 4 KB: %h len %0d", awaddr, awlen);
            end
            if (arvalid && arready
                && (({1'b0, araddr[11:5]} + {1'b0, arlen} + 9'd1) > 9'd128)) begin
                n_proto = n_proto + 1;
                if (n_proto <= 8) $display("  ERROR AR crosses 4 KB: %h len %0d", araddr, arlen);
            end
            // A pop the FIFO cannot honour: the beat repeats the word presented
            // and the counters lose one the FIFO still holds.
            h_cyc[cyc % 8] = cyc;
            h_sig[cyc % 8] = {dut.f_wr, dut.u_dfifo.rd_busy, dut.u_dfifo.rd_en, dut.w_take,
                              wvalid, wready, dut.wst};
            h_cnt[cyc % 8] = dut.fcnt;
            h_wl[cyc % 8]  = dut.w_left;
            h_wd[cyc % 8]  = wdata[31:0];
            h_wa[cyc % 8]  = (wvalid && wantv[widx(dut.w_addr)]) ? want[widx(dut.w_addr)][31:0] : 32'd0;
            if (dut.u_dfifo.rd_en && dut.u_dfifo.rd_busy) begin
                n_uf = n_uf + 1;
                if (n_uf <= 8) begin
                    $display("  ERROR pop on an empty data FIFO at cycle %0d: fcnt %0d occ %0d wst %0d w_left %0d f_wr %b wdata %h",
                             cyc, dut.fcnt, dut.occ, dut.wst, dut.w_left, dut.f_wr, wdata[31:0]);
                end
                if (n_uf == 1) begin
                    $display("    cycle  f_wr f_empty f_rd w_take wvalid wready wst w_left fcnt  wdata    wanted");
                    for (hk = 7; hk >= 0; hk = hk - 1) begin
                        $display("    %6d   %b     %b      %b     %b      %b      %b     %0d    %0d     %0d   %h %h",
                                 h_cyc[(cyc - hk) % 8], h_sig[(cyc - hk) % 8][7], h_sig[(cyc - hk) % 8][6],
                                 h_sig[(cyc - hk) % 8][5], h_sig[(cyc - hk) % 8][4],
                                 h_sig[(cyc - hk) % 8][3], h_sig[(cyc - hk) % 8][2],
                                 h_sig[(cyc - hk) % 8][1:0], h_wl[(cyc - hk) % 8],
                                 h_cnt[(cyc - hk) % 8], h_wd[(cyc - hk) % 8], h_wa[(cyc - hk) % 8]);
                    end
                end
            end
            // Bubbles inside a burst: legal AXI, counted for the report.
            if (w_open && !wvalid) n_bub = n_bub + 1;
            if (wvalid && wready) w_open <= !wlast;
            pa_aw <= awvalid && !awready; pa_awaddr <= awaddr; pa_awlen <= awlen;
            pa_ar <= arvalid && !arready; pa_araddr <= araddr; pa_arlen <= arlen;
            pa_w  <= wvalid && !wready;   pa_wdata  <= wdata;  pa_wlast <= wlast;
        end
    end

    // ---------------------------------------------------------------- stimulus
    integer errors = 0, checks = 0, spin = 0, n_left = 0;
    reg [31:0] ts = 32'h0BAD_5EED;
    integer perm [0:2047];

    function [31:0] f32(input integer kk);
        integer e;
        reg [31:0] m;
        begin
            f32 = 32'd0;
            if (kk > 0) begin
                e = 0;
                while ((kk >> (e + 1)) != 0) begin
                    e = e + 1;
                end
                m = kk << (23 - e);
                f32 = {1'b0, 8'd127 + e[7:0], m[22:0]};
            end
        end
    endfunction

    function integer rnd(input integer n);
        begin
            ts  = xs(ts);
            rnd = ts % n;
        end
    endfunction

    // One register write through the configuration queue, never into a full one.
    task wr(input [7:0] ad, input [63:0] d);
        begin
            @(negedge clk);
            while (!cfg_room) @(negedge clk);
            cfg_en = 1'b1; cfg_addr = ad; cfg_data = d;
            @(negedge clk);
            cfg_en = 1'b0;
        end
    endtask

    task hdr(input sel, input [AW-1:0] base, input [2:0] ndim);
        begin wr(8'h10, {17'd0, ndim, base, 3'd0, sel}); end
    endtask

    task dim(input sel, input [2:0] d, input [15:0] cnt, input signed [31:0] strd);
        begin
            wr(8'h18, {12'd0, strd, cnt, d, sel});
            wr(8'h20, 64'd0);
        end
    endtask

    task go(input [2:0] mode, input [7:0] flags, input fp);
        begin wr(8'h00, {46'd0, fp, 1'b1, flags, 3'd0, 2'd1, mode}); end
    endtask

    task wait_idle(input [255:0] what);
        begin
            @(negedge clk);
            spin = 0;
            while (stat_busy && spin < 400000) begin
                spin = spin + 1;
                @(negedge clk);
            end
            if (spin >= 400000) begin
                errors = errors + 1;
                $display("  FAIL %0s: the mover never went idle (wst %0d fcnt %0d occ %0d)",
                         what, dut.wst, dut.fcnt, dut.occ);
            end
        end
    endtask

    // Idle: nothing may be left in the data FIFO or in its counters.
    task idle_clean(input [255:0] what);
        begin
            checks = checks + 1;
            if (!dut.u_dfifo.rd_busy || (dut.fcnt != 0) || (dut.occ != 0)) begin
                errors = errors + 1;
                $display("  FAIL %0s: idle with the data FIFO %0s, fcnt %0d occ %0d",
                         what, dut.u_dfifo.rd_busy ? "empty" : "HOLDING A WORD",
                         dut.fcnt, dut.occ);
            end
        end
    endtask

    // Expect `src` word at `dst`; poison `dst` so a missing write shows.
    task expect_word(input [AW-1:0] dst, input [DW-1:0] v);
        begin
            want[widx(dst)]  = v;
            wantv[widx(dst)] = 1'b1;
            mem[widx(dst)]   = ~v;
        end
    endtask

    integer n_dbad = 0;
    task check_word(input [AW-1:0] dst, input [255:0] what, input integer where);
        begin
            checks = checks + 1;
            if (mem[widx(dst)] !== want[widx(dst)]) begin
                n_dbad = n_dbad + 1;
                if (n_dbad <= 12) begin
                    $display("  FAIL %0s [%0d] at %h: got %h want %h", what, where, dst,
                             mem[widx(dst)][31:0], want[widx(dst)][31:0]);
                end
            end
            wantv[widx(dst)] = 1'b0;
        end
    endtask

    // An index list of `n` entries at `ib`, u32 or fp32.
    task put_index(input [AW-1:0] ib, input integer r, input integer v, input fp);
        begin
            if ((r % 8) == 0) mem[widx(ib) + r / 8] = {DW{1'b0}};
            mem[widx(ib) + r / 8][(r % 8) * 32 +: 32] = fp ? f32(v) : v;
        end
    endtask

    task q_gather(input [AW-1:0] ib, input integer n, input fp, input [AW-1:0] db);
        begin
            wr(8'h30, {8'd0, n[15:0], ib});
            wr(8'h50, {16'd0, 16'd1, 32'd32});
            hdr(1'b0, TBL, 3'd1);
            hdr(1'b1, db, 3'd1);
            dim(1'b1, 3'd0, n[15:0], PITCH);
            go(3'd2, 8'd0, fp);
        end
    endtask

    // ---- short gathers, queued back to back: the silicon shape ----
    integer sm_chk;
    task check_short(input integer m);
        integer r, s;
        begin
            s = m % 16;
            for (r = 0; r < SROWS; r = r + 1) begin
                check_word(DSTS + (s * SROWS + r) * PITCH, "short gather", m * 100 + r);
            end
        end
    endtask

    task short_batch(input integer nm, input fp);
        integer m, s, r, v, done0;
        begin
            done0 = stat_done;
            sm_chk = 0;
            for (m = 0; m < nm; m = m + 1) begin
                spin = 0;
                while (((m - (stat_done - done0)) >= 8) && (spin < 200000)) begin
                    spin = spin + 1;
                    @(negedge clk);
                end
                if (spin >= 200000) begin
                    errors = errors + 1;
                    $display("  FAIL short gathers stalled at move %0d (done %0d, fault %0d)",
                             m, stat_done - done0, stat_fault);
                    m = nm;
                end
                else begin
                    while (sm_chk < (stat_done - done0)) begin
                        check_short(sm_chk);
                        sm_chk = sm_chk + 1;
                    end
                    s = m % 16;
                    for (r = 0; r < SROWS; r = r + 1) begin
                        v = rnd(NTBL);
                        put_index(IDXS + s * 128, r, v, fp);
                        expect_word(DSTS + (s * SROWS + r) * PITCH, mem[widx(TBL) + v]);
                    end
                    q_gather(IDXS + s * 128, SROWS, fp, DSTS + s * SROWS * PITCH);
                end
            end
            wait_idle("short gathers");
            while (sm_chk < (stat_done - done0)) begin
                check_short(sm_chk);
                sm_chk = sm_chk + 1;
            end
            checks = checks + 1;
            if ((stat_done - done0) != nm) begin
                errors = errors + 1;
                $display("  FAIL %0d of %0d short gathers completed", stat_done - done0, nm);
            end
        end
    endtask

    task big_gather(input integer n, input fp);
        integer r, v;
        begin
            for (r = 0; r < n; r = r + 1) begin
                v = rnd(NTBL);
                put_index(IDXL, r, v, fp);
                expect_word(DSTL + r * PITCH, mem[widx(TBL) + v]);
            end
            q_gather(IDXL, n, fp, DSTL);
            wait_idle("long gather");
            for (r = 0; r < n; r = r + 1) begin
                check_word(DSTL + r * PITCH, fp ? "long fp32 gather" : "long gather", r);
            end
        end
    endtask

    // Rows scattered to a permutation of n pitched rows.
    task big_scatter(input integer n);
        integer r, j, t;
        begin
            for (r = 0; r < n; r = r + 1) perm[r] = r;
            for (r = n - 1; r > 0; r = r - 1) begin
                j = rnd(r + 1);
                t = perm[r]; perm[r] = perm[j]; perm[j] = t;
            end
            for (r = 0; r < n; r = r + 1) begin
                mem[widx(SRCC) + r] = pat(r, 32'h5C00_0000 ^ (cyc << 8));
                put_index(IDXL, r, perm[r], 1'b0);
                expect_word(DSTL + perm[r] * PITCH, mem[widx(SRCC) + r]);
            end
            wr(8'h30, {8'd0, n[15:0], IDXL});
            wr(8'h50, {16'd0, 16'd1, PITCH[31:0]});
            hdr(1'b0, SRCC, 3'd1);
            dim(1'b0, 3'd0, n[15:0], 32);
            hdr(1'b1, DSTL, 3'd1);
            go(3'd6, 8'd0, 1'b0);
            wait_idle("scatter");
            for (r = 0; r < n; r = r + 1) begin
                check_word(DSTL + r * PITCH, "scatter", r);
            end
        end
    endtask

    // A copy of n words, either side contiguous (stride 32) or pitched.
    task copy(input [AW-1:0] sb, input integer ss, input [AW-1:0] db, input integer ds,
              input integer n, input [7:0] flags, input [255:0] what);
        integer r;
        begin
            for (r = 0; r < n; r = r + 1) begin
                mem[widx(sb + r * ss)] = pat(r, 32'hC0_000000 ^ (cyc << 8));
                expect_word(db + r * ds, mem[widx(sb + r * ss)]);
            end
            hdr(1'b0, sb, 3'd1);
            dim(1'b0, 3'd0, n[15:0], ss);
            hdr(1'b1, db, 3'd1);
            dim(1'b1, 3'd0, n[15:0], ds);
            go(3'd0, flags, 1'b0);
            wait_idle(what);
            for (r = 0; r < n; r = r + 1) begin
                check_word(db + r * ds, what, r);
            end
        end
    endtask

    integer i, c0, c1;
    initial begin
        for (i = 0; i < WORDS; i = i + 1) begin
            mem[i]   = pat(i, 32'hA500_0000);
            want[i]  = {DW{1'b0}};
            wantv[i] = 1'b0;
        end

        repeat (8) @(negedge clk);
        resetn = 1'b1;
        repeat (8) @(negedge clk);

        $display("--- 1. %0d-row gathers, fp32 indices, pitch %0d, queued back to back ---",
                 SROWS, PITCH);
        c0 = cyc;
        short_batch(400, 1'b1);
        idle_clean("fp32 short gathers");
        $display("    400 moves, %0d cycles", cyc - c0);

        $display("--- 2. %0d-row gathers, u32 indices ---", SROWS);
        c0 = cyc;
        short_batch(400, 1'b0);
        idle_clean("u32 short gathers");
        $display("    400 moves, %0d cycles", cyc - c0);

        $display("--- 3. %0d-row gathers, pitch %0d ---", NBIG, PITCH);
        c0 = cyc;
        big_gather(NBIG, 1'b1);
        big_gather(NBIG, 1'b0);
        big_gather(NBIG, 1'b1);
        big_gather(NBIG, 1'b0);
        idle_clean("long gathers");
        $display("    4 moves, %0d cycles", cyc - c0);

        $display("--- 4. %0d-row scatters to a permutation, pitch %0d ---", NBIG, PITCH);
        c0 = cyc;
        big_scatter(NBIG);
        big_scatter(NBIG);
        idle_clean("scatters");
        $display("    2 moves, %0d cycles", cyc - c0);

        $display("--- 5. copies: pitched destination, pitched source, coalesced runs ---");
        c0 = cyc;
        copy(SRCC, 32, DSTL, PITCH, NBIG, 8'd0, "copy to pitch");
        copy(SRCC, 32, DSTL, PITCH, NBIG, 8'd0, "copy to pitch");
        // flags[3] coalesces writes; flags[7:5] caps a burst at 2^(k-1), 0 = 128.
        copy(SRCP, PITCH, DSTC, 32, NBIG, 8'h08, "pitched source, coalesced");
        copy(SRCP, PITCH, DSTC, 32, NBIG, 8'h48, "pitched source, cap 4");
        for (i = 0; i < 5; i = i + 1) begin
            copy(SRCC, 32, DSTC, 32, 2048, {i[2:0], 5'b01000}, "contiguous, coalesced");
        end
        idle_clean("copies");
        $display("    9 moves, %0d cycles", cyc - c0);

        // A word stranded in the FIFO by any earlier move shifts this one.
        $display("--- 6. canary gather ---");
        prof_force = 0;
        c1 = n_dbad;
        short_batch(1, 1'b0);
        idle_clean("canary");
        if (n_dbad != c1) $display("  FAIL the canary gather is wrong: a word was left behind");

        errors = errors + n_dbad + n_werr + n_proto + n_uf + n_offer;
        $display("    %0d R beats, %0d W beats, %0d W bubbles inside bursts", n_rbeat, n_wbeat, n_bub);
        $display("    %0d bad destination words, %0d bad W beats taken, %0d bad W beats offered",
                 n_dbad, n_werr, n_offer);
        $display("    %0d empty-FIFO pops, %0d protocol errors", n_uf, n_proto);
        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end

    initial begin
        #8000000;
        $display("  FAIL -- watchdog (cycle %0d, wst %0d, fcnt %0d)", cyc, dut.wst, dut.fcnt);
        $finish;
    end

endmodule

`default_nettype wire

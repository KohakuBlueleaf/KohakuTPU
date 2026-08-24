// THE L2 ADAPTER, ON A LOCAL LINK: router <-> noc_l2_adapter <-> endpoint.

// Two OOC runs proved it synthesises and closes timing. Nothing had ever
// checked that it returns the right data, which is what this does.

// The bench plays BOTH faces: the endpoint on eu/ep, the router on rt/ru.

// A second instance runs at PASS=1 off the same stimulus, so bypass is checked
// on every cycle of every test below rather than in a section of its own.

`default_nettype none
`timescale 1ns/1ps

module noc_l2_adapter_tb;
    localparam integer FW = 288, PW = 4;
    localparam integer DEPTH = 8192;             // 8 URAM at a 256-bit line
    localparam [39:0]  BASE  = 40'h00_F000_0000;
    localparam integer BITS  = 18;               // 5 + clog2(DEPTH)
    localparam [7:0]   CB    = 8'hE0;            // the addon control window

    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1;
    localparam [3:0] T_MEM_RD_RESP = 4'h2, T_MEM_WR_ACK = 4'h3;
    localparam [3:0] T_MEM_WR_DATA = 4'h4, T_CU_INST = 4'h5, T_CU_CTRL = 4'h7;

    // The endpoint behind the adapter, the node its requests are addressed to,
    // and the controller that programs the window.
    localparam [3:0] EX = 4'd1, EY = 4'd1;
    localparam [3:0] MX = 4'd0, MY = 4'd1;
    localparam [3:0] OX = 4'd0, OY = 4'd0;

    reg clk = 0, rst = 1;
    always begin
        #2 clk = ~clk;
    end

    // ---------------------------------------------------------- the two DUTs
    reg  [FW-1:0] rt_data, eu_data;
    reg           rt_valid, eu_valid, ru_busy, ep_busy;

    wire [FW-1:0] ep_data, ru_data;
    wire          ep_valid, ru_valid, rt_busy, eu_busy;

    noc_l2_adapter #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DEPTH(DEPTH),
                     .L2_BASE(BASE), .L2_BITS(BITS), .CTRL_BASE(CB),
                     .PASS(0)) dut (
        .clk(clk), .rst(rst),
        .rt_data(rt_data), .rt_valid(rt_valid), .rt_busy(rt_busy),
        .ru_data(ru_data), .ru_valid(ru_valid), .ru_busy(ru_busy),
        .ep_data(ep_data), .ep_valid(ep_valid), .ep_busy(ep_busy),
        .eu_data(eu_data), .eu_valid(eu_valid), .eu_busy(eu_busy)
    );

    wire [FW-1:0] b_ep_data, b_ru_data;
    wire          b_ep_valid, b_ru_valid, b_rt_busy, b_eu_busy;

    noc_l2_adapter #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DEPTH(DEPTH),
                     .L2_BASE(BASE), .L2_BITS(BITS), .CTRL_BASE(CB),
                     .PASS(1)) byp (
        .clk(clk), .rst(rst),
        .rt_data(rt_data), .rt_valid(rt_valid), .rt_busy(b_rt_busy),
        .ru_data(b_ru_data), .ru_valid(b_ru_valid), .ru_busy(ru_busy),
        .ep_data(b_ep_data), .ep_valid(b_ep_valid), .ep_busy(ep_busy),
        .eu_data(eu_data), .eu_valid(eu_valid), .eu_busy(b_eu_busy)
    );

    integer errors = 0, checks = 0, spin;

    task chk(input cond, input [255:0] what, input integer where);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                if (errors < 25) begin
                    $display("  FAIL %0s [%0d]", what, where);
                end
            end
        end
    endtask

    // ================================================= bypass, every cycle
    // PASS=1 must be the six-signal identity and nothing else.
    integer byp_bad = 0, byp_seen = 0;

    always @(posedge clk) if (!rst) begin
        if (
            (b_ep_data !== rt_data)
            || (b_ep_valid !== rt_valid)
            || (b_rt_busy !== ep_busy)
            || (b_ru_data !== eu_data)
            || (b_ru_valid !== eu_valid)
            || (b_eu_busy !== ru_busy)
        ) begin
            byp_bad = byp_bad + 1;
        end
        if (b_ru_valid && !ru_busy) begin
            byp_seen = byp_seen + 1;
        end
    end

    // ==================================================== link drivers
    // The #1 puts the busy sample inside the cycle its rising edge belongs to.
    task ep_send(input [FW-1:0] f);
        begin
            @(negedge clk);
            eu_data = f; eu_valid = 1'b1;
            #1;
            while (eu_busy) begin @(negedge clk); #1; end
            @(negedge clk);
            eu_valid = 1'b0;
        end
    endtask

    task rt_send(input [FW-1:0] f);
        begin
            @(negedge clk);
            rt_data = f; rt_valid = 1'b1;
            #1;
            while (rt_busy) begin @(negedge clk); #1; end
            @(negedge clk);
            rt_valid = 1'b0;
        end
    endtask

    // ==================================================== what each face saw
    localparam integer CAP = 128;

    reg [FW-1:0] ep_f [0:CAP-1];
    reg [FW-1:0] ru_f [0:CAP-1];
    integer ep_n = 0, ru_n = 0;

    always @(posedge clk) if (!rst) begin
        if (ep_valid && !ep_busy) begin
            if (ep_n < CAP) begin
                ep_f[ep_n] = ep_data;
            end
            ep_n = ep_n + 1;
        end
        if (ru_valid && !ru_busy) begin
            if (ru_n < CAP) begin
                ru_f[ru_n] = ru_data;
            end
            ru_n = ru_n + 1;
        end
    end

    task clear_capture;
        begin ep_n = 0; ru_n = 0; end
    endtask

    // "Never forever" as a running property. Counted only while the bench is
    // NOT holding the far side, so what is measured is the adapter's own stall.
    localparam integer BUSY_MAX = 500;
    integer rt_hi = 0, eu_hi = 0, stuck = 0;

    always @(posedge clk) if (!rst) begin
        rt_hi = (rt_busy && !ep_busy) ? rt_hi + 1 : 0;
        eu_hi = (eu_busy && !ru_busy) ? eu_hi + 1 : 0;
        if ((rt_hi > BUSY_MAX) || (eu_hi > BUSY_MAX)) begin
            stuck = stuck + 1;
        end
    end

    // Field accessors on a captured flit.
    function [3:0]   f_ty  (input [FW-1:0] f); f_ty  = f[FW-4*PW-1 -: 4];   endfunction
    function [7:0]   f_tag (input [FW-1:0] f); f_tag = f[FW-4*PW-5 -: 8];   endfunction
    function         f_last(input [FW-1:0] f); f_last = f[FW-4*PW-13];      endfunction
    function [1:0]   f_word(input [FW-1:0] f); f_word = f[FW-4*PW-15 -: 2]; endfunction
    function [3:0]   f_dx  (input [FW-1:0] f); f_dx  = f[FW-1      -: PW];  endfunction
    function [3:0]   f_dy  (input [FW-1:0] f); f_dy  = f[FW-PW-1   -: PW];  endfunction
    function [3:0]   f_sx  (input [FW-1:0] f); f_sx  = f[FW-2*PW-1 -: PW];  endfunction
    function [3:0]   f_sy  (input [FW-1:0] f); f_sy  = f[FW-3*PW-1 -: PW];  endfunction
    function [255:0] f_pay (input [FW-1:0] f); f_pay = f[255:0];            endfunction
    function [7:0]   f_cop (input [FW-1:0] f); f_cop = f[255 -: 8];         endfunction
    function [7:0]   f_cix (input [FW-1:0] f); f_cix = f[247 -: 8];         endfunction
    function [63:0]  f_cvl (input [FW-1:0] f); f_cvl = f[239 -: 64];        endfunction

    // ==================================================== flit builders
    function [FW-1:0] hdr(input [3:0] ty, input [3:0] dx, input [3:0] dy,
                          input [3:0] sx, input [3:0] sy, input [7:0] txn);
        begin
            hdr = {FW{1'b0}};
            hdr[FW-1      -: PW] = dx;
            hdr[FW-PW-1   -: PW] = dy;
            hdr[FW-2*PW-1 -: PW] = sx;
            hdr[FW-3*PW-1 -: PW] = sy;
            hdr[FW-4*PW-1 -: 4]  = ty;
            hdr[FW-4*PW-5 -: 8]  = txn;
        end
    endfunction

    // A streaming fill descriptor, exactly as mx_cluster_cu builds one: STREAM
    // in flags[6], entries in `cnt`, words per entry in `ew`.
    function [FW-1:0] rd_stream(input [7:0] txn, input [39:0] addr,
                                input [7:0] cnt, input [7:0] ew,
                                input [7:0] flags);
        begin
            rd_stream = hdr(T_MEM_RD_REQ, MX, MY, EX, EY, txn);
            rd_stream[255 -: 40] = addr;
            rd_stream[207 -: 8]  = flags | 8'h40;
            rd_stream[199 -: 8]  = cnt;
            rd_stream[165 -: 8]  = ew;
        end
    endfunction

    // The single-shot read mag_mem_port answers from `st`: len+1 beats, word 0
    // on every one, LAST only on the final beat.
    function [FW-1:0] rd_plain(input [7:0] txn, input [39:0] addr,
                               input [7:0] len);
        begin
            rd_plain = hdr(T_MEM_RD_REQ, MX, MY, EX, EY, txn);
            rd_plain[255 -: 40] = addr;
            rd_plain[215 -: 8]  = len;
        end
    endfunction

    function [FW-1:0] wr_req(input [7:0] txn, input [39:0] addr,
                             input [7:0] len);
        begin
            wr_req = hdr(T_MEM_WR_REQ, MX, MY, EX, EY, txn);
            wr_req[255 -: 40] = addr;
            wr_req[215 -: 8]  = len;
        end
    endfunction

    function [FW-1:0] wr_data(input [7:0] txn, input [255:0] pay);
        begin
            wr_data = hdr(T_MEM_WR_DATA, MX, MY, EX, EY, txn);
            wr_data[255:0] = pay;
        end
    endfunction

    // A framework control message, addressed to the ENDPOINT's coordinate. The
    // adapter claims it out of the link; noc_cu_base would answer it otherwise.
    function [FW-1:0] ctrl(input [7:0] txn, input [7:0] op, input [7:0] idx,
                           input [63:0] val);
        begin
            ctrl = hdr(T_CU_CTRL, EX, EY, OX, OY, txn);
            ctrl[255 -: 8]  = op;
            ctrl[247 -: 8]  = idx;
            ctrl[239 -: 64] = val;
        end
    endfunction

    function [255:0] line_pay(input integer ln);
        begin line_pay = {8{16'hBEEF, ln[15:0]}}; end
    endfunction

    function [39:0] line_addr(input integer ln);
        begin line_addr = BASE + (ln * 32); end
    endfunction

    // Stage `n` lines from `ln0` as one burst, the shape a cluster drains in.
    task stage(input integer ln0, input integer n, input [7:0] txn);
        integer k;
        begin
            ep_send(wr_req(txn, line_addr(ln0), n[7:0] - 8'd1));
            for (k = 0; k < n; k = k + 1) begin
                ep_send(wr_data(txn, line_pay(ln0 + k)));
            end
        end
    endtask

    task settle(input integer n);
        begin repeat (n) @(negedge clk); end
    endtask

    // ==================================================== the run
    integer i, e, w, bad;
    reg [FW-1:0] f;
    reg [63:0]   cv;

    initial begin
        rt_data = 0; rt_valid = 0; eu_data = 0; eu_valid = 0;
        ru_busy = 0; ep_busy = 0;

        repeat (10) @(negedge clk);
        rst = 0;
        repeat (10) @(negedge clk);

        // ============================================ 0. the control plane
        // Disabled at reset, so an unprogrammed adapter claims nothing.
        $display("--- 0. CU_CTRL: the window is programmed, not compiled in ---");
        clear_capture;
        ep_send(rd_stream(8'h01, line_addr(0), 8'd1, 8'd4, 8'h00));
        settle(40);
        chk(ru_n == 1, "before enable, an in-window read is forwarded", ru_n);
        chk(ep_n == 0, "and nothing is served", ep_n);

        clear_capture;
        rt_send(ctrl(8'h70, 8'd0, CB + 8'd0, 64'd0));       // CAPS
        settle(20);
        chk(ep_n == 0, "a claimed CU_CTRL never reaches the endpoint", ep_n);
        chk(ru_n == 1, "it is answered on the router face", ru_n);
        if (ru_n > 0) begin
            f = ru_f[0];
            chk(f_ty(f) == T_CU_CTRL, "the reply is a CU_CTRL", 0);
            chk(f_cop(f) == 8'd2, "op 2, a read response", 0);
            chk(f_cix(f) == (CB + 8'd0), "echoing its index", 0);
            chk(f_tag(f) == 8'h70, "and its txn", 0);
            chk((f_dx(f) == OX) && (f_dy(f) == OY),
                "addressed back to the controller", 0);
            chk((f_sx(f) == EX) && (f_sy(f) == EY),
                "from the endpoint's coordinate, as noc_cu_base would", 0);
            chk(f_cvl(f) == {16'h0002, 8'd1, BITS[7:0], DEPTH[31:0]},
                "CAPS names the slot, the version and the store", 0);
        end

        // An index OUTSIDE the window belongs to the endpoint and passes through.
        clear_capture;
        rt_send(ctrl(8'h71, 8'd0, 8'd0, 64'd0));
        settle(20);
        chk(ep_n == 1, "a CU_CTRL outside the window reaches the endpoint", ep_n);
        chk(ru_n == 0, "and the adapter does not answer it", ru_n);

        clear_capture;
        rt_send(ctrl(8'h72, 8'd1, CB + 8'd1, {24'd0, BASE}));   // BASE
        rt_send(ctrl(8'h73, 8'd1, CB + 8'd2, 64'd1));           // ENABLE
        rt_send(ctrl(8'h74, 8'd0, CB + 8'd1, 64'd0));
        rt_send(ctrl(8'h75, 8'd0, CB + 8'd2, 64'd0));
        settle(40);
        chk(ru_n == 4, "every control message is answered exactly once", ru_n);
        chk((ru_n > 2) && (f_cvl(ru_f[2]) == {24'd0, BASE}),
            "the base reads back as written", 0);
        chk((ru_n > 3) && (f_cvl(ru_f[3]) == 64'd1), "and so does enable", 0);
        chk(ep_n == 0, "none of them reached the endpoint", ep_n);

        // ============================================ 1. stores, and their acks
        $display("--- 1. stage 16 lines, one burst of 8 per descriptor ---");
        clear_capture;
        stage(0, 8, 8'h20);
        stage(8, 8, 8'h21);
        settle(60);

        chk(ru_n == 0, "an in-range store is swallowed, not forwarded", ru_n);
        chk(ep_n == 2, "one WR_ACK per descriptor", ep_n);
        for (i = 0; i < 2 && i < ep_n; i = i + 1) begin
            f = ep_f[i];
            chk(f_ty(f) == T_MEM_WR_ACK, "the ack is a WR_ACK", i);
            chk(f_tag(f) == (8'h20 + i[7:0]), "the ack carries its txn", i);
            chk((f_dx(f) == EX) && (f_dy(f) == EY),
                "the ack goes back to the writer", i);
            chk((f_sx(f) == MX) && (f_sy(f) == MY),
                "the ack comes from the node that was addressed", i);
        end

        // tag = txn + entry, word = 0..3, LAST on the entry's last word --
        // mag_mem_port's emit, field for field.
        $display("--- 2. a hit: STREAM cnt=4 ew=4, 16 responses ---");
        clear_capture;
        ep_send(rd_stream(8'h10, line_addr(0), 8'd4, 8'd4, 8'h00));
        settle(200);

        chk(ru_n == 0, "an in-range fill is served, not forwarded", ru_n);
        chk(ep_n == 16, "4 entries of 4 words is 16 response flits", ep_n);
        bad = 0;
        for (i = 0; i < 16 && i < ep_n; i = i + 1) begin
            f = ep_f[i];
            e = i / 4;  w = i % 4;
            if (f_ty(f)   !== T_MEM_RD_RESP) begin
                bad = bad + 1;
            end
            if (f_tag(f)  !== (8'h10 + e[7:0])) begin
                bad = bad + 1;
            end
            if (f_word(f) !== w[1:0]) begin
                bad = bad + 1;
            end
            if (f_last(f) !== (w == 3)) begin
                bad = bad + 1;
            end
            if (f_pay(f)  !== line_pay(i)) begin
                bad = bad + 1;
            end
            if ((f_dx(f) !== EX) || (f_dy(f) !== EY)) begin
                bad = bad + 1;
            end
            if ((f_sx(f) !== MX) || (f_sy(f) !== MY)) begin
                bad = bad + 1;
            end
        end
        chk(bad == 0, "every response word is tagged and placed correctly", bad);

        // ============================================ 3. a hit: plain read
        $display("--- 3. a hit: a single-shot read of 4 beats ---");
        clear_capture;
        ep_send(rd_plain(8'h33, line_addr(8), 8'd3));
        settle(120);

        chk(ep_n == 4, "len=3 is four beats", ep_n);
        bad = 0;
        for (i = 0; i < 4 && i < ep_n; i = i + 1) begin
            f = ep_f[i];
            if (f_ty(f)   !== T_MEM_RD_RESP) begin
                bad = bad + 1;
            end
            if (f_tag(f)  !== 8'h33) begin
                bad = bad + 1;
            end
            if (f_word(f) !== 2'd0) begin
                bad = bad + 1;
            end
            if (f_last(f) !== (i == 3)) begin
                bad = bad + 1;
            end
            if (f_pay(f)  !== line_pay(8+i)) begin
                bad = bad + 1;
            end
        end
        chk(bad == 0, "a plain read returns its beats verbatim", bad);
        chk(ru_n == 0, "a plain in-range read is not forwarded", ru_n);

        // ============================================ 4. a miss
        $display("--- 4. a miss: outside the window, forwarded byte for byte ---");
        clear_capture;
        f = rd_stream(8'h44, 40'h00_1000_0000, 8'd4, 8'd4, 8'h00);
        ep_send(f);
        settle(60);
        chk(ru_n == 1, "a miss leaves on the router face", ru_n);
        chk(ep_n == 0, "a miss produces no local response", ep_n);
        chk((ru_n > 0) && (ru_f[0] === f), "the forwarded flit is unchanged", 0);

        // ANOTHER MESH'S SAME OFFSET. The window compare covers [39:BITS], so
        // a remote address falls out as an ordinary miss with no mesh logic.
        clear_capture;
        f = rd_stream(8'h46, BASE | 40'h01_0000_0000, 8'd4, 8'd4, 8'h00);
        ep_send(f);
        settle(60);
        chk(ru_n == 1, "mesh 1's copy of the same offset is a miss", ru_n);
        chk(ep_n == 0, "and is served from nothing local", ep_n);

        clear_capture;
        ep_send(wr_req(8'h45, 40'h00_1000_0000, 8'd1));
        ep_send(wr_data(8'h45, line_pay(999)));
        ep_send(wr_data(8'h45, line_pay(998)));
        settle(60);
        chk(ru_n == 3, "an out-of-range store forwards descriptor and data",
            ru_n);
        chk(ep_n == 0, "and is not acknowledged locally", ep_n);

        // The ERROR below is the point: a run straddling the top of the window
        // is named, then forwarded whole.
        $display("--- 5. straddling the top of the window: 3 ERRORs expected ---");
        clear_capture;
        f = rd_stream(8'h50, line_addr(DEPTH-2), 8'd2, 8'd4, 8'h00);
        ep_send(f);
        settle(60);
        chk(ru_n == 1, "a run past the top of the window is forwarded", ru_n);
        chk(ep_n == 0, "and served from nothing local", ep_n);
        chk((ru_n > 0) && (ru_f[0] === f), "forwarded unchanged", 0);

        // The last line that DOES fit is served, so the boundary is exactly
        // where the guard line puts it: an entry may end at DEPTH-2, not -1.
        clear_capture;
        stage(DEPTH-5, 4, 8'h51);
        settle(40);
        clear_capture;
        ep_send(rd_stream(8'h52, line_addr(DEPTH-5), 8'd1, 8'd4, 8'h00));
        settle(80);
        chk(ep_n == 4, "the last entry that fits under the guard is served",
            ep_n);
        bad = 0;
        for (i = 0; i < 4 && i < ep_n; i = i + 1) begin
            if (f_pay(ep_f[i]) !== line_pay(DEPTH-5+i)) begin
                bad = bad + 1;
            end
        end
        chk(bad == 0, "with the right lines", bad);

        // One line further up ends on the guard line, so it is forwarded.
        clear_capture;
        ep_send(rd_stream(8'h54, line_addr(DEPTH-4), 8'd1, 8'd4, 8'h00));
        settle(60);
        chk(ru_n == 1, "an entry ending on the guard line is forwarded", ru_n);
        chk(ep_n == 0, "and served from nothing local", ep_n);

        // Just below the base: no top-bit match at all, so it never enters.
        clear_capture;
        f = rd_plain(8'h53, BASE - 40'd32, 8'd3);
        ep_send(f);
        settle(60);
        chk(ru_n == 1, "a run reaching UP into the window is forwarded", ru_n);
        chk(ep_n == 0, "and served from nothing local", ep_n);

        // ================================ 6. a reserved flag, and multicast
        // flags[4] IS IGNORED, not refused. It was QUANT; a fetch is never
        // transformed now, so the bit is reserved and a requester that sets it
        // gets an ordinary untransformed read (spec/flit-format s4.1.1). This
        // asserted the opposite -- that such a read is forwarded to MAG -- which
        // would answer the same request differently depending on a dead bit.
        $display("--- 6. a reserved flag is ignored; nd stays with MAG ---");
        clear_capture;
        ep_send(rd_stream(8'h60, line_addr(0), 8'd2, 8'd4, 8'h10));  // flags[4]
        settle(60);
        chk(ru_n == 0, "a read setting the reserved flag is NOT forwarded", ru_n);
        chk(ep_n == 8, "2 entries of 4 words, served like any other", ep_n);

        clear_capture;
        f = rd_stream(8'h61, line_addr(0), 8'd2, 8'd4, 8'h00);
        f[167 -: 2] = 2'd1;                       // one extra destination
        ep_send(f);
        settle(60);
        chk(ru_n == 1, "an in-range multicast read is forwarded", ru_n);
        chk(ep_n == 0, "staging does not multicast", ep_n);

        // ============================================ 7. inbound, and priority
        $display("--- 7. router -> endpoint passes through, response first ---");
        clear_capture;
        f = hdr(T_CU_INST, EX, EY, MX, MY, 8'h70);
        f[255 -: 8] = 8'hAB;
        rt_send(f);
        settle(20);
        chk(ep_n == 1, "an inbound flit reaches the endpoint", ep_n);
        chk((ep_n > 0) && (ep_f[0] === f), "unchanged", 0);

        // ============================================ 8. backpressure
        // The URAM pipeline cannot stop, so the queue is what absorbs a stall.
        $display("--- 8. the endpoint stalls in the middle of a fill ---");
        clear_capture;
        ep_busy = 1'b1;
        ep_send(rd_stream(8'h80, line_addr(0), 8'd4, 8'd4, 8'h00));
        settle(120);
        chk(ep_n == 0, "a busy endpoint receives nothing", ep_n);
        @(negedge clk); ep_busy = 1'b0;
        settle(200);
        chk(ep_n == 16, "and every response survives the stall", ep_n);
        bad = 0;
        for (i = 0; i < 16 && i < ep_n; i = i + 1) begin
            if (f_pay(ep_f[i]) !== line_pay(i)) begin
                bad = bad + 1;
            end
        end
        chk(bad == 0, "in order, with the right lines", bad);

        // A router that never accepts must not stop a hit: the response never
        // travels that way.
        $display("--- 9. a hit is served with the router face jammed ---");
        clear_capture;
        ru_busy = 1'b1;
        ep_send(rd_stream(8'h90, line_addr(4), 8'd1, 8'd4, 8'h00));
        settle(120);
        chk(ep_n == 4, "a jammed router does not stall a hit", ep_n);
        @(negedge clk); ru_busy = 1'b0;
        settle(20);

        // The second descriptor lands while the first run is still emitting:
        // held, not dropped -- and being dropped is silent.
        $display("--- 10. two fills back to back, no gap ---");
        clear_capture;
        ep_send(rd_stream(8'hA0, line_addr(0), 8'd2, 8'd4, 8'h00));
        ep_send(rd_stream(8'hB0, line_addr(8), 8'd2, 8'd4, 8'h00));
        settle(300);
        chk(ep_n == 16, "both runs were served in full", ep_n);
        bad = 0;
        for (i = 0; i < 8 && i < ep_n; i = i + 1) begin
            if (
                (f_tag(ep_f[i]) !== (8'hA0 + i/4))
                || (f_pay(ep_f[i]) !== line_pay(i))
            ) begin
                bad = bad + 1;
            end
        end
        for (i = 8; i < 16 && i < ep_n; i = i + 1) begin
            if (
                (f_tag(ep_f[i]) !== (8'hB0 + (i-8)/4))
                || (f_pay(ep_f[i]) !== line_pay(i))
            ) begin
                bad = bad + 1;
            end
        end
        chk(bad == 0, "each run kept its own tags and lines", bad);

        // ============================================ 11. the counters, and off
        $display("--- 11. counters, then disable, then busy ---");
        clear_capture;
        rt_send(ctrl(8'hC0, 8'd0, CB + 8'd3, 64'd0));
        settle(20);
        cv = (ru_n > 0) ? f_cvl(ru_f[0]) : 64'd0;
        chk(cv[63:32] > 32'd0, "the served counter moved", 0);
        chk(cv[31:0]  > 32'd0,
            "and so did the in-window-but-forwarded counter", 0);

        // Disable puts it back to a wire, which is what a project that does not
        // want the slot gets without removing the instance.
        clear_capture;
        rt_send(ctrl(8'hC1, 8'd1, CB + 8'd2, 64'd0));
        settle(20);
        clear_capture;
        ep_send(rd_stream(8'hC2, line_addr(0), 8'd1, 8'd4, 8'h00));
        settle(60);
        chk(ru_n == 1, "disabled, an in-window read is forwarded", ru_n);
        chk(ep_n == 0, "and nothing is served", ep_n);

        settle(50);
        chk(rt_busy === 1'b0, "the router face is released", 0);
        chk(eu_busy === 1'b0, "the endpoint face is released", 0);
        chk(stuck == 0, "and neither was ever held for its own reasons", stuck);
        chk(ep_valid === 1'b0, "no response is left stranded", 0);
        chk(ru_valid === 1'b0, "nothing is left half-forwarded", 0);

        // ============================================ bypass
        chk(byp_bad == 0, "PASS=1 was the identity on every cycle", byp_bad);
        chk(byp_seen > 0, "and really did carry the traffic", byp_seen);

        $display("========================================");
        if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $display("========================================");
        $finish;
    end

    initial begin
        #400000;
        $display("  FAIL -- watchdog (ep_n=%0d ru_n=%0d eu_busy=%b rt_busy=%b)",
                 ep_n, ru_n, eu_busy, rt_busy);
        $finish;
    end

endmodule

`default_nettype wire

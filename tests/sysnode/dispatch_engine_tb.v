// dispatch_engine: randomised entry streams against a reference model.
//
// Checked every cycle: writes leave in queue order with their data; no write
// while the controller holds the port; no mailbox commit while the previous one
// is still offered (a lost flit); no mover-window write while the mover's
// config queue has no room; no interlink write while a move runs or is queued;
// a write behind a WAIT only once the model's counter has reached the WAIT; the
// claim matches (mapped source, retirement code, enabled); the counters read
// back equal the model's; overflow and clear behave.
//
// Negative builds must FAIL: +define+NEG_GO hides the offered flit, NEG_MV
// hides the running move, NEG_ROOM claims room always, NEG_WAIT makes the
// engine count more than the model.

`timescale 1ns/1ps

module dispatch_engine_tb;
    localparam integer DEPTH = 8;
    localparam integer NCTR  = 16;
    localparam integer CTR_W = 12;
    localparam integer N     = 3000;

    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg resetn = 1'b0;

    reg        enq_en = 1'b0;
    reg [5:0]  enq_code = 6'd0;
    reg [63:0] enq_data = 64'd0;
    reg        cfg_en = 1'b0;
    reg [1:0]  cfg_addr = 2'd0;
    reg [63:0] cfg_data = 64'd0;
    reg [4:0]  rd_addr = 5'd0;
    wire [63:0] rd_data;
    wire        t_en;
    wire [4:0]  t_code;
    wire [63:0] t_data;
    reg         t_hold = 1'b0;
    reg         sig_v = 1'b0;
    reg [3:0]   sig_x = 4'd0, sig_y = 4'd0;
    reg [7:0]   sig_code = 8'd0;
    wire        sig_claim;

    // ---- target models ----
    reg         offered = 1'b0, dr_go = 1'b0;
    reg         mv_busy_r = 1'b0, dr_mvgo = 1'b0, dr_mvw = 1'b0;
    reg  [4:0]  mv_left = 5'd0;
    reg  [15:0] mv_done = 16'd0, mv_started = 16'd0, mv_queued = 16'd0;
    reg         room = 1'b1;
`ifdef NEG_ROOM
    wire        room_in = 1'b1;
`else
    wire        room_in = room;
`endif

`ifdef NEG_GO
    wire        offered_in = 1'b0;
`else
    wire        offered_in = offered;
`endif
`ifdef NEG_MV
    wire        busy_in = 1'b0;
    wire [15:0] done_in = mv_queued;
`else
    wire        busy_in = mv_busy_r || dr_mvgo || (mv_queued != mv_started);
    wire [15:0] done_in = mv_done;
`endif

    dispatch_engine #(.DEPTH(DEPTH), .NCTR(NCTR), .CTR_W(CTR_W)) dut (
        .clk(clk), .resetn(resetn),
        .enq_en(enq_en), .enq_code(enq_code), .enq_data(enq_data),
        .cfg_en(cfg_en), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
        .rd_addr(rd_addr), .rd_data(rd_data),
        .t_en(t_en), .t_code(t_code), .t_data(t_data), .t_hold(t_hold),
        .tx_offered(offered_in), .mv_busy(busy_in), .mv_done(done_in),
        .mv_room(room_in),
        .sig_v(sig_v), .sig_x(sig_x), .sig_y(sig_y), .sig_code(sig_code),
        .sig_claim(sig_claim)
    );

    integer errors = 0;
    task fail(input [8*48-1:0] what);
        begin
            errors = errors + 1;
            if (errors <= 10) $display("ERROR @%0t %0s", $time, what);
        end
    endtask

    // ---- the scoreboard: every entry in queue order ----
    reg  [5:0]  sb_code [0:N+15];
    reg  [63:0] sb_data [0:N+15];
    integer     sb_n = 0, sb_i = 0;
    integer     cnt_m [0:NCTR-1];
    integer     mv_base_m = 0;
    reg         en_m = 1'b0, map_on = 1'b0;
    integer     k;

    // A WAIT is met when the model's counter has reached it.
    function automatic met(input [63:0] d);
        integer s, cur;
        begin
            s = d[60:56];
            cur = (s < NCTR) ? cnt_m[s] : (mv_done - mv_base_m);
            met = ((cur - d[CTR_W-1:0]) & ((1 << CTR_W) - 1)) < (1 << (CTR_W - 1));
        end
    endfunction

    wire sig_mapped = (sig_y < 4);
    wire claim_m = en_m && map_on && sig_mapped && (sig_code == 8'h00 || sig_code == 8'h01);
    integer neg_tick = 0;

    always @(posedge clk) if (resetn) begin
        // writes in order, behind their waits
        if (t_en) begin
            if (t_hold) fail("write while the controller holds the port");
            while (sb_i < sb_n && sb_code[sb_i] == 6'd32) begin
                if (!met(sb_data[sb_i])) fail("write issued before its WAIT was met");
                sb_i = sb_i + 1;
            end
            if (sb_i >= sb_n) fail("write with nothing queued");
            else begin
                if (sb_code[sb_i][4:0] != t_code || sb_data[sb_i] != t_data)
                    fail("write out of order or with the wrong data");
                sb_i = sb_i + 1;
            end
            if (t_code >= 5'd8 && t_code < 5'd24 && !room)
                fail("mover-window write with no queue room");
            if (t_code >= 5'd24 && (mv_busy_r || dr_mvgo || mv_queued != mv_done))
                fail("interlink write while a move runs or is queued");
        end
        // The mover's config queue: room comes and goes.
        room <= ($urandom % 5 != 0);
        if (sig_v && (sig_claim !== claim_m)) fail("claim differs from the model");
        if (sig_v && claim_m) begin
`ifdef NEG_WAIT
            neg_tick = neg_tick + 1;
            if (neg_tick[0]) cnt_m[{sig_y[1:0], sig_x[1:0]}] = cnt_m[{sig_y[1:0], sig_x[1:0]}] + 1;
`else
            cnt_m[{sig_y[1:0], sig_x[1:0]}] = cnt_m[{sig_y[1:0], sig_x[1:0]}] + 1;
`endif
        end

        // the mailbox: a commit reaches it a cycle after issue
        dr_go <= t_en && t_code == 5'd5;
        if (offered && ($urandom % 3 == 0)) offered <= 1'b0;
        if (dr_go) begin
            if (offered) fail("mailbox commit while a flit is offered: lost");
            else offered <= 1'b1;
        end

        // the mover: a GO reaches its queue a cycle after issue; queued moves
        // run one after another, 2..17 cycles each
        dr_mvgo <= t_en && t_code == 5'd8 && t_data[16];
        if (dr_mvgo) mv_queued <= mv_queued + 1'b1;
        if (!mv_busy_r && (mv_queued != mv_started)) begin
            mv_busy_r  <= 1'b1;
            mv_left    <= 5'd2 + ($urandom % 16);
            mv_started <= mv_started + 1'b1;
        end
        else if (mv_busy_r) begin
            if (mv_left == 5'd0) begin
                mv_busy_r <= 1'b0;
                mv_done   <= mv_done + 1'b1;
            end
            else mv_left <= mv_left - 1'b1;
        end
    end

    // ---- stimulus ----
    integer gos_enq = 0, cyc = 0, u, want, i;
    reg [63:0] d;
    reg [5:0]  c;

    task cfg(input [1:0] a, input [63:0] v);
        begin
            @(negedge clk);
            cfg_en = 1'b1; cfg_addr = a; cfg_data = v;
            @(negedge clk);
            cfg_en = 1'b0;
        end
    endtask

    task push(input [5:0] pc, input [63:0] pd);
        begin
            sb_code[sb_n] = pc; sb_data[sb_n] = pd; sb_n = sb_n + 1;
            enq_en = 1'b1; enq_code = pc; enq_data = pd;
            @(negedge clk);
            enq_en = 1'b0;
        end
    endtask

    initial begin
        for (k = 0; k < NCTR; k = k + 1) cnt_m[k] = 0;
        repeat (4) @(negedge clk);
        resetn = 1'b1;
        @(negedge clk);
        if (rd_data[63:48] != 16'hDE01) fail("STAT magic");

        for (k = 0; k < NCTR; k = k + 1)
            cfg(2'd2, {32'd0, 1'b1, 11'd0, k[3:0], 4'd0, 2'd0, k[3:2], 6'd0, k[1:0]});
        map_on = 1'b1;

        // overflow: a disabled engine keeps DEPTH + 1, the next push is lost
        cfg(2'd1, 64'd2);
        repeat (NCTR + 2) @(negedge clk);
        for (i = 0; i < DEPTH + 3; i = i + 1) begin
            enq_en = 1'b1; enq_code = 6'd1; enq_data = i;
            @(negedge clk);
        end
        enq_en = 1'b0;
        @(negedge clk);
        if (rd_data[15:0] != DEPTH + 1) fail("used after filling");
        if (!rd_data[35]) fail("overflow flag");
        cfg(2'd1, 64'd2);
        repeat (NCTR + 2) @(negedge clk);
        if (rd_data[15:0] != 0 || rd_data[35]) fail("clear");

        cfg(2'd1, 64'd3);
        en_m = 1'b1;
        mv_base_m = mv_done;
        repeat (NCTR + 2) @(negedge clk);

        fork
            begin : feed
                for (i = 0; i < N; i = i + 1) begin
                    while (rd_data[15:0] >= DEPTH + 1) @(negedge clk);
                    if ($urandom % 10 < 3) begin
                        if ($urandom % 5 == 0) begin
                            want = gos_enq - ($urandom % 2);
                            if (want < 0) want = 0;
                            d = {3'd0, 5'd16, 56'd0} | (want & 12'hfff);
                        end
                        else begin
                            u = $urandom % NCTR;
                            want = cnt_m[u] + ($urandom % 24);
                            d = {3'd0, u[4:0], 56'd0} | (want & 12'hfff);
                        end
                        push(6'd32, d);
                    end
                    else begin
                        case ($urandom % 10)
                            0, 1:    c = 6'd5;
                            2, 3:    c = 6'd8;
                            default: c = $urandom % 32;
                        endcase
                        d = {$urandom, $urandom};
                        if (c == 6'd8) begin
                            d[16] = ($urandom % 3 == 0);
                            if (d[16]) gos_enq = gos_enq + 1;
                        end
                        push(c, d);
                    end
                    if ($urandom % 4 == 0) @(negedge clk);
                end
                push(6'd1, 64'hFEED);
            end
            begin : signals
                while (sb_i < sb_n || sb_n < N) begin
                    @(negedge clk);
                    sig_v = ($urandom % 2 == 0);
                    sig_x = $urandom % 4;
                    sig_y = ($urandom % 8 == 0) ? 4'd5 : ($urandom % 4);
                    case ($urandom % 6)
                        0, 1, 2: sig_code = 8'h00;
                        3:       sig_code = 8'h01;
                        4:       sig_code = 8'h03;
                        default: sig_code = 8'h04;
                    endcase
                    t_hold = ($urandom % 5 == 0);
                end
                sig_v = 1'b0; t_hold = 1'b0;
            end
            begin : watchdog
                while (cyc < 400000 && (sb_i < sb_n || sb_n < N)) begin
                    @(negedge clk);
                    cyc = cyc + 1;
                end
                if (cyc >= 400000) begin
                    fail("hang: entries left unissued");
                    $display("@@@ issued %0d of %0d entries, STAT %h", sb_i, sb_n, rd_data);
                    $display("FAIL dispatch_engine: %0d errors", errors);
                    $finish;
                end
            end
        join

        repeat (30) @(negedge clk);
        if (rd_data[15:0] != 0) fail("queue not empty at the end");
        for (k = 0; k < NCTR; k = k + 1) begin
            rd_addr = 5'd16 + k;
            #1;
`ifndef NEG_WAIT
            if (rd_data[CTR_W-1:0] != (cnt_m[k] & 12'hfff)) fail("counter read back");
`endif
        end
        rd_addr = 5'd1;
        #1;
        if (rd_data[32 +: CTR_W] != (gos_enq & 12'hfff)) fail("moves started");
        rd_addr = 5'd0;
        cfg(2'd1, 64'd3);
        repeat (NCTR + 2) @(negedge clk);
        rd_addr = 5'd16;
        #1;
        if (rd_data != 0) fail("counter after clear");

        $display("@@@ entries %0d, cycles %0d, moves %0d, counts u0 %0d", sb_n, cyc, gos_enq, cnt_m[0]);
        if (errors == 0) $display("PASS dispatch_engine");
        else $display("FAIL dispatch_engine: %0d errors", errors);
        $finish;
    end
endmodule

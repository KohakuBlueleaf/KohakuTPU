// interlink_4mesh_tb -- the 2x2 grid, under load designed to wedge it.
//
// Functional tests do not find deadlocks; mag_switch_tb already proves every
// route works once. This one is adversarial, and the difference is that it
// asserts FORWARD PROGRESS rather than absence of error:
//
//   * all-to-all with no idle gaps, long enough that every buffer fills and
//     every credit is exhausted at least once;
//   * asymmetric load -- one mesh flooded while another is idle, which is the
//     case where a shared buffer starves a victim doing nothing wrong;
//   * receivers that stall in bursts, so backpressure reaches the senders.
//
// A DEADLOCKED BENCH THAT PASSES BY TIMEOUT IS NOT A PASSING BENCH. The
// progress monitor below fails if delivery ever stops for longer than
// STALL_LIMIT cycles while packets are still owed, and it fails BEFORE the
// watchdog, so a hang is reported as a hang and not as "no verdict".

`timescale 1ns / 1ps
`default_nettype none

module interlink_4mesh_tb;
    localparam integer LW   = 288;
    localparam integer UW   = 96;
    localparam integer RXB  = 64;
    localparam integer MAXB = 32;
    localparam integer STALL_LIMIT = 4000;

    localparam integer U_KIND = 0, U_DMESH = 4, U_SMESH = 6,
                       U_TXN = 8,  U_LEN = 16;
    localparam [3:0] K_MEM_WR = 4'h1;

    reg clk = 0, resetn = 0;
    always #1.666 clk = ~clk;

    integer errors = 0, checks = 0;

    task fail(input [1023:0] why);
        begin
            errors = errors + 1;
            $display("  FAIL: %0s", why);
        end
    endtask

    reg  [UW-1:0] ltx_hdr [0:3];
    reg  [LW-1:0] ltx_dat [0:3];
    reg  [3:0]    ltx_hv, ltx_dv, ltx_dl;
    wire [3:0]    ltx_hr, ltx_dr;

    wire [UW-1:0] lrx_hdr [0:3];
    wire [LW-1:0] lrx_dat [0:3];
    wire [3:0]    lrx_hv, lrx_dv, lrx_dl;
    reg  [3:0]    lrx_hr, lrx_dr;

    wire [LW-1:0] x_td [0:3];
    wire [UW-1:0] x_tu [0:3];
    wire [3:0]    x_tl, x_tv;
    wire [LW-1:0] y_td [0:3];
    wire [UW-1:0] y_tu [0:3];
    wire [3:0]    y_tl, y_tv;

    wire [63:0] c_fwd [0:3];
    wire [3:0]  flt [0:3];
    wire [63:0] c_st0 [0:3];

    reg [3:0] rx_hold;

    genvar g;
    generate
    for (g = 0; g < 4; g = g + 1) begin : m
        mag_switch #(.LINK_W(LW), .TUSER_W(UW), .RX_BEATS(RXB),
                     .MAX_BEATS(MAXB)) u (
            .clk(clk), .resetn(resetn), .my_mesh(g[1:0]),
            .ltx_hdr(ltx_hdr[g]), .ltx_hvalid(ltx_hv[g]), .ltx_hready(ltx_hr[g]),
            .ltx_dat(ltx_dat[g]), .ltx_dlast(ltx_dl[g]), .ltx_dvalid(ltx_dv[g]),
            .ltx_dready(ltx_dr[g]),
            .lrx_hdr(lrx_hdr[g]), .lrx_hvalid(lrx_hv[g]), .lrx_hready(lrx_hr[g]),
            .lrx_dat(lrx_dat[g]), .lrx_dlast(lrx_dl[g]), .lrx_dvalid(lrx_dv[g]),
            .lrx_dready(lrx_dr[g]),
            .m0_tdata(x_td[g]), .m0_tuser(x_tu[g]), .m0_tlast(x_tl[g]),
            .m0_tvalid(x_tv[g]), .m0_tready(1'b1),
            .s0_tdata(x_td[g^1]), .s0_tuser(x_tu[g^1]), .s0_tlast(x_tl[g^1]),
            .s0_tvalid(x_tv[g^1]), .s0_tready(),
            .m1_tdata(y_td[g]), .m1_tuser(y_tu[g]), .m1_tlast(y_tl[g]),
            .m1_tvalid(y_tv[g]), .m1_tready(1'b1),
            .s1_tdata(y_td[g^2]), .s1_tuser(y_tu[g^2]), .s1_tlast(y_tl[g^2]),
            .s1_tvalid(y_tv[g^2]), .s1_tready(),
            .ctr_tx0(), .ctr_rx0(), .ctr_stall0(c_st0[g]),
            .ctr_tx1(), .ctr_rx1(), .ctr_stall1(),
            .ctr_fwd(c_fwd[g]), .ctr_lblock(),
            .cred0_state(), .cred1_state(), .fault(flt[g])
        );
    end
    endgenerate

    // ---- accounting -------------------------------------------------------
    integer sent [0:3];
    integer got  [0:3];
    integer bad  [0:3];

    function [LW-1:0] patt(input [31:0] s, input [15:0] i);
        integer k;
        begin
            for (k = 0; k < LW/32; k = k + 1)
                patt[k*32 +: 32] = s * 32'h9E37 + {16'd0, i} * 32'd7 + k[31:0];
        end
    endfunction

    function [UW-1:0] hdr(input [1:0] dm, input [1:0] sm, input [7:0] txn,
                          input [15:0] len);
        begin
            hdr = {UW{1'b0}};
            hdr[U_KIND  +: 4]  = K_MEM_WR;
            hdr[U_DMESH +: 2]  = dm;
            hdr[U_SMESH +: 2]  = sm;
            hdr[U_TXN   +: 8]  = txn;
            hdr[U_LEN   +: 16] = len;
        end
    endfunction

    task automatic send(input integer src, input [1:0] dm, input [7:0] txn,
                        input [15:0] len);
        integer i;
        begin
            ltx_hdr[src] <= hdr(dm, src[1:0], txn, len);
            ltx_hv[src]  <= 1'b1;
            @(posedge clk);
            while (!ltx_hr[src]) @(posedge clk);
            ltx_hv[src] <= 1'b0;
            for (i = 0; i <= len; i = i + 1) begin
                ltx_dat[src] <= patt({22'd0, src[1:0], txn}, i[15:0]);
                ltx_dl[src]  <= (i == len);
                ltx_dv[src]  <= 1'b1;
                @(posedge clk);
                while (!ltx_dr[src]) @(posedge clk);
            end
            ltx_dv[src] <= 1'b0;
            ltx_dl[src] <= 1'b0;
            sent[src]   = sent[src] + 1;
        end
    endtask

    // Receivers run for the whole test. Payload is checked against the sender's
    // id and txn, so a packet delivered to the wrong mesh is caught by content
    // as well as by count.
    genvar r;
    generate
    for (r = 0; r < 4; r = r + 1) begin : rcv
        integer i;
        reg [7:0]  txn;
        reg [1:0]  sm;
        reg [15:0] ln;
        initial begin
            lrx_hr[r] = 1'b0;
            lrx_dr[r] = 1'b0;
            forever begin
                // BOTH sides of the handshake, not just valid. `lrx_hr` is a
                // register driven from `rx_hold`, so it lags it by a cycle:
                // leaving this wait on `lrx_hv && !rx_hold` exits on a cycle
                // where ready is still low, the header is never popped, and the
                // receiver then reads data for a packet it never accepted.
                // That desync presents as a deadlock in the RTL and is not one.
                lrx_hr[r] <= !rx_hold[r];
                @(posedge clk);
                while (!(lrx_hv[r] && lrx_hr[r]) || !resetn) begin
                    lrx_hr[r] <= !rx_hold[r];
                    @(posedge clk);
                end
                txn = lrx_hdr[r][U_TXN +: 8];
                sm  = lrx_hdr[r][U_SMESH +: 2];
                ln  = lrx_hdr[r][U_LEN +: 16];
                lrx_hr[r] <= 1'b0;
                if (lrx_hdr[r][U_DMESH +: 2] != r[1:0]) bad[r] = bad[r] + 1;
                lrx_dr[r] <= 1'b1;
                for (i = 0; i <= ln; i = i + 1) begin
                    @(posedge clk);
                    while (!lrx_dv[r]) @(posedge clk);
                    if (lrx_dat[r] !== patt({22'd0, sm, txn}, i[15:0]))
                        bad[r] = bad[r] + 1;
                end
                lrx_dr[r] <= 1'b0;
                got[r] = got[r] + 1;
            end
        end
    end
    endgenerate

    // ---- the progress monitor --------------------------------------------
    // Owed packets and no delivery for STALL_LIMIT cycles is a deadlock, and it
    // is reported as one. Without this the bench would sit until the watchdog
    // and report "no verdict", which is the failure mode `cluster_data` had.
    integer last_total = 0, quiet = 0;
    reg     running = 0;
    reg     wedged = 0;

    function integer total_got;
        integer k;
        begin
            total_got = 0;
            for (k = 0; k < 4; k = k + 1) total_got = total_got + got[k];
        end
    endfunction

    function integer total_sent;
        integer k;
        begin
            total_sent = 0;
            for (k = 0; k < 4; k = k + 1) total_sent = total_sent + sent[k];
        end
    endfunction

    // What each switch is holding when it stops. A deadlock is a cycle of
    // waits, so the state that names it is per link: which class is mid-packet,
    // what credit is left, and whether a header is stuck in a holding slot.
    task dump;
        begin
            $display("        mesh | ltx hv/hr dv/dr | L0 tst cr0/cr1 q0/q1 ret | L1 tst cr0/cr1 q0/q1 ret");
            $display("        0 | %b%b %b%b | %0d %0d/%0d %b%b %0d/%0d | %0d %0d/%0d %b%b %0d/%0d",
                ltx_hv[0], ltx_hr[0], ltx_dv[0], ltx_dr[0],
                m[0].u.u_l0.tst, m[0].u.u_l0.cred0, m[0].u.u_l0.cred1,
                m[0].u.u_l0.q0_val, m[0].u.u_l0.q1_val,
                m[0].u.u_l0.ret0, m[0].u.u_l0.ret1,
                m[0].u.u_l1.tst, m[0].u.u_l1.cred0, m[0].u.u_l1.cred1,
                m[0].u.u_l1.q0_val, m[0].u.u_l1.q1_val,
                m[0].u.u_l1.ret0, m[0].u.u_l1.ret1);
            $display("        1 | %b%b %b%b | %0d %0d/%0d %b%b %0d/%0d | %0d %0d/%0d %b%b %0d/%0d",
                ltx_hv[1], ltx_hr[1], ltx_dv[1], ltx_dr[1],
                m[1].u.u_l0.tst, m[1].u.u_l0.cred0, m[1].u.u_l0.cred1,
                m[1].u.u_l0.q0_val, m[1].u.u_l0.q1_val,
                m[1].u.u_l0.ret0, m[1].u.u_l0.ret1,
                m[1].u.u_l1.tst, m[1].u.u_l1.cred0, m[1].u.u_l1.cred1,
                m[1].u.u_l1.q0_val, m[1].u.u_l1.q1_val,
                m[1].u.u_l1.ret0, m[1].u.u_l1.ret1);
            $display("        2 | %b%b %b%b | %0d %0d/%0d %b%b %0d/%0d | %0d %0d/%0d %b%b %0d/%0d",
                ltx_hv[2], ltx_hr[2], ltx_dv[2], ltx_dr[2],
                m[2].u.u_l0.tst, m[2].u.u_l0.cred0, m[2].u.u_l0.cred1,
                m[2].u.u_l0.q0_val, m[2].u.u_l0.q1_val,
                m[2].u.u_l0.ret0, m[2].u.u_l0.ret1,
                m[2].u.u_l1.tst, m[2].u.u_l1.cred0, m[2].u.u_l1.cred1,
                m[2].u.u_l1.q0_val, m[2].u.u_l1.q1_val,
                m[2].u.u_l1.ret0, m[2].u.u_l1.ret1);
            $display("        3 | %b%b %b%b | %0d %0d/%0d %b%b %0d/%0d | %0d %0d/%0d %b%b %0d/%0d",
                ltx_hv[3], ltx_hr[3], ltx_dv[3], ltx_dr[3],
                m[3].u.u_l0.tst, m[3].u.u_l0.cred0, m[3].u.u_l0.cred1,
                m[3].u.u_l0.q0_val, m[3].u.u_l0.q1_val,
                m[3].u.u_l0.ret0, m[3].u.u_l0.ret1,
                m[3].u.u_l1.tst, m[3].u.u_l1.cred0, m[3].u.u_l1.cred1,
                m[3].u.u_l1.q0_val, m[3].u.u_l1.q1_val,
                m[3].u.u_l1.ret0, m[3].u.u_l1.ret1);
            $display("        mesh0 ingress: lmux busy=%b sel=%b | lrx hv=%b hr=%b dv=%b dr=%b dl=%b | rx_hold=%b",
                m[0].u.u_lmux.busy, m[0].u.u_lmux.sel,
                lrx_hv[0], lrx_hr[0], lrx_dv[0], lrx_dr[0], lrx_dl[0], rx_hold[0]);
            $display("        mesh0 L0 rx0 hempty=%b dempty=%b | L1 rx0 hempty=%b dempty=%b",
                m[0].u.u_l0.rx0_hempty, m[0].u.u_l0.rx0_dempty,
                m[0].u.u_l1.rx0_hempty, m[0].u.u_l1.rx0_dempty);
            $display("        demux busy/sel: %b%0d %b%0d %b%0d %b%0d   ymux busy/sel: %b%b %b%b %b%b %b%b",
                m[0].u.u_ldm.busy, m[0].u.u_ldm.sel_r,
                m[1].u.u_ldm.busy, m[1].u.u_ldm.sel_r,
                m[2].u.u_ldm.busy, m[2].u.u_ldm.sel_r,
                m[3].u.u_ldm.busy, m[3].u.u_ldm.sel_r,
                m[0].u.u_ymux.busy, m[0].u.u_ymux.sel,
                m[1].u.u_ymux.busy, m[1].u.u_ymux.sel,
                m[2].u.u_ymux.busy, m[2].u.u_ymux.sel,
                m[3].u.u_ymux.busy, m[3].u.u_ymux.sel);
        end
    endtask

    always @(posedge clk) if (resetn && running && !wedged) begin
        if (total_got != last_total) begin
            last_total <= total_got;
            quiet <= 0;
        end else if (total_got < total_sent) begin
            quiet <= quiet + 1;
            if (quiet == STALL_LIMIT) begin
                wedged <= 1'b1;
                $display("  FAIL DEADLOCK: %0d packets delivered of %0d sent, and nothing has moved for %0d cycles",
                         total_got, total_sent, STALL_LIMIT);
                errors = errors + 1;
                dump;
            end
        end
    end

    // Bursty backpressure on every receiver, so buffers fill rather than being
    // drained as fast as they are written.
    integer hb;
    initial begin
        rx_hold = 4'd0;
        forever begin
            repeat (60) @(posedge clk);
            for (hb = 0; hb < 4; hb = hb + 1) rx_hold[hb] = ($random & 3) == 0;
            repeat (40) @(posedge clk);
            rx_hold = 4'd0;
        end
    end

    integer n, s, d, k, rounds;

    initial begin
        for (k = 0; k < 4; k = k + 1) begin
            sent[k] = 0; got[k] = 0; bad[k] = 0;
            ltx_hdr[k] = {UW{1'b0}}; ltx_dat[k] = {LW{1'b0}};
        end
        ltx_hv = 4'd0; ltx_dv = 4'd0; ltx_dl = 4'd0;
        repeat (8) @(posedge clk);
        resetn <= 1'b1;
        repeat (4) @(posedge clk);
        running <= 1'b1;

        $display("=== interlink, four meshes, adversarial ===");

        // ---- 1. all-to-all, sustained, no gaps ------------------------
        // Every source drives its own process, so the four never take turns
        // and the arbiters see genuine simultaneous demand.
        fork
            begin : q0
                integer i, t;
                for (i = 0; i < 24; i = i + 1) begin
                    t = (i % 3) + 1;
                    send(0, t[1:0], i[7:0], (i % 4) * 8);
                end
            end
            begin : q1
                integer i, t;
                for (i = 0; i < 24; i = i + 1) begin
                    t = (i % 3);
                    if (t >= 1) t = t + 1;
                    send(1, t[1:0], 8'd64 + i[7:0], (i % 4) * 8);
                end
            end
            begin : q2
                integer i, t;
                for (i = 0; i < 24; i = i + 1) begin
                    t = (i % 3);
                    if (t >= 2) t = t + 1;
                    send(2, t[1:0], 8'd128 + i[7:0], (i % 4) * 8);
                end
            end
            begin : q3
                integer i;
                for (i = 0; i < 24; i = i + 1)
                    send(3, (i % 3), 8'd192 + i[7:0], (i % 4) * 8);
            end
        join

        // ---- 2. asymmetric: mesh 0 floods mesh 3 through mesh 1 --------
        // The diagonal is the only forwarding route, so this is mesh 1 doing
        // nothing but someone else's work while its own traffic must continue.
        fork
            begin : flood
                integer i;
                for (i = 0; i < 40; i = i + 1)
                    send(0, 2'd3, 8'd200 + i[7:0], MAXB[15:0] - 16'd1);
            end
            begin : victim
                integer i;
                repeat (100) @(posedge clk);
                for (i = 0; i < 8; i = i + 1) send(1, 2'd0, 8'd240 + i[7:0], 16'd3);
            end
        join

        rounds = 0;
        while ((total_got < total_sent) && (rounds < 200) && !wedged) begin
            repeat (100) @(posedge clk);
            rounds = rounds + 1;
        end
        running <= 1'b0;

        checks = checks + 1;
        if (total_got != total_sent) begin
            fail("packets were lost");
            $display("        sent %0d, delivered %0d", total_sent, total_got);
        end

        for (k = 0; k < 4; k = k + 1) begin
            checks = checks + 1;
            if (bad[k] != 0) begin
                fail("a packet arrived at the wrong mesh or with the wrong payload");
                $display("        mesh %0d, %0d bad", k, bad[k]);
            end
            checks = checks + 1;
            if (flt[k] != 4'd0) begin
                fail("a switch raised a fault under load");
                $display("        mesh %0d fault %b", k, flt[k]);
            end
        end

        checks = checks + 1;
        if (c_fwd[1][31:0] == 32'd0)
            fail("mesh 1 never forwarded, so the diagonals never used the second routing layer");

        // Without contention this test proves only that four idle switches do
        // not deadlock. The forward path having been blocked is the evidence
        // that the buffers were actually loaded.
        checks = checks + 1;
        if (c_fwd[1][63:32] == 32'd0)
            fail("the forward path was never once blocked, so nothing here was under load and the absence of a deadlock means nothing");

        $display("  delivered %0d packets; mesh1 forwarded %0d, blocked %0d cycles",
                 total_got, c_fwd[1][31:0], c_fwd[1][63:32]);
        $display("  link0 credit-stalled: %0d %0d %0d %0d cycles",
                 c_st0[0][31:0], c_st0[1][31:0], c_st0[2][31:0], c_st0[3][31:0]);

        $display("--- %0d checks, %0d errors", checks, errors);
        if (errors == 0) $display("PASS interlink_4mesh");
        else             $display("FAIL interlink_4mesh");
        $finish;
    end

    initial begin
        #20_000_000;
        $display("WATCHDOG interlink_4mesh_tb -- the progress monitor should have fired first.");
        $display("FAIL interlink_4mesh");
        $finish;
    end
endmodule

`default_nettype wire

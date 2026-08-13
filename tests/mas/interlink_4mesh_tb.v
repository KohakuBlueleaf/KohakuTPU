// interlink_4mesh_tb -- the SLR chain 0-1-3-2, under load designed to wedge it.
//
// Functional tests do not find deadlocks; mag_switch_tb already proves every
// route works once. This one is adversarial, and the difference is that it
// asserts FORWARD PROGRESS rather than absence of error:
//
//   * all-to-all with no idle gaps, long enough that every buffer fills and
//     every credit is exhausted at least once;
//   * SATURATING BIDIRECTIONAL TRANSIT -- mesh0->mesh2 and mesh2->mesh0 flat
//     out, three hops each and crossing on every link, while mesh1 and mesh3
//     inject their own traffic into the same links. This is the case that
//     deadlocks if transit and local arbitration is wrong, and the case the
//     old 2x2 grid never had because no route there was longer than two hops;
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
    localparam [3:0] K_MEM_WR = 4'h1, K_NOC_FLIT = 4'h2, K_DOORBELL = 4'h3;

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

    wire [LW-1:0] dn_td [0:3];
    wire [UW-1:0] dn_tu [0:3];
    wire [3:0]    dn_tl, dn_tv;
    wire [LW-1:0] up_td [0:3];
    wire [UW-1:0] up_tu [0:3];
    wire [3:0]    up_tl, up_tv;

    wire [63:0] c_fwd [0:3];
    wire [3:0]  flt [0:3];
    wire [63:0] c_st0 [0:3];

    reg [3:0] rx_hold;

    // The chain, and the only place the SLR order appears in this bench.
    function integer cpos(input integer m);
        cpos = (m == 0) ? 0 : (m == 1) ? 1 : (m == 3) ? 2 : 3;
    endfunction
    function integer cat(input integer p);
        cat = (p == 0) ? 0 : (p == 1) ? 1 : (p == 2) ? 3 : 2;
    endfunction

    genvar g;
    generate
    for (g = 0; g < 4; g = g + 1) begin : m
        localparam integer P    = cpos(g);
        localparam integer HAS0 = (P != 0) ? 1 : 0;
        localparam integer HAS1 = (P != 3) ? 1 : 0;
        localparam integer DN   = (P != 0) ? cat(P - 1) : 0;
        localparam integer UP   = (P != 3) ? cat(P + 1) : 0;

        mag_switch #(.LINK_W(LW), .TUSER_W(UW), .RX_BEATS(RXB),
                     .MAX_BEATS(MAXB)) u (
            .clk(clk), .resetn(resetn), .my_mesh(g[1:0]),
            .ltx_hdr(ltx_hdr[g]), .ltx_hvalid(ltx_hv[g]), .ltx_hready(ltx_hr[g]),
            .ltx_dat(ltx_dat[g]), .ltx_dlast(ltx_dl[g]), .ltx_dvalid(ltx_dv[g]),
            .ltx_dready(ltx_dr[g]),
            .lrx_hdr(lrx_hdr[g]), .lrx_hvalid(lrx_hv[g]), .lrx_hready(lrx_hr[g]),
            .lrx_dat(lrx_dat[g]), .lrx_dlast(lrx_dl[g]), .lrx_dvalid(lrx_dv[g]),
            .lrx_dready(lrx_dr[g]),
            .m0_tdata(dn_td[g]), .m0_tuser(dn_tu[g]), .m0_tlast(dn_tl[g]),
            .m0_tvalid(dn_tv[g]), .m0_tready(1'b1),
            .s0_tdata(HAS0 ? up_td[DN] : {LW{1'b0}}),
            .s0_tuser(HAS0 ? up_tu[DN] : {UW{1'b0}}),
            .s0_tlast(HAS0 ? up_tl[DN] : 1'b0),
            .s0_tvalid(HAS0 ? up_tv[DN] : 1'b0), .s0_tready(),
            .m1_tdata(up_td[g]), .m1_tuser(up_tu[g]), .m1_tlast(up_tl[g]),
            .m1_tvalid(up_tv[g]), .m1_tready(1'b1),
            .s1_tdata(HAS1 ? dn_td[UP] : {LW{1'b0}}),
            .s1_tuser(HAS1 ? dn_tu[UP] : {UW{1'b0}}),
            .s1_tlast(HAS1 ? dn_tl[UP] : 1'b0),
            .s1_tvalid(HAS1 ? dn_tv[UP] : 1'b0), .s1_tready(),
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
    integer ord_n [0:3];

    // Per-txn, for the ordering and kind checks. Only the phases that need them
    // reset them; the load phases just count.
    localparam integer NTXN = 256;
    integer land_ord  [0:NTXN-1];
    integer land_kind [0:NTXN-1];

    function [LW-1:0] patt(input [31:0] s, input [15:0] i);
        integer k;
        begin
            for (k = 0; k < LW/32; k = k + 1)
                patt[k*32 +: 32] = s * 32'h9E37 + {16'd0, i} * 32'd7 + k[31:0];
        end
    endfunction

    function [UW-1:0] hdr(input [3:0] knd, input [1:0] dm, input [1:0] sm,
                          input [7:0] txn, input [15:0] len);
        begin
            hdr = {UW{1'b0}};
            hdr[U_KIND  +: 4]  = knd;
            hdr[U_DMESH +: 2]  = dm;
            hdr[U_SMESH +: 2]  = sm;
            hdr[U_TXN   +: 8]  = txn;
            hdr[U_LEN   +: 16] = len;
        end
    endfunction

    task automatic sendk(input integer src, input [3:0] knd, input [1:0] dm,
                         input [7:0] txn, input [15:0] len);
        integer i;
        begin
            ltx_hdr[src] <= hdr(knd, dm, src[1:0], txn, len);
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

    task automatic send(input integer src, input [1:0] dm, input [7:0] txn,
                        input [15:0] len);
        begin
            sendk(src, K_MEM_WR, dm, txn, len);
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
        reg [3:0]  kn;
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
                kn  = lrx_hdr[r][U_KIND +: 4];
                lrx_hr[r] <= 1'b0;
                if (lrx_hdr[r][U_DMESH +: 2] != r[1:0]) bad[r] = bad[r] + 1;
                land_ord[txn]  = ord_n[r];
                land_kind[txn] = kn;
                ord_n[r] = ord_n[r] + 1;
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
            $display("        3 | %b%b %b%b | %0d %0d/%0d %b%b %0d/%0d | %0d %0d/%0d %b%b %0d/%0d",
                ltx_hv[3], ltx_hr[3], ltx_dv[3], ltx_dr[3],
                m[3].u.u_l0.tst, m[3].u.u_l0.cred0, m[3].u.u_l0.cred1,
                m[3].u.u_l0.q0_val, m[3].u.u_l0.q1_val,
                m[3].u.u_l0.ret0, m[3].u.u_l0.ret1,
                m[3].u.u_l1.tst, m[3].u.u_l1.cred0, m[3].u.u_l1.cred1,
                m[3].u.u_l1.q0_val, m[3].u.u_l1.q1_val,
                m[3].u.u_l1.ret0, m[3].u.u_l1.ret1);
            $display("        2 | %b%b %b%b | %0d %0d/%0d %b%b %0d/%0d | %0d %0d/%0d %b%b %0d/%0d",
                ltx_hv[2], ltx_hr[2], ltx_dv[2], ltx_dr[2],
                m[2].u.u_l0.tst, m[2].u.u_l0.cred0, m[2].u.u_l0.cred1,
                m[2].u.u_l0.q0_val, m[2].u.u_l0.q1_val,
                m[2].u.u_l0.ret0, m[2].u.u_l0.ret1,
                m[2].u.u_l1.tst, m[2].u.u_l1.cred0, m[2].u.u_l1.cred1,
                m[2].u.u_l1.q0_val, m[2].u.u_l1.q1_val,
                m[2].u.u_l1.ret0, m[2].u.u_l1.ret1);
            // The transit muxes are where a wrong arbitration wedges: `busy`
            // set with the far side never granting is the signature.
            $display("        ldm busy/sel: %b%0d %b%0d %b%0d %b%0d",
                m[0].u.u_ldm.busy, m[0].u.u_ldm.sel_r,
                m[1].u.u_ldm.busy, m[1].u.u_ldm.sel_r,
                m[3].u.u_ldm.busy, m[3].u.u_ldm.sel_r,
                m[2].u.u_ldm.busy, m[2].u.u_ldm.sel_r);
            $display("        mesh1 t00/t01/t10/t11 busy-sel: %b%b %b%b %b%b %b%b   mesh3: %b%b %b%b %b%b %b%b",
                m[1].u.u_t00.busy, m[1].u.u_t00.sel,
                m[1].u.u_t01.busy, m[1].u.u_t01.sel,
                m[1].u.u_t10.busy, m[1].u.u_t10.sel,
                m[1].u.u_t11.busy, m[1].u.u_t11.sel,
                m[3].u.u_t00.busy, m[3].u.u_t00.sel,
                m[3].u.u_t01.busy, m[3].u.u_t01.sel,
                m[3].u.u_t10.busy, m[3].u.u_t10.sel,
                m[3].u.u_t11.busy, m[3].u.u_t11.sel);
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

    task drain(input integer limit);
        integer rounds;
        begin
            rounds = 0;
            while ((total_got < total_sent) && (rounds < limit) && !wedged) begin
                repeat (100) @(posedge clk);
                rounds = rounds + 1;
            end
        end
    endtask

    integer n, k, ord_db;

    initial begin
        for (k = 0; k < 4; k = k + 1) begin
            sent[k] = 0; got[k] = 0; bad[k] = 0; ord_n[k] = 0;
            ltx_hdr[k] = {UW{1'b0}}; ltx_dat[k] = {LW{1'b0}};
        end
        for (k = 0; k < NTXN; k = k + 1) begin
            land_ord[k] = -1; land_kind[k] = -1;
        end
        ltx_hv = 4'd0; ltx_dv = 4'd0; ltx_dl = 4'd0;
        repeat (8) @(posedge clk);
        resetn <= 1'b1;
        repeat (4) @(posedge clk);
        running <= 1'b1;

        $display("=== interlink, four meshes on the SLR chain, adversarial ===");

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

        drain(200);

        checks = checks + 1;
        if (total_got != total_sent) begin
            fail("packets were lost under all-to-all load");
            $display("        sent %0d, delivered %0d", total_sent, total_got);
        end

        // ---- 2. saturating bidirectional transit ----------------------
        // The two ends stream through the whole chain at once, so mesh1 and
        // mesh3 each carry transit in BOTH directions while injecting their
        // own. Every link is loaded from both sides with no gap.
        fork
            begin : up02
                integer i;
                for (i = 0; i < 48; i = i + 1)
                    send(0, 2'd2, i[7:0], (i % 3) * 15);
            end
            begin : dn20
                integer i;
                for (i = 0; i < 48; i = i + 1)
                    send(2, 2'd0, 8'd128 + i[7:0], (i % 3) * 15);
            end
            begin : mid1
                integer i;
                for (i = 0; i < 32; i = i + 1)
                    send(1, (i[0] ? 2'd2 : 2'd0), 8'd64 + i[7:0], 16'd7);
            end
            begin : mid3
                integer i;
                for (i = 0; i < 32; i = i + 1)
                    send(3, (i[0] ? 2'd0 : 2'd2), 8'd192 + i[7:0], 16'd7);
            end
        join

        drain(400);

        checks = checks + 1;
        if (total_got != total_sent) begin
            fail("packets were lost under saturating bidirectional transit");
            $display("        sent %0d, delivered %0d", total_sent, total_got);
        end

        // Both interior meshes must have forwarded in both directions, or the
        // two streams never actually crossed and the case did not happen.
        checks = checks + 1;
        if (c_fwd[1][31:0] == 32'd0 || c_fwd[3][31:0] == 32'd0)
            fail("an interior mesh never forwarded, so the chain was not crossed");

        // Without contention this proves only that four idle switches do not
        // deadlock. The forward path having been blocked is the evidence that
        // the buffers were actually loaded.
        checks = checks + 1;
        if (c_fwd[1][63:32] == 32'd0 || c_fwd[3][63:32] == 32'd0)
            fail("a forward path was never once blocked, so nothing here was under load and the absence of a deadlock means nothing");

        $display("  delivered %0d packets; forwarded mesh1 %0d (blocked %0d), mesh3 %0d (blocked %0d)",
                 total_got, c_fwd[1][31:0], c_fwd[1][63:32],
                 c_fwd[3][31:0], c_fwd[3][63:32]);

        // ---- 3. the three kinds, three hops, and doorbell ordering -----
        // A DOORBELL means "the data ahead of me has landed", so it must not
        // overtake the data it follows -- across two forwarding hops, where
        // every mux and demux is a fresh chance to reorder.
        for (k = 0; k < NTXN; k = k + 1) begin
            land_ord[k] = -1; land_kind[k] = -1;
        end
        for (k = 0; k < 4; k = k + 1) ord_n[k] = 0;

        sendk(0, K_MEM_WR,   2'd2, 8'd20, 16'd7);
        sendk(0, K_NOC_FLIT, 2'd2, 8'd21, 16'd7);
        sendk(0, K_MEM_WR,   2'd2, 8'd22, 16'd7);
        sendk(0, K_DOORBELL, 2'd2, 8'd23, 16'd0);

        sendk(2, K_MEM_WR,   2'd0, 8'd30, 16'd7);
        sendk(2, K_NOC_FLIT, 2'd0, 8'd31, 16'd7);
        sendk(2, K_MEM_WR,   2'd0, 8'd32, 16'd7);
        sendk(2, K_DOORBELL, 2'd0, 8'd33, 16'd0);

        drain(200);

        checks = checks + 1;
        if (total_got != total_sent) fail("a packet was lost on the three-hop route");

        // The switch routes on dst alone, so a kind that changed in flight
        // means the header was rebuilt somewhere it should have been carried.
        if (land_kind[20] != K_MEM_WR   || land_kind[21] != K_NOC_FLIT ||
            land_kind[22] != K_MEM_WR   || land_kind[23] != K_DOORBELL ||
            land_kind[30] != K_MEM_WR   || land_kind[31] != K_NOC_FLIT ||
            land_kind[32] != K_MEM_WR   || land_kind[33] != K_DOORBELL) begin
            fail("a packet kind did not survive two forwarding hops");
            $display("        0->2 %0d %0d %0d %0d, 2->0 %0d %0d %0d %0d",
                     land_kind[20], land_kind[21], land_kind[22], land_kind[23],
                     land_kind[30], land_kind[31], land_kind[32], land_kind[33]);
        end
        checks = checks + 1;

        checks = checks + 1;
        ord_db = land_ord[23];
        if (ord_db < land_ord[20] || ord_db < land_ord[21] ||
            ord_db < land_ord[22]) begin
            fail("a forwarded doorbell overtook the data it was released by, 0->2");
            $display("        arrival order: data %0d %0d %0d, doorbell %0d",
                     land_ord[20], land_ord[21], land_ord[22], ord_db);
        end

        checks = checks + 1;
        ord_db = land_ord[33];
        if (ord_db < land_ord[30] || ord_db < land_ord[31] ||
            ord_db < land_ord[32]) begin
            fail("a forwarded doorbell overtook the data it was released by, 2->0");
            $display("        arrival order: data %0d %0d %0d, doorbell %0d",
                     land_ord[30], land_ord[31], land_ord[32], ord_db);
        end

        running <= 1'b0;

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

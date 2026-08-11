// mag_switch_tb -- four switches wired as the 2x2 grid, and every route in it.
//
//        mesh0 ──X── mesh1          link0 is X, link1 is Y
//          │Y          │Y
//        mesh2 ──X── mesh3
//
// Twelve ordered pairs. Four are one hop on X, four are one hop on Y, and four
// are the diagonals -- which are the only routes that FORWARD, and therefore the
// only ones that exercise the second routing layer at all. A bench that tested
// neighbours only would pass with the forward path disconnected.
//
// Every packet carries a unique txn and a payload seeded from it, so a packet
// arriving at the wrong mesh is caught by which scoreboard slot it lands in
// rather than by a checksum that happens not to match.

`timescale 1ns / 1ps
`default_nettype none

module mag_switch_tb;
    localparam integer LW   = 288;
    localparam integer UW   = 96;
    localparam integer RXB  = 64;
    localparam integer MAXB = 32;

    localparam integer U_KIND = 0,  U_DMESH = 4,  U_SMESH = 6,
                       U_TXN  = 8,  U_LEN   = 16, U_ADDR  = 32;
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

    // ---- per-mesh ports ---------------------------------------------------
    reg  [UW-1:0] ltx_hdr [0:3];
    reg  [LW-1:0] ltx_dat [0:3];
    reg  [3:0]    ltx_hv, ltx_dv, ltx_dl;
    wire [3:0]    ltx_hr, ltx_dr;

    wire [UW-1:0] lrx_hdr [0:3];
    wire [LW-1:0] lrx_dat [0:3];
    wire [3:0]    lrx_hv, lrx_dv, lrx_dl;
    reg  [3:0]    lrx_hr, lrx_dr;

    // AXIS between the four, one bundle per directed edge.
    wire [LW-1:0] x_td [0:3];
    wire [UW-1:0] x_tu [0:3];
    wire [3:0]    x_tl, x_tv;
    wire [LW-1:0] y_td [0:3];
    wire [UW-1:0] y_tu [0:3];
    wire [3:0]    y_tl, y_tv;

    // X partners: 0<->1, 2<->3.   Y partners: 0<->2, 1<->3.
    function integer xp(input integer m); xp = m ^ 1; endfunction
    function integer yp(input integer m); yp = m ^ 2; endfunction

    wire [63:0] c_fwd [0:3];
    wire [63:0] c_lblk [0:3];
    wire [3:0]  flt [0:3];

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
            .ctr_tx0(), .ctr_rx0(), .ctr_stall0(),
            .ctr_tx1(), .ctr_rx1(), .ctr_stall1(),
            .ctr_fwd(c_fwd[g]), .ctr_lblock(c_lblk[g]),
            .cred0_state(), .cred1_state(), .fault(flt[g])
        );
    end
    endgenerate

    // ---- scoreboard, indexed by txn ---------------------------------------
    localparam integer NTXN = 64;
    integer land_at  [0:NTXN-1];      // which mesh it arrived at, -1 for none
    integer land_bad [0:NTXN-1];      // payload or length mismatch
    integer want_at  [0:NTXN-1];
    integer want_len [0:NTXN-1];

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
            want_at[txn]  = dm;
            want_len[txn] = len;
            ltx_hdr[src] <= hdr(dm, src[1:0], txn, len);
            ltx_hv[src]  <= 1'b1;
            @(posedge clk);
            while (!ltx_hr[src]) @(posedge clk);
            ltx_hv[src] <= 1'b0;
            for (i = 0; i <= len; i = i + 1) begin
                ltx_dat[src] <= patt({24'd0, txn}, i[15:0]);
                ltx_dl[src]  <= (i == len);
                ltx_dv[src]  <= 1'b1;
                @(posedge clk);
                while (!ltx_dr[src]) @(posedge clk);
            end
            ltx_dv[src] <= 1'b0;
            ltx_dl[src] <= 1'b0;
        end
    endtask

    // One receiver process per mesh, always running: a packet that arrives
    // where it should not is then caught by arriving at all.
    // Held meshes stop consuming, which is how the forward path is made to back
    // up without touching the RTL.
    reg [3:0] rx_hold;

    genvar r;
    generate
    for (r = 0; r < 4; r = r + 1) begin : rcv
        integer i;
        reg [7:0]  txn;
        reg [15:0] ln;
        initial begin
            lrx_hr[r] = 1'b0;
            lrx_dr[r] = 1'b0;
            forever begin
                // Both sides of the handshake: `lrx_hr` lags `rx_hold` by a
                // cycle, so waiting on valid alone exits where ready is still
                // low and the header is never popped.
                lrx_hr[r] <= !rx_hold[r];
                @(posedge clk);
                while (!(lrx_hv[r] && lrx_hr[r]) || !resetn) begin
                    lrx_hr[r] <= !rx_hold[r];
                    @(posedge clk);
                end
                txn = lrx_hdr[r][U_TXN +: 8];
                ln  = lrx_hdr[r][U_LEN +: 16];
                lrx_hr[r] <= 1'b0;
                land_at[txn] = r;
                if (ln != want_len[txn]) land_bad[txn] = 1;
                lrx_dr[r] <= 1'b1;
                for (i = 0; i <= ln; i = i + 1) begin
                    @(posedge clk);
                    while (!lrx_dv[r]) @(posedge clk);
                    if (lrx_dat[r] !== patt({24'd0, txn}, i[15:0]))
                        land_bad[txn] = 1;
                    if (lrx_dl[r] !== (i == ln)) land_bad[txn] = 1;
                end
                lrx_dr[r] <= 1'b0;
            end
        end
    end
    endgenerate

    integer n, s, d, txn_n;

    task reset_all;
        integer k;
        begin
            resetn <= 1'b0;
            rx_hold <= 4'd0;
            ltx_hv <= 4'd0; ltx_dv <= 4'd0; ltx_dl <= 4'd0;
            for (k = 0; k < 4; k = k + 1) begin
                ltx_hdr[k] <= {UW{1'b0}}; ltx_dat[k] <= {LW{1'b0}};
            end
            for (k = 0; k < NTXN; k = k + 1) begin
                land_at[k] = -1; land_bad[k] = 0;
                want_at[k] = -1; want_len[k] = 0;
            end
            repeat (8) @(posedge clk);
            resetn <= 1'b1;
            repeat (4) @(posedge clk);
        end
    endtask

    initial begin
        $display("=== mag_switch: the 2x2 grid, every ordered pair ===");
        reset_all;

        // ---- 1. all twelve routes, sequentially so a stall is unambiguous
        txn_n = 1;
        for (s = 0; s < 4; s = s + 1)
            for (d = 0; d < 4; d = d + 1)
                if (s != d) begin
                    send(s, d[1:0], txn_n[7:0], 16'd3);
                    txn_n = txn_n + 1;
                end

        repeat (400) @(posedge clk);

        for (n = 1; n < txn_n; n = n + 1) begin
            checks = checks + 1;
            if (land_at[n] == -1) begin
                fail("a packet never arrived");
                $display("        txn %0d, wanted mesh %0d", n, want_at[n]);
            end else if (land_at[n] != want_at[n]) begin
                fail("a packet arrived at the wrong mesh");
                $display("        txn %0d, wanted %0d, got %0d",
                         n, want_at[n], land_at[n]);
            end else if (land_bad[n]) begin
                fail("payload or framing wrong");
                $display("        txn %0d", n);
            end
        end

        // The diagonals are the only forwarding routes, so the forward counter
        // proves the second routing layer ran rather than being bypassed.
        checks = checks + 1;
        if (c_fwd[1][31:0] == 32'd0 && c_fwd[2][31:0] == 32'd0)
            fail("no packet was ever forwarded -- the diagonals took some other path");

        // ---- 2. all twelve at once, which is where an arbiter livelocks
        reset_all;
        txn_n = 1;
        fork
            begin : s0
                integer k;
                for (k = 1; k <= 3; k = k + 1) send(0, k[1:0], k[7:0], 16'd7);
            end
            begin : s1
                integer k;
                for (k = 0; k <= 3; k = k + 1)
                    if (k != 1) send(1, k[1:0], 8'd16 + k[7:0], 16'd7);
            end
            begin : s2
                integer k;
                for (k = 0; k <= 3; k = k + 1)
                    if (k != 2) send(2, k[1:0], 8'd32 + k[7:0], 16'd7);
            end
            begin : s3
                integer k;
                for (k = 0; k <= 3; k = k + 1)
                    if (k != 3) send(3, k[1:0], 8'd48 + k[7:0], 16'd7);
            end
        join

        repeat (2000) @(posedge clk);
        for (n = 0; n < NTXN; n = n + 1)
            if (want_at[n] != -1) begin
                checks = checks + 1;
                if (land_at[n] != want_at[n] || land_bad[n]) begin
                    fail("a packet was lost or misrouted under all-to-all load");
                    $display("        txn %0d, wanted %0d, got %0d, bad %0d",
                             n, want_at[n], land_at[n], land_bad[n]);
                end
            end

        // ---- 3. a jammed forward path must not stop the rest of the grid.
        // mesh3 stops consuming, so mesh0's diagonal traffic backs up through
        // mesh1's forward queue and then through mesh1's outbound Y link. Every
        // other route must still complete.
        //
        // This proves LIVENESS under the jam. It does not prove that mesh0 can
        // still reach mesh1 while its own diagonal packet is stuck, because
        // mesh0's local egress is one queue and that is head-of-line blocking by
        // design -- measured below rather than asserted away.
        reset_all;
        rx_hold[3] <= 1'b1;
        repeat (4) @(posedge clk);

        fork
            begin : jam
                integer k;
                for (k = 0; k < 6; k = k + 1)
                    send(0, 2'd3, 8'd40 + k[7:0], MAXB[15:0] - 16'd1);
            end
            begin : others
                repeat (200) @(posedge clk);
                send(1, 2'd0, 8'd10, 16'd3);      // one X hop
                send(2, 2'd0, 8'd11, 16'd3);      // one Y hop
                send(2, 2'd1, 8'd12, 16'd3);      // a diagonal that avoids mesh1
            end
        join_any

        repeat (600) @(posedge clk);
        for (n = 10; n <= 12; n = n + 1) begin
            checks = checks + 1;
            if (land_at[n] != want_at[n] || land_bad[n]) begin
                fail("a jammed forward path stopped an unrelated route");
                $display("        txn %0d, wanted %0d, got %0d", n, want_at[n], land_at[n]);
            end
        end

        checks = checks + 1;
        if (c_fwd[1][63:32] == 32'd0)
            fail("the forward path never reported being blocked, so the jam did not happen and the test proved nothing");

        $display("  mesh0 local egress blocked %0d cycles by head-of-line",
                 c_lblk[0][31:0]);

        rx_hold[3] <= 1'b0;
        repeat (2000) @(posedge clk);

        // ---- 4. no fault bit anywhere. F_YTURN in particular means a packet
        //         asked for the turn the deadlock proof forbids.
        for (n = 0; n < 4; n = n + 1) begin
            checks = checks + 1;
            if (flt[n] != 4'd0) begin
                fail("a switch raised a fault");
                $display("        mesh %0d, fault %b", n, flt[n]);
            end
        end

        $display("--- %0d checks, %0d errors", checks, errors);
        if (errors == 0) $display("PASS mag_switch");
        else             $display("FAIL mag_switch");
        $finish;
    end

    initial begin
        #4_000_000;
        $display("WATCHDOG mag_switch_tb -- no verdict. Routing stopped making progress.");
        $display("FAIL mag_switch");
        $finish;
    end
endmodule

`default_nettype wire

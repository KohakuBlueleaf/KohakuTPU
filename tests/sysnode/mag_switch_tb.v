// mag_switch_tb -- four switches wired as the SLR chain, and every route in it.
//
//        mesh0 ── mesh1 ── mesh3 ── mesh2      link1 is the higher neighbour,
//        SLR0     SLR1     SLR2     SLR3       link0 the lower, so every link
//                                              joins one m1 to one s0.
//
// Twelve ordered pairs over four hop counts. Six are one hop, four are two and
// two are three -- and only the multi-hop ones FORWARD, so a bench testing
// neighbours only would pass with the forward path disconnected. mesh0<->mesh2
// is the pair that used to be a link and is now the longest path in the design.
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

    localparam integer U_KIND = 0, U_DMESH = 4, U_SMESH = 6, U_TXN = 8;
    localparam integer U_LEN = 16, U_ADDR = 32;
    localparam [3:0] K_MEM_WR = 4'h1;

    reg clk = 0, resetn = 0;
    always begin
        #1.666 clk = ~clk;
    end

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

    // One surface per directed edge, named by which way it leaves the mesh,
    // plus the credit each mesh issues on the link it receives there.
    wire [LW-1:0] dn_f [0:3], up_f [0:3];
    wire [3:0]    dn_v, dn_vc, dn_l, up_v, up_vc, up_l;
    wire [3:0]    c0_v, c0_vc, c1_v, c1_vc;
    wire [3:0]    c0_n [0:3];
    wire [3:0]    c1_n [0:3];

    // The chain, and the only place the SLR order appears in this bench.
    function integer cpos(input integer m);
        cpos = (m == 0) ? 0 : (m == 1) ? 1 : (m == 3) ? 2 : 3;
    endfunction
    function integer cat(input integer p);
        cat = (p == 0) ? 0 : (p == 1) ? 1 : (p == 2) ? 3 : 2;
    endfunction
    function integer hops(input integer s, input integer d);
        hops = (cpos(s) > cpos(d)) ? (cpos(s) - cpos(d)) : (cpos(d) - cpos(s));
    endfunction

    wire [63:0] c_fwd [0:3];
    wire [63:0] c_lblk [0:3];
    wire [63:0] c_st1 [0:3];
    wire [3:0]  flt [0:3];

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
            .m0_valid(dn_v[g]), .m0_vc(dn_vc[g]), .m0_last(dn_l[g]),
            .m0_flit(dn_f[g]),
            .m0_crd_valid(HAS0 ? c1_v[DN] : 1'b0),
            .m0_crd_vc(HAS0 ? c1_vc[DN] : 1'b0),
            .m0_crd_n(HAS0 ? c1_n[DN] : 4'd0),
            .s0_valid(HAS0 ? up_v[DN] : 1'b0),
            .s0_vc(HAS0 ? up_vc[DN] : 1'b0),
            .s0_last(HAS0 ? up_l[DN] : 1'b0),
            .s0_flit(HAS0 ? up_f[DN] : {LW{1'b0}}),
            .s0_crd_valid(c0_v[g]), .s0_crd_vc(c0_vc[g]), .s0_crd_n(c0_n[g]),
            .m1_valid(up_v[g]), .m1_vc(up_vc[g]), .m1_last(up_l[g]),
            .m1_flit(up_f[g]),
            .m1_crd_valid(HAS1 ? c0_v[UP] : 1'b0),
            .m1_crd_vc(HAS1 ? c0_vc[UP] : 1'b0),
            .m1_crd_n(HAS1 ? c0_n[UP] : 4'd0),
            .s1_valid(HAS1 ? dn_v[UP] : 1'b0),
            .s1_vc(HAS1 ? dn_vc[UP] : 1'b0),
            .s1_last(HAS1 ? dn_l[UP] : 1'b0),
            .s1_flit(HAS1 ? dn_f[UP] : {LW{1'b0}}),
            .s1_crd_valid(c1_v[g]), .s1_crd_vc(c1_vc[g]), .s1_crd_n(c1_n[g]),
            .ctr_tx0(), .ctr_rx0(), .ctr_stall0(),
            .ctr_tx1(), .ctr_rx1(), .ctr_stall1(c_st1[g]),
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
            for (k = 0; k < LW/32; k = k + 1) begin
                patt[k*32 +: 32] = s * 32'h9E37 + {16'd0, i} * 32'd7 + k[31:0];
            end
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
            while (!ltx_hr[src]) begin
                @(posedge clk);
            end
            ltx_hv[src] <= 1'b0;
            for (i = 0; i <= len; i = i + 1) begin
                ltx_dat[src] <= patt({24'd0, txn}, i[15:0]);
                ltx_dl[src]  <= (i == len);
                ltx_dv[src]  <= 1'b1;
                @(posedge clk);
                while (!ltx_dr[src]) begin
                    @(posedge clk);
                end
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
                if (ln != want_len[txn]) begin
                    land_bad[txn] = 1;
                end
                lrx_dr[r] <= 1'b1;
                for (i = 0; i <= ln; i = i + 1) begin
                    @(posedge clk);
                    while (!lrx_dv[r]) begin
                        @(posedge clk);
                    end
                    if (lrx_dat[r] !== patt({24'd0, txn}, i[15:0])) begin
                        land_bad[txn] = 1;
                    end
                    if (lrx_dl[r] !== (i == ln)) begin
                        land_bad[txn] = 1;
                    end
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
        $display("=== mag_switch: the SLR chain 0-1-3-2, every ordered pair ===");
        reset_all;

        // ---- 1. all twelve routes, sequentially so a stall is unambiguous
        txn_n = 1;
        for (s = 0; s < 4; s = s + 1) begin
            for (d = 0; d < 4; d = d + 1) begin
                if (s != d) begin
                    send(s, d[1:0], txn_n[7:0], 16'd3);
                    txn_n = txn_n + 1;
                end
            end
        end

        repeat (600) @(posedge clk);

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

        // Only mesh1 and mesh3 are interior, so they are the only two that can
        // forward at all -- and both must have, or a multi-hop route did not run.
        checks = checks + 1;
        if (c_fwd[1][31:0] == 32'd0) begin
            fail("mesh1 never forwarded, so nothing crossed it");
        end
        checks = checks + 1;
        if (c_fwd[3][31:0] == 32'd0) begin
            fail("mesh3 never forwarded, so nothing crossed it");
        end

        $display("  forwarded: mesh1 %0d, mesh3 %0d packets",
                 c_fwd[1][31:0], c_fwd[3][31:0]);

        // ---- 2. all twelve at once, which is where an arbiter livelocks
        reset_all;
        txn_n = 1;
        fork
            begin : s0
                integer k;
                for (k = 1; k <= 3; k = k + 1) begin
                    send(0, k[1:0], k[7:0], 16'd7);
                end
            end
            begin : s1
                integer k;
                for (k = 0; k <= 3; k = k + 1) begin
                    if (k != 1) begin
                        send(1, k[1:0], 8'd16 + k[7:0], 16'd7);
                    end
                end
            end
            begin : s2
                integer k;
                for (k = 0; k <= 3; k = k + 1) begin
                    if (k != 2) begin
                        send(2, k[1:0], 8'd32 + k[7:0], 16'd7);
                    end
                end
            end
            begin : s3
                integer k;
                for (k = 0; k <= 3; k = k + 1) begin
                    if (k != 3) begin
                        send(3, k[1:0], 8'd48 + k[7:0], 16'd7);
                    end
                end
            end
        join

        repeat (3000) @(posedge clk);
        for (n = 0; n < NTXN; n = n + 1) begin
            if (want_at[n] != -1) begin
                checks = checks + 1;
                if (land_at[n] != want_at[n] || land_bad[n]) begin
                    fail("a packet was lost or misrouted under all-to-all load");
                    $display("        txn %0d, wanted %0d, got %0d, bad %0d",
                             n, want_at[n], land_at[n], land_bad[n]);
                end
            end
        end

        // ---- 3. a jammed forward path must not stop the rest of the chain.
        // mesh2 stops consuming, so mesh0's three-hop traffic backs up through
        // mesh1's and then mesh3's forward queues. Every other route must still
        // complete.
        //
        // This proves LIVENESS under the jam. It does not prove that mesh0 can
        // still reach mesh1 while its own long packet is stuck, because mesh0's
        // local egress is one queue and that is head-of-line blocking by
        // design -- measured below rather than asserted away.
        //
        // 16 packets, not 6: the chain holds a 64-beat class buffer at each of
        // four hops, so 6*31 beats vanish into them and never reach mesh0.
        reset_all;
        rx_hold[2] <= 1'b1;
        repeat (4) @(posedge clk);

        fork
            begin : jam
                integer k;
                for (k = 0; k < 16; k = k + 1) begin
                    send(0, 2'd2, 8'd40 + k[7:0], MAXB[15:0] - 16'd1);
                end
            end
            begin : others
                repeat (200) @(posedge clk);
                send(1, 2'd0, 8'd10, 16'd3);      // one hop down
                send(3, 2'd1, 8'd11, 16'd3);      // one hop down
                send(3, 2'd0, 8'd12, 16'd3);      // two hops, crossing mesh1
            end
        join_any

        repeat (800) @(posedge clk);
        for (n = 10; n <= 12; n = n + 1) begin
            checks = checks + 1;
            if (land_at[n] != want_at[n] || land_bad[n]) begin
                fail("a jammed forward path stopped an unrelated route");
                $display("        txn %0d, wanted %0d, got %0d", n, want_at[n], land_at[n]);
            end
        end

        checks = checks + 1;
        if (c_fwd[1][63:32] == 32'd0) begin
            fail("the forward path never reported being blocked, so the jam did not happen and the test proved nothing");
        end

        // Backpressure crossing three hops back to the injector. It shows up as
        // mesh0's link running out of credit, not as `lblock` -- a starved link
        // stalls the data channel, where ltx_hvalid is already low.
        checks = checks + 1;
        if (c_st1[0][31:0] == 32'd0) begin
            fail("mesh0's link never ran out of credit, so the jam stayed inside the chain's buffers and no source ever felt it");
        end

        $display("  mesh0 link1 credit-stalled %0d cycles; local egress blocked %0d by head-of-line",
                 c_st1[0][31:0], c_lblk[0][31:0]);

        rx_hold[2] <= 1'b0;
        repeat (3000) @(posedge clk);

        // ---- 4. no fault bit anywhere. F_NOFWD in particular means a packet
        //         reached an end of the chain still needing to be forwarded.
        for (n = 0; n < 4; n = n + 1) begin
            checks = checks + 1;
            if (flt[n] != 4'd0) begin
                fail("a switch raised a fault");
                $display("        mesh %0d, fault %b", n, flt[n]);
            end
        end

        $display("--- %0d checks, %0d errors", checks, errors);
        if (errors == 0) begin
            $display("PASS mag_switch");
        end
        else begin
            $display("FAIL mag_switch");
        end
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

// mag_link_tb -- one crossing, both classes, every latency it might be built at.
//
//    A (mesh 0, peer 1) ── pipe(tap) ──► B (mesh 1, peer 0)
//                       ◄── pipe(tap) ──
//
// A packet A sends with dst_mesh=1 stops at B and lands in B's rx0; one with
// dst_mesh=2 is B's to forward and lands in B's rx1. The reverse direction
// carries no data at all, deliberately: it must still carry credit, and a link
// that only returns credit alongside traffic deadlocks as soon as one direction
// goes quiet.
//
// THE LATENCY SWEEP IS THE POINT. The real crossing latency is unknown until
// placement, and a credit depth that is right at one value and silently wrong at
// another is the defect this bench exists to catch, so every test runs at taps
// 1..16 and the verdict is over all of them.
//
// Handshake convention: drive with non-blocking assignments, sample after
// `@(posedge clk)`. In the active region following an edge the DUT's registers
// still hold their pre-edge values, so a combinational ready read there is the
// value the transfer was decided on.

`timescale 1ns / 1ps
`default_nettype none

module mag_link_tb;
    localparam integer LW   = 288;
    localparam integer UW   = 96;
    localparam integer RXB  = 64;
    localparam integer MAXB = 32;

    localparam integer U_KIND = 0, U_DMESH = 4, U_SMESH = 6, U_TXN = 8;
    localparam integer U_LEN = 16, U_ADDR = 32;
    localparam [3:0] K_MEM_WR = 4'h1, K_NOC_FLIT = 4'h2, K_DOORBELL = 4'h3;

    reg clk = 0, resetn = 0;
    always begin
        #1.666 clk = ~clk;
    end

    integer errors = 0;
    integer checks = 0;
    reg [4:0] tap = 5'd1;

    task fail(input [1023:0] why);
        begin
            errors = errors + 1;
            $display("  FAIL (tap %0d): %0s", tap, why);
        end
    endtask

    reg  [UW-1:0] a_h0, a_h1;
    reg  [LW-1:0] a_d0, a_d1;
    reg           a_hv0, a_hv1, a_dv0, a_dv1, a_dl0, a_dl1;
    wire          a_hr0, a_hr1, a_dr0, a_dr1;

    wire [UW-1:0] b_rh0, b_rh1;
    wire [LW-1:0] b_rd0, b_rd1;
    wire          b_rhv0, b_rhv1, b_rdv0, b_rdv1, b_rdl0, b_rdl1;
    reg           b_rhr0, b_rhr1, b_rdr0, b_rdr1;

    wire [LW-1:0] ab_td, ab_pd, ba_td, ba_pd;
    wire [UW-1:0] ab_tu, ab_pu, ba_tu, ba_pu;
    wire          ab_tl, ab_pl, ba_tl, ba_pl;
    wire          ab_tv, ab_pv, ba_tv, ba_pv;

    wire [63:0] a_ctr_tx, a_ctr_rx, a_ctr_stall;
    wire [63:0] b_ctr_tx, b_ctr_rx, b_ctr_stall;
    wire [31:0] a_cred, b_cred;
    wire        a_flen, b_flen;

    mag_link #(.LINK_W(LW), .TUSER_W(UW), .RX_BEATS(RXB), .MAX_BEATS(MAXB)) A (
        .clk(clk), .resetn(resetn), .my_mesh(2'd0), .peer_mesh(2'd1),
        .tx0_hdr(a_h0), .tx0_hvalid(a_hv0), .tx0_hready(a_hr0),
        .tx0_dat(a_d0), .tx0_dlast(a_dl0), .tx0_dvalid(a_dv0), .tx0_dready(a_dr0),
        .tx1_hdr(a_h1), .tx1_hvalid(a_hv1), .tx1_hready(a_hr1),
        .tx1_dat(a_d1), .tx1_dlast(a_dl1), .tx1_dvalid(a_dv1), .tx1_dready(a_dr1),
        .rx0_hdr(), .rx0_hvalid(), .rx0_hready(1'b1),
        .rx0_dat(), .rx0_dlast(), .rx0_dvalid(), .rx0_dready(1'b1),
        .rx1_hdr(), .rx1_hvalid(), .rx1_hready(1'b1),
        .rx1_dat(), .rx1_dlast(), .rx1_dvalid(), .rx1_dready(1'b1),
        .m_axis_tdata(ab_td), .m_axis_tuser(ab_tu), .m_axis_tlast(ab_tl),
        .m_axis_tvalid(ab_tv), .m_axis_tready(1'b1),
        .s_axis_tdata(ba_pd), .s_axis_tuser(ba_pu), .s_axis_tlast(ba_pl),
        .s_axis_tvalid(ba_pv), .s_axis_tready(),
        .ctr_tx(a_ctr_tx), .ctr_rx(a_ctr_rx), .ctr_stall(a_ctr_stall),
        .cred_state(a_cred), .fault_len(a_flen)
    );

    mag_link #(.LINK_W(LW), .TUSER_W(UW), .RX_BEATS(RXB), .MAX_BEATS(MAXB)) B (
        .clk(clk), .resetn(resetn), .my_mesh(2'd1), .peer_mesh(2'd0),
        .tx0_hdr({UW{1'b0}}), .tx0_hvalid(1'b0), .tx0_hready(),
        .tx0_dat({LW{1'b0}}), .tx0_dlast(1'b0), .tx0_dvalid(1'b0), .tx0_dready(),
        .tx1_hdr({UW{1'b0}}), .tx1_hvalid(1'b0), .tx1_hready(),
        .tx1_dat({LW{1'b0}}), .tx1_dlast(1'b0), .tx1_dvalid(1'b0), .tx1_dready(),
        .rx0_hdr(b_rh0), .rx0_hvalid(b_rhv0), .rx0_hready(b_rhr0),
        .rx0_dat(b_rd0), .rx0_dlast(b_rdl0), .rx0_dvalid(b_rdv0),
        .rx0_dready(b_rdr0),
        .rx1_hdr(b_rh1), .rx1_hvalid(b_rhv1), .rx1_hready(b_rhr1),
        .rx1_dat(b_rd1), .rx1_dlast(b_rdl1), .rx1_dvalid(b_rdv1),
        .rx1_dready(b_rdr1),
        .m_axis_tdata(ba_td), .m_axis_tuser(ba_tu), .m_axis_tlast(ba_tl),
        .m_axis_tvalid(ba_tv), .m_axis_tready(1'b1),
        .s_axis_tdata(ab_pd), .s_axis_tuser(ab_pu), .s_axis_tlast(ab_pl),
        .s_axis_tvalid(ab_pv), .s_axis_tready(),
        .ctr_tx(b_ctr_tx), .ctr_rx(b_ctr_rx), .ctr_stall(b_ctr_stall),
        .cred_state(b_cred), .fault_len(b_flen)
    );

    mag_link_pipe #(.LINK_W(LW), .TUSER_W(UW), .DEPTH(16)) P_AB (
        .clk(clk), .resetn(resetn), .tap(tap),
        .i_tdata(ab_td), .i_tuser(ab_tu), .i_tlast(ab_tl), .i_tvalid(ab_tv),
        .o_tdata(ab_pd), .o_tuser(ab_pu), .o_tlast(ab_pl), .o_tvalid(ab_pv)
    );
    mag_link_pipe #(.LINK_W(LW), .TUSER_W(UW), .DEPTH(16)) P_BA (
        .clk(clk), .resetn(resetn), .tap(tap),
        .i_tdata(ba_td), .i_tuser(ba_tu), .i_tlast(ba_tl), .i_tvalid(ba_tv),
        .o_tdata(ba_pd), .o_tuser(ba_pu), .o_tlast(ba_pl), .o_tvalid(ba_pv)
    );

    // ------------------------------------------------------------ expectation
    localparam integer QD = 64;
    reg [UW-1:0] exp_h0 [0:QD-1];  reg [UW-1:0] exp_h1 [0:QD-1];
    reg [31:0]   exp_s0 [0:QD-1];  reg [31:0]   exp_s1 [0:QD-1];
    reg [15:0]   exp_l0 [0:QD-1];  reg [15:0]   exp_l1 [0:QD-1];
    integer wr0, rd0, wr1, rd1;

    function [LW-1:0] patt(input [31:0] s, input [15:0] i);
        integer k;
        begin
            for (k = 0; k < LW/32; k = k + 1) begin
                patt[k*32 +: 32] = s + {16'd0, i} * 32'd7 + k[31:0];
            end
        end
    endfunction

    function [UW-1:0] hdr(input [3:0] kind, input [1:0] dm, input [7:0] txn,
                          input [15:0] len, input [33:0] addr);
        begin
            hdr = {UW{1'b0}};
            hdr[U_KIND  +: 4]  = kind;
            hdr[U_DMESH +: 2]  = dm;
            hdr[U_TXN   +: 8]  = txn;
            hdr[U_LEN   +: 16] = len;
            hdr[U_ADDR  +: 34] = addr;
        end
    endfunction

    // ---- one sender process per class; concurrent calls on one class would
    // ---- fight over the same wires whatever the task's storage class.
    task automatic send0(input [3:0] kind, input [1:0] dm, input [7:0] txn,
                         input [15:0] len, input [33:0] addr, input [31:0] seed);
        integer i;
        begin
            exp_h0[wr0] = hdr(kind, dm, txn, len, addr);
            exp_s0[wr0] = seed;
            exp_l0[wr0] = len;
            wr0 = wr0 + 1;

            a_h0 <= hdr(kind, dm, txn, len, addr);
            a_hv0 <= 1'b1;
            @(posedge clk);
            while (!a_hr0) begin
                @(posedge clk);
            end
            a_hv0 <= 1'b0;
            for (i = 0; i <= len; i = i + 1) begin
                a_d0 <= patt(seed, i[15:0]);
                a_dl0 <= (i == len);
                a_dv0 <= 1'b1;
                @(posedge clk);
                while (!a_dr0) begin
                    @(posedge clk);
                end
            end
            a_dv0 <= 1'b0;
            a_dl0 <= 1'b0;
        end
    endtask

    task automatic send1(input [3:0] kind, input [1:0] dm, input [7:0] txn,
                         input [15:0] len, input [33:0] addr, input [31:0] seed);
        integer i;
        begin
            exp_h1[wr1] = hdr(kind, dm, txn, len, addr);
            exp_s1[wr1] = seed;
            exp_l1[wr1] = len;
            wr1 = wr1 + 1;

            a_h1 <= hdr(kind, dm, txn, len, addr);
            a_hv1 <= 1'b1;
            @(posedge clk);
            while (!a_hr1) begin
                @(posedge clk);
            end
            a_hv1 <= 1'b0;
            for (i = 0; i <= len; i = i + 1) begin
                a_d1 <= patt(seed, i[15:0]);
                a_dl1 <= (i == len);
                a_dv1 <= 1'b1;
                @(posedge clk);
                while (!a_dr1) begin
                    @(posedge clk);
                end
            end
            a_dv1 <= 1'b0;
            a_dl1 <= 1'b0;
        end
    endtask

    task automatic recv0;
        integer i;
        reg [UW-1:0] got_h;
        reg [15:0]   want_len;
        reg [31:0]   want_seed;
        begin
            b_rhr0 <= 1'b1;
            @(posedge clk);
            while (!b_rhv0) begin
                @(posedge clk);
            end
            got_h = b_rh0;
            b_rhr0 <= 1'b0;

            checks = checks + 1;
            if (rd0 >= wr0) begin
                fail("a packet arrived on class 0 that was never sent");
            end
            else begin
                if (got_h !== exp_h0[rd0]) begin
                    fail("class 0 header mismatch");
                    $display("        got  %h", got_h);
                    $display("        want %h", exp_h0[rd0]);
                end
                want_seed = exp_s0[rd0];
                want_len  = exp_l0[rd0];
                rd0 = rd0 + 1;

                b_rdr0 <= 1'b1;
                for (i = 0; i <= want_len; i = i + 1) begin
                    @(posedge clk);
                    while (!b_rdv0) begin
                        @(posedge clk);
                    end
                    checks = checks + 1;
                    if (b_rd0 !== patt(want_seed, i[15:0])) begin
                        fail("class 0 payload mismatch");
                    end
                    if (b_rdl0 !== (i == want_len)) begin
                        fail("class 0 TLAST is on the wrong beat");
                    end
                end
                b_rdr0 <= 1'b0;
            end
        end
    endtask

    task automatic recv1;
        integer i;
        reg [UW-1:0] got_h;
        reg [15:0]   want_len;
        reg [31:0]   want_seed;
        begin
            b_rhr1 <= 1'b1;
            @(posedge clk);
            while (!b_rhv1) begin
                @(posedge clk);
            end
            got_h = b_rh1;
            b_rhr1 <= 1'b0;

            checks = checks + 1;
            if (rd1 >= wr1) begin
                fail("a packet arrived on class 1 that was never sent");
            end
            else begin
                if (got_h !== exp_h1[rd1]) begin
                    fail("class 1 header mismatch");
                end
                want_seed = exp_s1[rd1];
                want_len  = exp_l1[rd1];
                rd1 = rd1 + 1;

                b_rdr1 <= 1'b1;
                for (i = 0; i <= want_len; i = i + 1) begin
                    @(posedge clk);
                    while (!b_rdv1) begin
                        @(posedge clk);
                    end
                    checks = checks + 1;
                    if (b_rd1 !== patt(want_seed, i[15:0])) begin
                        fail("class 1 payload mismatch");
                    end
                end
                b_rdr1 <= 1'b0;
            end
        end
    endtask

    task reset_all;
        begin
            resetn <= 1'b0;
            a_hv0 <= 0; a_hv1 <= 0; a_dv0 <= 0; a_dv1 <= 0;
            a_dl0 <= 0; a_dl1 <= 0;
            b_rhr0 <= 0; b_rhr1 <= 0; b_rdr0 <= 0; b_rdr1 <= 0;
            a_h0 <= 0; a_h1 <= 0; a_d0 <= 0; a_d1 <= 0;
            wr0 = 0; rd0 = 0; wr1 = 0; rd1 = 0;
            repeat (8) @(posedge clk);
            resetn <= 1'b1;
            repeat (4) @(posedge clk);
        end
    endtask

    // =====================================================================
    integer t, n;
    integer stall_before;

    initial begin
        $display("=== mag_link: one crossing, taps 1..16 ===");

        for (t = 1; t <= 16; t = t + 1) begin
            tap = t[4:0];
            reset_all;

            // ---- 1. every kind, len 0 / 1 / many, on the terminating class
            fork
                begin
                    send0(K_MEM_WR,   2'd1, 8'h11, 16'd0,  34'h1_0000_0040, 32'hA1);
                    send0(K_NOC_FLIT, 2'd1, 8'h22, 16'd1,  34'h0,           32'hB2);
                    send0(K_DOORBELL, 2'd1, 8'h33, 16'd0,  34'h0,           32'hC3);
                    send0(K_MEM_WR,   2'd1, 8'h44, 16'd31, 34'h1_0000_0800, 32'hD4);
                end
                begin
                    recv0; recv0; recv0; recv0;
                end
            join

            // ---- 2. the forwarded class, same shapes
            fork
                begin
                    send1(K_MEM_WR, 2'd2, 8'h55, 16'd0,  34'h2_0000_0000, 32'hE5);
                    send1(K_MEM_WR, 2'd2, 8'h66, 16'd31, 34'h2_0000_1000, 32'hF6);
                end
                begin
                    recv1; recv1;
                end
            join

            // ---- 3. credits exhaust and recover
            // Four 32-beat packets against 64 beats of credit: two fit, the
            // rest cannot move until the receiver frees space. Nothing is
            // consumed until the link has provably stopped, so the stall is
            // tested as well as the recovery.
            reset_all;
            stall_before = a_ctr_stall[31:0];
            fork
                begin
                    for (n = 0; n < 4; n = n + 1) begin
                        send0(K_MEM_WR, 2'd1, n[7:0], MAXB[15:0] - 16'd1,
                              34'h1_0000_0000 + n * 64 * MAXB, 32'h1000 + n);
                    end
                end
                begin
                    repeat (300) @(posedge clk);
                    if (a_ctr_stall[31:0] == stall_before) begin
                        fail("the link never stalled -- credit is bounding nothing");
                    end
                    if (b_ctr_rx[63:32] > RXB) begin
                        fail("more beats arrived than the receiver has room for");
                    end
                    recv0; recv0; recv0; recv0;
                end
            join
            if (rd0 != 4) begin
                fail("packets were lost across the credit stall");
            end

            // ---- 4. class isolation, protocol.md s4(b)
            // The forward class is filled and never drained. The terminating
            // class must keep moving; a shared credit pool is exactly what
            // would stop it, and stopping it is a deadlock rather than a
            // slowdown.
            reset_all;
            fork
                begin
                    for (n = 0; n < 4; n = n + 1) begin
                        send1(K_MEM_WR, 2'd2, n[7:0], MAXB[15:0] - 16'd1,
                              34'h2_0000_0000 + n * 64 * MAXB, 32'h2000 + n);
                    end
                end
                begin
                    repeat (300) @(posedge clk);
                    send0(K_MEM_WR, 2'd1, 8'h77, 16'd3, 34'h1_0000_0000, 32'h77);
                    send0(K_MEM_WR, 2'd1, 8'h78, 16'd3, 34'h1_0000_0100, 32'h78);
                end
                begin
                    repeat (320) @(posedge clk);
                    recv0; recv0;
                    if (rd0 != 2) begin
                        fail("a full forward class stopped terminating traffic -- the credit classes share something");
                    end
                    recv1; recv1; recv1; recv1;
                end
            join

            if (a_flen || b_flen) begin
                fail("a length fault was raised by a packet within MAX_BEATS");
            end
        end

        $display("--- %0d checks, %0d errors", checks, errors);
        if (errors == 0) begin
            $display("PASS mag_link");
        end
        else begin
            $display("FAIL mag_link");
        end
        $finish;
    end

    initial begin
        #6_000_000;
        $display("WATCHDOG mag_link_tb -- no verdict. The link stopped making progress.");
        $display("FAIL mag_link");
        $finish;
    end

    // Structural, and checked every cycle rather than once: a TREADY that is not
    // a constant is a real combinational path across the SLR.
    always @(posedge clk) begin
        if (resetn && (A.s_axis_tready !== 1'b1 || B.s_axis_tready !== 1'b1)) begin
            fail("TREADY moved");
        end
    end
endmodule

`default_nettype wire

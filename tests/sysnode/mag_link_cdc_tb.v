// mag_link_cdc_tb -- one interlink across two mesh clocks, both classes.

//    A (mesh 0) ── cdc ──► B (mesh 1)        A on clkA, B on clkB
//               ◄── cdc ──                   credit comes back the same way

// CREDIT RECIRCULATION IS THE REAL TEST. 60 packets is ~750 beats against a
// 64-beat pool, so a credit return lost in the reverse crossing is a hang.

// THREE RATIOS, one of them deliberately non-harmonic: a crossing that works
// at 1:1 and hangs at 3:7 is the ordinary outcome (docs/workflow/simulate.md).

`timescale 1ns / 1ps
`default_nettype none

module mag_link_cdc_tb;
    localparam integer LW   = 288;
    localparam integer UW   = 96;
    localparam integer RXB  = 64;
    localparam integer MAXB = 32;
    localparam integer NPKT = 60;

    localparam integer U_KIND = 0, U_DMESH = 4, U_SMESH = 6, U_TXN = 8;
    localparam integer U_LEN = 16, U_ADDR = 32;
    localparam [3:0] K_NOC_FLIT = 4'h2;

    reg clk_a = 0, clk_b = 0, resetn = 0;
    real ha = 4.0, hb = 3.0;
    always begin
        #ha clk_a = ~clk_a;
    end
    always begin
        #hb clk_b = ~clk_b;
    end

    integer errors = 0, checks = 0, phase = 0;

    task chk(input cond, input [511:0] what, input integer where);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                if (errors < 20) begin
                    $display("  FAIL phase %0d: %0s [%0d]", phase, what, where);
                end
            end
        end
    endtask

    // ---- A's transmit side -------------------------------------------------
    reg  [UW-1:0] a_h0, a_h1;
    reg  [LW-1:0] a_d0, a_d1;
    reg           a_hv0, a_hv1, a_dv0, a_dv1, a_dl0, a_dl1;
    wire          a_hr0, a_hr1, a_dr0, a_dr1;

    // Sampled at the edge that decided the transfer, so the driver below can
    // read the outcome at the next negedge without racing the DUT.
    reg a_hr0_q, a_hr1_q, a_dr0_q, a_dr1_q;
    always @(posedge clk_a) begin
        a_hr0_q <= a_hr0; a_hr1_q <= a_hr1;
        a_dr0_q <= a_dr0; a_dr1_q <= a_dr1;
    end

    // ---- B's receive side --------------------------------------------------
    wire [UW-1:0] b_rh0, b_rh1;
    wire [LW-1:0] b_rd0, b_rd1;
    wire          b_rhv0, b_rhv1, b_rdv0, b_rdv1, b_rdl0, b_rdl1;
    reg           b_rhr0, b_rhr1, b_rdr0, b_rdr1;

    wire [LW-1:0] ab_td, ab_cd, ba_td, ba_cd;
    wire [UW-1:0] ab_tu, ab_cu, ba_tu, ba_cu;
    wire          ab_tl, ab_cl, ba_tl, ba_cl;
    wire          ab_tv, ab_cv, ba_tv, ba_cv;
    wire          ab_fault, ba_fault, ab_ready, ba_ready;

    mag_link #(.LINK_W(LW), .TUSER_W(UW), .RX_BEATS(RXB), .MAX_BEATS(MAXB)) A (
        .clk(clk_a), .resetn(resetn), .my_mesh(2'd0), .peer_mesh(2'd1),
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
        .s_axis_tdata(ba_cd), .s_axis_tuser(ba_cu), .s_axis_tlast(ba_cl),
        .s_axis_tvalid(ba_cv), .s_axis_tready(),
        .ctr_tx(), .ctr_rx(), .ctr_stall(), .cred_state(), .fault_len()
    );

    mag_link #(.LINK_W(LW), .TUSER_W(UW), .RX_BEATS(RXB), .MAX_BEATS(MAXB)) B (
        .clk(clk_b), .resetn(resetn), .my_mesh(2'd1), .peer_mesh(2'd0),
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
        .s_axis_tdata(ab_cd), .s_axis_tuser(ab_cu), .s_axis_tlast(ab_cl),
        .s_axis_tvalid(ab_cv), .s_axis_tready(),
        .ctr_tx(), .ctr_rx(), .ctr_stall(), .cred_state(), .fault_len()
    );

    mag_link_cdc #(.LINK_W(LW), .TUSER_W(UW), .RX_BEATS(RXB)) C_AB (
        .s_axis_aclk(clk_a), .s_axis_aresetn(resetn),
        .m_axis_aclk(clk_b), .m_axis_aresetn(resetn),
        .S_AXIS_tdata(ab_td), .S_AXIS_tuser(ab_tu), .S_AXIS_tlast(ab_tl),
        .S_AXIS_tvalid(ab_tv), .S_AXIS_tready(),
        .M_AXIS_tdata(ab_cd), .M_AXIS_tuser(ab_cu), .M_AXIS_tlast(ab_cl),
        .M_AXIS_tvalid(ab_cv), .M_AXIS_tready(1'b1),
        .fault(ab_fault), .ready(ab_ready)
    );

    mag_link_cdc #(.LINK_W(LW), .TUSER_W(UW), .RX_BEATS(RXB)) C_BA (
        .s_axis_aclk(clk_b), .s_axis_aresetn(resetn),
        .m_axis_aclk(clk_a), .m_axis_aresetn(resetn),
        .S_AXIS_tdata(ba_td), .S_AXIS_tuser(ba_tu), .S_AXIS_tlast(ba_tl),
        .S_AXIS_tvalid(ba_tv), .S_AXIS_tready(),
        .M_AXIS_tdata(ba_cd), .M_AXIS_tuser(ba_cu), .M_AXIS_tlast(ba_cl),
        .M_AXIS_tvalid(ba_cv), .M_AXIS_tready(1'b1),
        .fault(ba_fault), .ready(ba_ready)
    );

    // Order holds inside a class, so the expected stream is a counter and a
    // mismatch names the exact beat that went missing.
    integer sent_beat [0:1];
    integer got_beat  [0:1];
    integer sent_pkt  [0:1];
    integer got_hdr   [0:1];

    function [LW-1:0] payload(input integer cls, input integer n);
        payload = {{(LW-64){1'b0}}, cls[31:0], n[31:0]};
    endfunction

    function [UW-1:0] header(input integer cls, input integer len_m1);
        begin
            header = {UW{1'b0}};
            header[U_KIND  +: 4]  = K_NOC_FLIT;
            // class 0 stops at B (dst is B); class 1 B forwards (dst is beyond).
            header[U_DMESH +: 2]  = cls ? 2'd2 : 2'd1;
            header[U_SMESH +: 2]  = 2'd0;
            header[U_TXN   +: 8]  = len_m1[7:0];
            header[U_LEN   +: 16] = len_m1[15:0];
        end
    endfunction

    // Ready driven at the POSEDGE, outcome read at the NEGEDGE: the rx FIFOs
    // are FWFT, so reading `rd_data` at the edge that pops it races.
    integer rseed = 12345;
    always @(posedge clk_b) begin
        b_rhr0 <= 1'b1;
        b_rhr1 <= 1'b1;
        b_rdr0 <= (($random(rseed) & 3) != 0);
        b_rdr1 <= (($random(rseed) & 3) != 0);
    end

    always @(negedge clk_b) if (resetn) begin
        if (b_rdv0 && b_rdr0) begin
            if (b_rd0 !== payload(0, got_beat[0])) begin
                chk(1'b0, "class 0 beat is out of order or lost", got_beat[0]);
            end
            got_beat[0] = got_beat[0] + 1;
        end
        if (b_rdv1 && b_rdr1) begin
            if (b_rd1 !== payload(1, got_beat[1])) begin
                chk(1'b0, "class 1 beat is out of order or lost", got_beat[1]);
            end
            got_beat[1] = got_beat[1] + 1;
        end
        if (b_rhv0 && b_rhr0) begin
            got_hdr[0] = got_hdr[0] + 1;
        end
        if (b_rhv1 && b_rhr1) begin
            got_hdr[1] = got_hdr[1] + 1;
        end
    end

    // ---- A sends -----------------------------------------------------------
    integer k;

    task send_pkt(input integer cls, input integer len_m1);
        begin
            @(negedge clk_a);
            if (cls == 0) begin a_h0 = header(0, len_m1); a_hv0 = 1'b1; end
            else          begin a_h1 = header(1, len_m1); a_hv1 = 1'b1; end
            @(negedge clk_a);
            while (!(cls ? a_hr1_q : a_hr0_q)) begin
                @(negedge clk_a);
            end
            a_hv0 = 1'b0; a_hv1 = 1'b0;

            for (k = 0; k <= len_m1; k = k + 1) begin
                @(negedge clk_a);
                if (cls == 0) begin
                    a_d0 = payload(0, sent_beat[0]); a_dl0 = (k == len_m1);
                    a_dv0 = 1'b1;
                end else begin
                    a_d1 = payload(1, sent_beat[1]); a_dl1 = (k == len_m1);
                    a_dv1 = 1'b1;
                end
                @(negedge clk_a);
                while (!(cls ? a_dr1_q : a_dr0_q)) begin
                    @(negedge clk_a);
                end
                a_dv0 = 1'b0; a_dv1 = 1'b0;
                sent_beat[cls] = sent_beat[cls] + 1;
            end
            sent_pkt[cls] = sent_pkt[cls] + 1;
        end
    endtask

    integer p, cls, len_m1, spin;

    task run_phase(input real b_half, input [255:0] label);
        begin
            resetn = 1'b0;
            hb = b_half;
            a_hv0 = 0; a_hv1 = 0; a_dv0 = 0; a_dv1 = 0;
            a_dl0 = 0; a_dl1 = 0;
            for (k = 0; k < 2; k = k + 1) begin
                sent_beat[k] = 0; got_beat[k] = 0;
                sent_pkt[k] = 0;  got_hdr[k] = 0;
            end
            repeat (40) @(negedge clk_a);
            resetn = 1'b1;
            // What the block design must do with `ready`: release the meshes
            // only once every crossing on the chain is out of its own reset.
            spin = 0;
            while (!(ab_ready && ba_ready) && (spin < 10000)) begin
                spin = spin + 1;
                @(negedge clk_a);
            end
            chk(spin < 10000, "both crossings came out of reset", spin);

            $display("--- %0s: A %0.1f MHz, B %0.1f MHz ---", label,
                     500.0 / ha, 500.0 / b_half);

            for (p = 0; p < NPKT; p = p + 1) begin
                cls    = p & 1;
                len_m1 = (p * 7) % 24;
                send_pkt(cls, len_m1);
            end

            // Long enough for the slowest ratio to drain, and bounded so a
            // lost credit is a FAIL rather than a run that never ends.
            spin = 0;
            while (((got_beat[0] < sent_beat[0]) || (got_beat[1] < sent_beat[1]))
                   && (spin < 200000)) begin
                spin = spin + 1;
                @(negedge clk_b);
            end

            chk(spin < 200000, "every beat arrived before the deadline", spin);
            chk(got_beat[0] == sent_beat[0], "class 0 beat count", got_beat[0]);
            chk(got_beat[1] == sent_beat[1], "class 1 beat count", got_beat[1]);
            chk(got_hdr[0] == sent_pkt[0], "class 0 packet count", got_hdr[0]);
            chk(got_hdr[1] == sent_pkt[1], "class 1 packet count", got_hdr[1]);
            chk(ab_fault === 1'b0, "the forward crossing discarded nothing", 0);
            chk(ba_fault === 1'b0, "the credit crossing discarded nothing", 0);
            $display("    %0d + %0d beats in %0d + %0d packets, all delivered",
                     got_beat[0], got_beat[1], got_hdr[0], got_hdr[1]);
            phase = phase + 1;
        end
    endtask

    initial begin
        run_phase(3.00, "B faster than A");
        run_phase(7.00, "B slower than A");
        // 4.07 against 4.00 is deliberately non-harmonic: the pointer
        // synchronisers see a beat frequency instead of a fixed phase.
        run_phase(4.07, "B beside A, unrelated");

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
        #4000000;
        $display("  FAIL -- watchdog in phase %0d", phase);
        $finish;
    end

endmodule

`default_nettype wire

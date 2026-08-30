// kx_hop bench: one credited single-boundary hop, either landing buffer
// (-d TB_LEAN=0|1), any width (-d TB_W), any depth (-d TB_DEPTH).
// Checks: every flit arrives once, in order; the sender is held while the
// receiver is in reset and while credits are out; a receiver released AFTER
// the sender loses nothing; one flit per cycle sustained through the hop;
// the empty-hop latency; credit never exceeds DEPTH. Prints the measured
// latency and throughput so the two buffers can be compared.

`timescale 1ns/1ps
`default_nettype none

`ifndef TB_W
  `define TB_W 590
`endif
`ifndef TB_DEPTH
  `define TB_DEPTH 16
`endif
`ifndef TB_LEAN
  `define TB_LEAN 0
`endif

module kx_hop_tb;
    localparam integer W = `TB_W, DEPTH = `TB_DEPTH;
    // -d TB_LEAN=1 selects the lean ring; wide hops sit in block RAM, narrow
    // ones in LUTRAM, as the fabric builds them
    localparam         BUF = (`TB_LEAN != 0) ? "lean" : "xpm";
    localparam         MEM = (W > 128) ? "block" : "distributed";

    reg clk = 0;
    always #1.667 begin
        clk = ~clk;
    end
    reg s_rstn = 0, m_rstn = 0;

    reg          s_valid = 0;
    wire         s_ready;
    reg  [W-1:0] s_data = 0;
    wire         m_valid;
    reg          m_ready = 0;
    wire [W-1:0] m_data;

    kx_hop #(.WIDTH(W), .DEPTH(DEPTH), .MEM(MEM), .BUF(BUF)) dut (
        .clk(clk), .s_rstn(s_rstn), .m_rstn(m_rstn),
        .s_valid(s_valid), .s_ready(s_ready), .s_data(s_data),
        .m_valid(m_valid), .m_ready(m_ready), .m_data(m_data));

    integer errors = 0, checks = 0;
    task check(input cond, input [8*64-1:0] what);
        begin
            checks = checks + 1;
            if (!cond) begin errors = errors + 1; $display("%0t ERROR %0s", $time, what); end
        end
    endtask

    // the flit payload is its sequence number, stretched over the width
    function [W-1:0] pat(input integer n);
        integer k;
        begin
            pat = 0;
            for (k = 0; k < W; k = k + 32) begin pat[k +: 32] = n * 32'h9e3779b1 + k; end
        end
    endfunction

    // scoreboard: in order, once; accept-to-deliver latency of every flit,
    // its minimum being the empty-hop latency
    integer sent = 0, got = 0, lat_min = 1000, lat_c;
    real t_send [0:65535];
    always @(posedge clk) begin
        if (s_valid && s_ready) begin
            t_send[sent % 65536] <= $realtime;
            sent <= sent + 1;
        end
        if (m_valid && m_ready) begin
            check(m_data == pat(got), "flit out of order or corrupted");
            lat_c = ($realtime - t_send[got % 65536]) / 3.334 + 0.5;
            if (lat_c < lat_min) begin lat_min = lat_c; end
            got <= got + 1;
        end
    end
    // present the next flit whenever the driver says so
    reg drive = 0;
    always @(posedge clk) begin
        if (s_valid && s_ready) begin s_data <= pat(sent + 1); end
        else if (!s_valid) begin s_data <= pat(sent); end
    end
    always @(*) begin s_valid = drive && s_rstn; end

    // the sender must never see ready while the receiver is in reset
    always @(posedge clk) begin
        if (!m_rstn && s_ready) begin check(0, "s_ready high while the receiver is in reset"); end
    end
    // credit bookkeeping: the sender's counter never above DEPTH
    always @(posedge clk) begin
        if (!(dut.u_tx.credit <= DEPTH)) begin
            $display("  %0t credit=%0d s_rstn=%b m_rstn=%b cr=%b tx_v=%b", $time,
                     dut.u_tx.credit, s_rstn, m_rstn, dut.u_tx.cr, dut.u_tx.tx_v);
        end
        check(dut.u_tx.credit <= DEPTH, "credit above DEPTH");
    end

    // Stimulus changes are NON-BLOCKING at the edge and reads happen 1 ps
    // after it: a blocking write at the edge races the DUT's own sampling
    // (xsim orders processes differently from Verilator), and a read at the
    // edge sees values from before it.
    task cycles(input integer n); begin repeat (n) @(posedge clk); #1; end endtask
    integer k, i, run;
    initial begin
        s_data = pat(0);
        // 1. receiver released 6 cycles AFTER the sender, sender driving at once
        cycles(4);
        s_rstn <= 1; drive <= 1; m_ready <= 1;
        cycles(6);
        m_rstn <= 1;
        cycles(200);
        drive <= 0;
        cycles(40);
        check(got == sent, "flits lost across the late receiver release");
        check(sent > 100, "sender never ran after the receiver released");

        // 2. empty-hop latency: one flit into an idle hop, receiver ready
        m_ready <= 1; drive <= 1;
        @(posedge clk); #1;
        while (!(s_valid && s_ready)) begin @(posedge clk); #1; end
        drive <= 0;
        cycles(12);
        check(got == sent, "latency probe flit lost");
        $display("  @@@ HOP latency %0d cycles (accept to deliver), buf=%0s W=%0d", lat_min, BUF, W);

        // 3. sustained: sender always valid, receiver always ready, 1000 cycles
        run = got; drive <= 1;
        cycles(1000);
        drive <= 0;
        cycles(40);
        $display("  @@@ HOP throughput %0d flits in 1000 cycles, buf=%0s W=%0d", got - run, BUF, W);
        check(got - run >= 1000 - 2 * DEPTH, "throughput below one flit per cycle");
        check(got == sent, "sustained stream lost flits");

        // 4. back-pressure: receiver stalls 3 x DEPTH cycles mid-stream, then
        //    random ready/valid for a long soak
        drive <= 1; m_ready <= 0;
        cycles(3 * DEPTH);
        check(dut.u_tx.credit == 0 && !s_ready, "sender still ready with the receiver stalled past DEPTH");
        m_ready <= 1;
        cycles(100);
        for (i = 0; i < 20000; i = i + 1) begin
            @(posedge clk);
            drive   <= ($random & 3) != 0;
            m_ready <= ($random & 3) != 0;
        end
        @(posedge clk);
        drive <= 0; m_ready <= 1;
        cycles(60);
        check(got == sent, "soak lost or duplicated flits");
        check(got > 10000, "soak moved too little");

        // 5. the sender held in reset longer than the receiver takes to drain:
        //    nothing lost, nothing invented, credits whole afterwards
        drive <= 1;
        cycles(50);
        drive <= 0; s_rstn <= 0;
        cycles(60);
        check(got == sent, "flits lost or invented across the sender reset");
        s_rstn <= 1;
        cycles(10);
        check(!m_valid, "receiver presented a flit no sender sent");
        check(dut.u_tx.credit == DEPTH, "credits not whole after the sender reset");

        if (errors == 0) begin $display("PASS -- kx_hop buf=%0s W=%0d DEPTH=%0d, %0d checks, 0 errors", BUF, W, DEPTH, checks); end
        else             begin $display("FAIL -- kx_hop buf=%0s W=%0d DEPTH=%0d, %0d checks, %0d errors", BUF, W, DEPTH, checks, errors); end
        $finish;
    end
endmodule

`default_nettype wire

// kx_aring at both clock ratios and both flow-control shapes, four rings at
// once: FULL 0 with a credit counter the bench keeps (the sender never has more
// than DEPTH in flight), FULL 1 driven by wr_busy alone. Random offers and
// pops; every word checked for order and content; every ring must drain.

`timescale 1ns / 1ps
`default_nettype none

module kx_aring_tb;
    localparam integer W = 523;
    localparam integer D = 16;

    reg fast = 1'b0;
    reg slow = 1'b0;
    always begin
        #1.0 fast = ~fast;
    end
    always begin
        #3.0 slow = ~slow;
    end
    reg rstn_w = 1'b0;                 // the writing side
    reg rstn_r = 1'b0;                 // the reading side, released later
    integer errors = 0;

    genvar s;
    generate
    for (s = 0; s < 4; s = s + 1) begin : g_s
        // s[0]: 0 = write slow / read fast, 1 = write fast / read slow
        // s[1]: FULL
        localparam integer FULL = s / 2;
        wire wclk = (s % 2 == 0) ? slow : fast;
        wire rclk = (s % 2 == 0) ? fast : slow;

        reg  [W-1:0] wdata;
        reg          want;              // this cycle's offer, decided once
        wire         wbusy;
        wire [W-1:0] rdata;
        reg          ren;
        wire         rbusy;
        reg  [31:0]  seq_w, seq_r;
        reg          quiet;

        // FULL 1: the write is gated by full in the SAME cycle, as kx_scdc
        // gates it. FULL 0: the bench is the credit counter -- it reads the
        // pop count straight out of the read domain, which a bench may do,
        // so what is under test is the ring and not a credit return.
        wire wen = want && (FULL ? !wbusy : ((seq_w - seq_r) < D));

        kohaku_aring #(.WIDTH(W), .DEPTH(D), .FULL(FULL)) u_r (
            .wr_clk(wclk), .wr_rstn(rstn_w), .wr_en(wen), .wr_data(wdata), .wr_busy(wbusy),
            .clk(rclk), .rstn(rstn_r), .rd_en(ren), .rd_data(rdata), .rd_busy(rbusy));

        always @(posedge wclk) begin
            if (!rstn_w) begin
                want <= 1'b0; seq_w <= 0; quiet <= 1'b0; wdata <= 0;
            end
            else begin
                if (wen) begin seq_w <= seq_w + 1; end
                // next offer: fresh data whenever the current one was taken
                // or nothing was offered
                if (wen || !want) begin
                    want  <= !quiet && ($urandom % 3 != 0);
                    wdata <= {seq_w + (wen ? 32'd1 : 32'd0), {(W-32){1'b0}}}
                           | (seq_w + (wen ? 32'd1 : 32'd0));
                end
                else if (quiet) begin want <= 1'b0; end
            end
        end
        always @(posedge rclk) begin
            if (!rstn_r) begin ren <= 1'b0; seq_r <= 0; end
            else begin
                ren <= ($urandom % 3 != 0);
                if (ren && !rbusy) begin
                    if (rdata !== ({seq_r, {(W-32){1'b0}}} | seq_r)) begin
                        $display("%0t ERROR ring %0d: got %h, expected seq %0d", $time, s, rdata[31:0], seq_r);
                        errors = errors + 1;
                    end
                    seq_r <= seq_r + 1;
                end
            end
        end
    end
    endgenerate

`define DRAIN(i) \
    checks = checks + 1 + g_s[i].seq_r; \
    if (g_s[i].seq_r != g_s[i].seq_w) begin \
        $display("ERROR ring %0d did not drain: wrote %0d read %0d", i, g_s[i].seq_w, g_s[i].seq_r); \
        errors = errors + 1; \
    end \
    if (g_s[i].seq_w < 2000) begin \
        $display("ERROR ring %0d wrote only %0d", i, g_s[i].seq_w); \
        errors = errors + 1; \
    end

    integer checks;
    initial begin
        checks = 0;
        #40;
        @(posedge slow) rstn_w <= 1'b1;
        #1500;                          // the reader comes out of reset late
        @(posedge slow) rstn_r <= 1'b1;
        #40000;
        g_s[0].quiet <= 1'b1; g_s[1].quiet <= 1'b1; g_s[2].quiet <= 1'b1; g_s[3].quiet <= 1'b1;
        #4000;
        `DRAIN(0)
        `DRAIN(1)
        `DRAIN(2)
        `DRAIN(3)
        $display("@@@ kx_aring_tb: %0d checks", checks);
        if (errors == 0) begin $display("PASS"); end
        else begin $display("FAIL: %0d error(s)", errors); end
        $finish;
    end
endmodule

`default_nettype wire

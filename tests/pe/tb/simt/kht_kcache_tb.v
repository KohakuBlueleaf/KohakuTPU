// kht_kcache_tb -- the scalar cache against a reference memory: hits carry the
// exact word, misses fill, lines that alias in bit 31 or in the tag never answer
// for each other, a fill in progress never answers, and invalidate drops all.

`timescale 1ns/1ps
`default_nettype none

module kht_kcache_tb;
    localparam integer LINES = 8;

    reg clk = 1'b0, resetn = 1'b0;
    always #2 clk = ~clk;

    integer errors = 0, checks = 0, hits = 0, fills = 0, probes = 0;

    reg  [31:0]  rd_addr;
    wire [31:0]  rd_data;
    wire         rd_hit;
    reg          miss_req;
    reg  [31:0]  miss_addr;
    wire         miss_ready, fill_done, busy;
    reg          inval;
    wire         fill_valid;
    reg          fill_ready;
    wire [31:0]  fill_addr;
    reg          resp_valid;
    reg  [255:0] resp_data;

    kht_kcache #(.LINES(LINES)) dut (
        .clk(clk), .resetn(resetn),
        .rd_addr(rd_addr), .rd_data(rd_data), .rd_hit(rd_hit),
        .miss_req(miss_req), .miss_addr(miss_addr), .miss_ready(miss_ready),
        .fill_done(fill_done), .inval(inval), .busy(busy),
        .fill_valid(fill_valid), .fill_ready(fill_ready), .fill_addr(fill_addr),
        .resp_valid(resp_valid), .resp_data(resp_data)
    );

    function [31:0] mem_word;
        input [31:0] a;
        reg   [31:0] z;
        begin
            z = (a & 32'hFFFF_FFFC) * 32'h85EB_CA6B + 32'h1234_5677;
            mem_word = z ^ (z >> 13);
        end
    endfunction

    integer lat;
    always @(posedge clk) begin
        if (!resetn) begin
            fill_ready <= 1'b0;
            resp_valid <= 1'b0;
            lat        <= -1;
        end else begin
            resp_valid <= 1'b0;
            fill_ready <= 1'b0;
            if (fill_valid && !fill_ready && ($random & 3) == 0) begin
                fill_ready <= 1'b1;
                lat        <= 1 + ({$random} % 20);
            end
            if (lat > 0) begin
                lat <= lat - 1;
            end
            else if (lat == 0) begin
                lat        <= -1;
                resp_valid <= 1'b1;
                resp_data  <= {mem_word(fill_addr + 28), mem_word(fill_addr + 24),
                               mem_word(fill_addr + 20), mem_word(fill_addr + 16),
                               mem_word(fill_addr + 12), mem_word(fill_addr + 8),
                               mem_word(fill_addr + 4),  mem_word(fill_addr)};
            end
        end
    end

    // A combinational lookup: the answer is checked in the same cycle.
    task look(input [31:0] a, output reg was_hit);
        begin
            rd_addr = a;
            #0.5;
            was_hit = rd_hit;
            if (rd_hit) begin
                checks = checks + 1;
                hits   = hits + 1;
                if (rd_data !== mem_word(a)) begin
                    errors = errors + 1;
                    if (errors < 10)
                        $display("  FAIL hit at %08x: %08x want %08x", a, rd_data, mem_word(a));
                end
            end
            @(posedge clk);
            #1;
        end
    endtask

    task fill(input [31:0] a);
        reg h;
        integer guard;
        begin
            while (!miss_ready) @(posedge clk);
            miss_req  = 1'b1;
            miss_addr = a;
            @(posedge clk);
            #1;
            miss_req = 1'b0;
            fills = fills + 1;
            guard = 0;
            while (!fill_done && guard < 400) begin
                look({a[31:5], 5'd0} | ((guard % 8) << 2), h);
                probes = probes + 1;
                guard = guard + 1;
            end
            checks = checks + 1;
            if (!fill_done) begin
                errors = errors + 1;
                $display("  FAIL fill of %08x never completed", a);
            end
            @(posedge clk);
            #1;
        end
    endtask

    reg [31:0] addr [0:3];
    reg        h;
    integer i, k, n;

    initial begin
        rd_addr  = 32'h8000_0000;
        miss_req = 1'b0;
        inval    = 1'b0;
        repeat (4) @(posedge clk);
        resetn = 1'b1;

        $display("--- the reset sweep answers nothing ---");
        for (i = 0; i < LINES; i = i + 1) begin
            look(32'h8000_0000 + 32 * i, h);
            checks = checks + 1;
            if (h) begin
                errors = errors + 1;
                $display("  FAIL hit during or right after the reset sweep");
            end
        end
        while (busy) @(posedge clk);

        addr[0] = 32'h8000_0020;
        addr[1] = 32'h8000_0020 + 32 * LINES;
        addr[2] = 32'h0000_0020;
        addr[3] = 32'h0000_0020 + 32 * LINES;

        $display("--- a miss fills, then hits; a conflicting fill evicts ---");
        for (i = 0; i < 4; i = i + 1) begin
            fill(addr[i]);
            for (k = 0; k < 8; k = k + 1) begin
                look(addr[i] + 4 * k, h);
                checks = checks + 1;
                if (!h) begin
                    errors = errors + 1;
                    $display("  FAIL %08x missed right after its fill", addr[i] + 4 * k);
                end
            end
            if (i > 0) begin
                look(addr[i-1], h);
                checks = checks + 1;
                if (h) begin
                    errors = errors + 1;
                    $display("  FAIL %08x still hits after %08x took its index", addr[i-1], addr[i]);
                end
            end
        end

        $display("--- random lookups over 4x the cache, fill on every miss ---");
        for (n = 0; n < 2000; n = n + 1) begin
            k = {$random} % (4 * LINES * 8);
            look(((($random & 1) != 0) ? 32'h8000_0000 : 32'h0000_0000) + 4 * k, h);
            if (!h) begin
                fill(rd_addr);
                look(rd_addr, h);
                checks = checks + 1;
                if (!h) begin
                    errors = errors + 1;
                    $display("  FAIL %08x missed right after its fill", rd_addr);
                end
            end
        end

        $display("--- invalidate drops every line, during the sweep too ---");
        fill(32'h8000_0100);
        while (!miss_ready) @(posedge clk);
        inval = 1'b1;
        @(posedge clk);
        #1;
        inval = 1'b0;
        while (busy) begin
            look(32'h8000_0100, h);
            checks = checks + 1;
            if (h) begin
                errors = errors + 1;
                $display("  FAIL 80000100 hit while the sweep was running");
            end
        end
        for (i = 0; i < LINES * 8; i = i + 1) begin
            look(32'h8000_0000 + 4 * i, h);
            checks = checks + 1;
            if (h) begin
                errors = errors + 1;
                if (errors < 10) $display("  FAIL %08x hit after invalidate", 32'h8000_0000 + 4 * i);
            end
        end

        $display("    %0d hits checked, %0d fills, %0d lookups during fills", hits, fills, probes);
        $display("========================================");
        if (errors == 0 && checks > 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end

    initial begin
        #20000000;
        $display("  FAIL WATCHDOG");
        $finish;
    end
endmodule

`default_nettype wire

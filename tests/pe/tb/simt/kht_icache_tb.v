// kht_icache_tb -- the instruction cache against a reference memory.
//
// Every cycle a fetch address goes in; the next cycle, a hit must carry exactly
// the reference word and its predecode, and a miss is filled through a memory
// that answers after a random delay. Three things are aimed at on purpose:
//
//   * addresses that differ only in bit 31 or only in the tag (aliasing);
//   * fetches of the line BEING filled, every cycle of the fill (a tag that goes
//     valid before its last word is a wrong word here, never a hit);
//   * invalidate, after which nothing that hit before may hit.

`timescale 1ns/1ps
`default_nettype none

module kht_icache_tb;
    localparam integer LINES = 16;

    reg clk = 1'b0, resetn = 1'b0;
    always #2 clk = ~clk;

    integer errors = 0, checks = 0, hits = 0, fills = 0, probes = 0;

    reg  [31:0]  rd_addr;
    wire [31:0]  rd_instr;
    wire [59:0]  rd_ctrl;
    wire         rd_hit;
    reg          miss_req;
    reg  [31:0]  miss_addr;
    wire         miss_ready, fill_done, busy;
    wire [31:0]  fill_line;
    reg          inval;
    wire         fill_valid;
    reg          fill_ready;
    wire [31:0]  fill_addr;
    reg          resp_valid;
    reg  [255:0] resp_data;

    kht_icache #(.LINES(LINES), .MEM_PRIM("block")) dut (
        .clk(clk), .resetn(resetn),
        .rd_addr(rd_addr), .rd_instr(rd_instr), .rd_ctrl(rd_ctrl), .rd_hit(rd_hit),
        .miss_req(miss_req), .miss_addr(miss_addr), .miss_ready(miss_ready),
        .fill_done(fill_done), .fill_line(fill_line),
        .inval(inval), .busy(busy),
        .fill_valid(fill_valid), .fill_ready(fill_ready), .fill_addr(fill_addr),
        .resp_valid(resp_valid), .resp_data(resp_data)
    );

    // The reference memory: every word a function of its address, so a word
    // from the wrong line or the wrong slot is a different value.
    function [31:0] mem_word;
        input [31:0] a;
        reg   [31:0] z;
        begin
            z = (a & 32'hFFFF_FFFC) * 32'h9E37_79B1 + 32'h7F4A_7C15;
            mem_word = z ^ (z >> 15);
        end
    endfunction

    wire [59:0] ref_ctrl;
    reg  [31:0] ref_addr;
    kht_predec u_ref (.instr(mem_word(ref_addr)), .ctrl(ref_ctrl));

    // ---- the memory: accept a fill after 0-3 cycles, answer after 1-20 ----
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

    // ---- one fetch: present, then check the answer a cycle later ----------
    task fetch(input [31:0] a, output reg was_hit);
        begin
            rd_addr = a;
            @(posedge clk);
            #1;
            ref_addr = a;
            #0.1;
            was_hit = rd_hit;
            if (rd_hit) begin
                checks = checks + 1;
                hits   = hits + 1;
                if ((rd_instr !== mem_word(a)) || (rd_ctrl !== ref_ctrl)) begin
                    errors = errors + 1;
                    if (errors < 10)
                        $display("  FAIL hit at %08x: word %08x want %08x, ctrl %h want %h",
                                 a, rd_instr, mem_word(a), rd_ctrl, ref_ctrl);
                end
            end
        end
    endtask

    // Fill the line holding `a`, fetching that line's words every cycle of the
    // fill: any hit before the fill completes must already be the right word.
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
                fetch({a[31:5], 5'd0} | ((guard % 8) << 2), h);
                probes = probes + 1;
                guard = guard + 1;
            end
            checks = checks + 1;
            if (!fill_done || (fill_line !== {a[31:5], 5'd0})) begin
                errors = errors + 1;
                $display("  FAIL fill of %08x: done %b line %08x", a, fill_done, fill_line);
            end
            @(posedge clk);
            #1;
        end
    endtask

    reg [31:0] addr [0:63];
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
            fetch(32'h8000_0000 + 32 * i, h);
            checks = checks + 1;
            if (h) begin
                errors = errors + 1;
                $display("  FAIL hit during or right after the reset sweep at line %0d", i);
            end
        end
        while (busy) @(posedge clk);

        // Aliasing set: four lines on ONE index -- two DRAM tags, and two that
        // differ from them only in bit 31 (the window region).
        addr[0] = 32'h8000_0040;
        addr[1] = 32'h8000_0040 + 32 * LINES;
        addr[2] = 32'h0000_0040;
        addr[3] = 32'h0000_0040 + 32 * LINES;

        $display("--- a miss fills, then hits; a conflicting fill evicts ---");
        for (i = 0; i < 4; i = i + 1) begin
            fetch(addr[i], h);
            checks = checks + 1;
            if (h) begin
                errors = errors + 1;
                $display("  FAIL %08x hit before it was ever filled", addr[i]);
            end
            fill(addr[i]);
            for (k = 0; k < 8; k = k + 1) begin
                fetch(addr[i] + 4 * k, h);
                checks = checks + 1;
                if (!h) begin
                    errors = errors + 1;
                    $display("  FAIL %08x missed right after its fill", addr[i] + 4 * k);
                end
            end
            if (i > 0) begin
                fetch(addr[i-1], h);
                checks = checks + 1;
                if (h) begin
                    errors = errors + 1;
                    $display("  FAIL %08x still hits after %08x took its index",
                             addr[i-1], addr[i]);
                end
            end
        end

        $display("--- random fetch stream over 4x the cache, fill on every miss ---");
        for (n = 0; n < 3000; n = n + 1) begin
            k = {$random} % (4 * LINES * 8);
            fetch(((($random & 1) != 0) ? 32'h8000_0000 : 32'h0000_0000) + 4 * k, h);
            if (!h) begin
                fill(rd_addr);
                fetch(rd_addr, h);
                checks = checks + 1;
                if (!h) begin
                    errors = errors + 1;
                    $display("  FAIL %08x missed right after its fill", rd_addr);
                end
            end
        end

        $display("--- invalidate drops every line ---");
        fill(32'h8000_1000);
        fetch(32'h8000_1000, h);
        while (!miss_ready) @(posedge clk);
        inval = 1'b1;
        @(posedge clk);
        #1;
        inval = 1'b0;
        // DURING the sweep too: a line the cursor has not reached is still marked
        // valid in the array, and only the sweep state keeps it from answering.
        while (busy) begin
            fetch(32'h8000_1000, h);
            checks = checks + 1;
            if (h) begin
                errors = errors + 1;
                $display("  FAIL 80001000 hit while the invalidate sweep was running");
            end
        end
        for (i = 0; i < LINES * 8; i = i + 1) begin
            fetch(32'h8000_0000 + 4 * i, h);
            checks = checks + 1;
            if (h) begin
                errors = errors + 1;
                if (errors < 10) $display("  FAIL %08x hit after invalidate", 32'h8000_0000 + 4 * i);
            end
        end

        $display("    %0d hits checked, %0d fills, %0d fetches during fills", hits, fills, probes);
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

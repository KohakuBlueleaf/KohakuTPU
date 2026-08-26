// Cross-checks sim/verilator/shims/xpm_fifo_async.v against the real Xilinx
// xpm_fifo_async, by running the SAME bench under xsim and under Verilator: xsim
// binds the Xilinx cell, Verilator binds the shim. A stream of counted words
// must survive the crossing in order with none lost or duplicated.
//
// Deliberately free of the two idioms that make the tests/ benches disagree
// between simulators: no $random, and no non-blocking assignment in an initial
// block. Stimulus is driven off the clock's negative edge so nothing races the
// DUT's own edge.

`default_nettype none
`timescale 1ns/1ps

module vlt_async_fifo_tb;

    localparam integer DW    = 32;
    localparam integer DEPTH = 32;
    localparam integer N     = 512;

    reg wr_clk = 1'b0;
    reg rd_clk = 1'b0;
    reg wr_rst = 1'b1;

    // 7ns vs 11ns: non-harmonic on purpose, so the crossing is a real one.
    always #3.5 wr_clk = ~wr_clk;
    always #5.5 rd_clk = ~rd_clk;

    reg  [DW-1:0] wr_data = {DW{1'b0}};
    reg           wr_en   = 1'b0;
    wire          wr_full;
    wire [DW-1:0] rd_data;
    reg           rd_en   = 1'b0;
    wire          rd_empty;

    async_fifo #(
        .DATA_WIDTH(DW), .FIFO_DEPTH(DEPTH), .MEMORY_TYPE("distributed")
    ) dut (
        .wr_clk(wr_clk), .wr_rst(wr_rst),
        .wr_en(wr_en), .wr_data(wr_data), .wr_full(wr_full),
        .rd_clk(rd_clk), .rd_en(rd_en), .rd_data(rd_data), .rd_empty(rd_empty)
    );

    integer sent = 0;
    integer got  = 0;
    integer errors = 0;

    // A deterministic pattern, not $random: the two simulators must see the
    // identical stimulus or the comparison proves nothing. Driven from a
    // free-running tick, never from the item index -- indexing it lets a zero
    // stall the loop that would have advanced the index.
    function automatic pace(input integer i);
        pace = (((i * 2654435761) >> 13) & 32'h3) != 0;
    endfunction

    integer wtick = 0;
    integer rtick = 0;
    integer max_level = 0;

    initial begin
        wr_rst = 1'b1;
        repeat (20) @(negedge wr_clk);
        wr_rst = 1'b0;
        repeat (20) @(negedge wr_clk);

        while (sent < N) begin
            @(negedge wr_clk);
            wtick = wtick + 1;
            if (!wr_full && pace(wtick)) begin
                wr_data = sent[DW-1:0];
                wr_en   = 1'b1;
                sent    = sent + 1;
                if (sent - got > max_level) max_level = sent - got;
            end
            else begin
                wr_en = 1'b0;
            end
        end
        @(negedge wr_clk);
        wr_en = 1'b0;
    end

    initial begin
        rd_en = 1'b0;
        while (got < N) begin
            @(negedge rd_clk);
            rtick = rtick + 1;
            // Idle first, so the FIFO is driven hard against FULL and the peak
            // in flight is a real capacity measurement rather than a trickle.
            if (!rd_empty && rtick > 64 && pace(rtick + 7)) begin
                if (rd_data !== got[DW-1:0]) begin
                    errors = errors + 1;
                    if (errors < 8)
                        $display("  FAIL word %0d: got %h want %h", got, rd_data, got[DW-1:0]);
                end
                got   = got + 1;
                rd_en = 1'b1;
            end
            else begin
                rd_en = 1'b0;
            end
        end
        @(negedge rd_clk);
        rd_en = 1'b0;

        $display("========================================");
        // REPORTED, not asserted: peak in flight must MATCH the real cell, which
        // is what catches a shim that is a word or two shallow.
        $display("  peak occupancy %0d of %0d", max_level, DEPTH);
        if (errors == 0) $display("  PASS -- %0d words crossed, 0 errors", got);
        else             $display("  FAIL -- %0d words crossed, %0d errors", got, errors);
        $display("========================================");
        $finish;
    end

    initial begin
        #2000000;
        $display("  FAIL WATCHDOG -- sent %0d, got %0d (full=%b empty=%b)",
                 sent, got, wr_full, rd_empty);
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire

// Cross-checks sim/verilator/shims/xpm_fifo_sync.v against the real Xilinx
// xpm_fifo_sync: the same bench binds the Xilinx cell under xsim and the shim
// under the C++ simulator, and both must agree word for word.
//
// Harder than a stream: the reader is stalled long enough to drive the FIFO
// FULL and hold it there, then drains it EMPTY, so the wr_busy/rd_busy edges and
// the fwft first-word timing are both exercised rather than stepped around.

`default_nettype none
`timescale 1ns/1ps

module vlt_sync_fifo_tb;

    localparam integer DW    = 32;
    localparam integer DEPTH = 16;
    localparam integer N     = 400;

    reg clk = 1'b0;
    reg rst = 1'b1;
    always #2.5 clk = ~clk;

    reg  [DW-1:0] wr_data = 0;
    reg           wr_en   = 1'b0;
    wire          wr_busy, wr_almost;
    wire [DW-1:0] rd_data;
    reg           rd_en   = 1'b0;
    wire          rd_busy;

    sync_fifo #(
        .DATA_WIDTH(DW), .FIFO_DEPTH(DEPTH), .MEMORY_TYPE("distributed")
    ) dut (
        .clk(clk), .rst(rst),
        .wr_en(wr_en), .wr_data(wr_data), .wr_busy(wr_busy), .wr_almost(wr_almost),
        .rd_en(rd_en), .rd_data(rd_data), .rd_busy(rd_busy)
    );

    integer sent = 0, got = 0, errors = 0, tick = 0;
    integer max_level = 0, level = 0;

    // The reader idles for the first 64 ticks so the FIFO is driven hard against
    // FULL before anything is drained.
    function automatic reader_on(input integer t);
        reader_on = (t > 64) && (((t * 2654435761) >> 11) & 32'h3) != 0;
    endfunction

    initial begin
        repeat (12) @(negedge clk);
        rst = 1'b0;

        while (sent < N || got < N) begin
            @(negedge clk);
            tick = tick + 1;

            wr_en = 1'b0;
            if (!wr_busy && sent < N) begin
                wr_data = sent[DW-1:0];
                wr_en   = 1'b1;
                sent    = sent + 1;
                level   = level + 1;
            end

            rd_en = 1'b0;
            if (!rd_busy && reader_on(tick) && got < N) begin
                if (rd_data !== got[DW-1:0]) begin
                    errors = errors + 1;
                    if (errors < 8)
                        $display("  FAIL word %0d: got %h want %h", got, rd_data, got[DW-1:0]);
                end
                got   = got + 1;
                rd_en = 1'b1;
                level = level - 1;
            end

            // Occupancy is REPORTED, never asserted on: fwft holds words in
            // output stages beyond FIFO_WRITE_DEPTH, so the real cell legitimately
            // shows more in flight than the array is deep.
            if (level > max_level) max_level = level;
        end

        $display("========================================");
        $display("  peak occupancy %0d of %0d", max_level, DEPTH);
        if (errors == 0) $display("  PASS -- %0d words through, 0 errors", got);
        else             $display("  FAIL -- %0d words through, %0d errors", got, errors);
        $display("========================================");
        $finish;
    end

    initial begin
        #500000;
        $display("  FAIL WATCHDOG -- sent %0d got %0d wr_busy=%b rd_busy=%b",
                 sent, got, wr_busy, rd_busy);
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire

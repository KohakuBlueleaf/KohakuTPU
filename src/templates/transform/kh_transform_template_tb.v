// Bench for the transform-slot template: drives entries the way
// mag_mem_port does (pushed beats, no handshake) and checks the three slot
// rules -- fixed 4-word output, done-pulse timing, stability until next start.
`timescale 1ns / 1ps
`default_nettype none

module kh_transform_template_tb;
    localparam integer DW = 256;
    localparam integer NB = 4;

    reg clk = 0, rst = 1;
    always #2 begin
        clk = ~clk;
    end

    reg           start = 0, blay = 0, beat_v = 0;
    reg  [DW-1:0] beat;
    wire          done, need_beat;
    wire [DW-1:0] w0, w1, w2, w3;

    kh_transform_template #(.DATA_W(DW), .IN_BEATS(NB)) dut (
        .clk(clk), .rst(rst),
        .start(start), .b_layout(blay),
        .beat(beat), .beat_valid(beat_v), .need_beat(need_beat),
        .done(done), .word0(w0), .word1(w1), .word2(w2), .word3(w3)
    );

    integer errors = 0;
    integer checks = 0;
    task chk(input cond, input [511:0] name);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                $display("%0t ERROR %0s", $time, name);
            end
        end
    endtask

    // done is a one-cycle pulse and may fire during the TB's own gap delays,
    // so a wait-for-level would miss it; latch it per entry instead.
    reg done_seen;
    always @(posedge clk) begin
        if (start) begin
            done_seen <= 1'b0;
        end
        else if (done) begin
            done_seen <= 1'b1;
        end
    end

    reg [DW-1:0] sent [0:NB-1];
    integer i, gap;
    reg [DW-1:0] h0, h1, h2, h3;

    task entry(input integer seed, input integer spacing);
        begin
            @(negedge clk);
            start = 1;
            blay  = seed[0];
            @(negedge clk);
            start = 0;
            for (i = 0; i < NB; i = i + 1) begin
                sent[i] = {8{seed[31:0] + i}};
                beat    = sent[i];
                beat_v  = 1;
                @(negedge clk);
                beat_v = 0;
                // Beats need not be back to back; the port stalls on AXI.
                for (gap = 0; gap < spacing; gap = gap + 1) begin
                    @(negedge clk);
                end
            end
            gap = 0;
            while (!done_seen && gap < 20) begin gap = gap + 1; @(negedge clk); end
            chk(done_seen, "done fired within 20 cycles of the last beat");
            chk(w0 == sent[0] && w1 == sent[1] && w2 == sent[2] && w3 == sent[3],
                "identity: words equal the entry's beats in order");
            h0 = w0; h1 = w1; h2 = w2; h3 = w3;
            @(negedge clk);
            chk(!done, "done is a one-cycle pulse");
            repeat (5) @(negedge clk);
            chk(w0 == h0 && w1 == h1 && w2 == h2 && w3 == h3,
                "words stable after done until the next start");
        end
    endtask

    initial begin
        repeat (4) @(negedge clk);
        rst = 0;
        repeat (2) @(negedge clk);

        entry(32'h1000, 0);
        entry(32'hA500, 3);
        entry(32'h0BE0, 1);

        if (errors == 0) begin
            $display("PASS kh_transform_template_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL kh_transform_template_tb: %0d of %0d checks failed",
                     errors, checks);
        end
        $finish;
    end
endmodule

`default_nettype wire

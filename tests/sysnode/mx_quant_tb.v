// mx_quant against the driver's model, which is the spec.
//
// The bench computes nothing. It reads FP16 entries, runs them through the
// quantiser, and dumps what came out; kohakutpu.mxfp7 says what SHOULD have
// come out and the comparison happens there. Writing a second E5M3 model in
// Verilog would only test that two hand-written models agree, and they would
// agree because the same person wrote both.
//
//   quant_in.hex    one 256-bit word per line, 8 per entry, FP16 lane-major
//   quant_out.hex   per entry: 4 operand words then the 4 scale fields
//
// Driven by driver/run_quant_check.py.

`default_nettype none
`timescale 1ns/1ps

module mx_quant_tb;

    localparam integer ENTRIES = 256;

    reg clk = 0, rst = 1;
    always begin
        #2 clk = ~clk;
    end

    reg          start = 0, b_layout = 0, beat_valid = 0;
    reg  [255:0] beat = 0;
    wire         need_beat, done;
    wire [255:0] w0, w1, w2, w3;

    mx_quant u_dut (
        .clk(clk), .rst(rst),
        .start(start), .b_layout(b_layout),
        .beat(beat), .beat_valid(beat_valid), .need_beat(need_beat),
        .done(done), .word0(w0), .word1(w1), .word2(w2), .word3(w3)
    );

    reg [255:0] src  [0:ENTRIES*8-1];
    reg [255:0] outw [0:ENTRIES*5-1];

    integer e, b, t;

    initial begin
        for (e = 0; e < ENTRIES*8; e = e + 1) begin
            src[e] = 256'd0;
        end
        for (e = 0; e < ENTRIES*5; e = e + 1) begin
            outw[e] = 256'd0;
        end
        $readmemh("quant_in.hex", src);

        repeat (4) @(negedge clk);
        rst = 0;
        @(negedge clk);

        for (e = 0; e < ENTRIES; e = e + 1) begin
            // B packing on odd entries, so both layouts are exercised
            b_layout <= e[0];
            start <= 1'b1;
            @(negedge clk);
            start <= 1'b0;

            for (b = 0; b < 8; b = b + 1) begin
                beat <= src[e*8 + b];
                beat_valid <= 1'b1;
                @(negedge clk);
            end
            beat_valid <= 1'b0;

            t = 0;
            while (!done && t < 100) begin @(negedge clk); t = t + 1; end
            if (t >= 100) begin
                $display("  FAIL entry %0d never completed", e);
                $finish;
            end

            outw[e*5 + 0] = w0;
            outw[e*5 + 1] = w1;
            outw[e*5 + 2] = w2;
            outw[e*5 + 3] = w3;
            outw[e*5 + 4] = {248'd0, b_layout, 7'd0};   // echo the layout used
            @(negedge clk);
        end

        $writememh("quant_out.hex", outw);
        $display("  QUANT-DONE %0d entries", ENTRIES);
        $finish;
    end

    initial begin
        #20000000;
        $display("  FAIL watchdog");
        $finish;
    end

endmodule

`default_nettype wire

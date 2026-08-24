// Philox-4x32-10 against the Random123 known-answer vectors.
//
// These are the published KATs, not values read back out of this
// implementation, so they check the CONSTANTS and the key-bump schedule --
// the two things a plausible-looking Philox gets wrong silently. A generator
// that is self-consistent but disagrees with everyone else is useless the
// moment a result has to be reproduced off-device.

`default_nettype none
`timescale 1ns/1ps

module mm_prng_tb;
    reg clk = 0, rst = 1;
    always begin
        #2 clk = ~clk;
    end

    reg          start;
    reg  [63:0]  key_in;
    reg  [127:0] ctr_in;
    wire         busy, out_valid;
    wire [127:0] out;

    mm_prng #(.ROUNDS(10)) dut (
        .clk(clk), .rst(rst),
        .start(start), .key_in(key_in), .ctr_in(ctr_in),
        .busy(busy), .out_valid(out_valid), .out(out)
    );

    integer errors = 0, checks = 0;
    integer spin;

    task run_kat(input [127:0] ctr, input [63:0] key, input [127:0] want,
                 input [255:0] what);
        begin
            @(negedge clk);
            ctr_in = ctr; key_in = key; start = 1'b1;
            @(negedge clk);
            start = 1'b0;
            spin = 0;
            while (!out_valid && spin < 200) begin
                spin = spin + 1;
                @(negedge clk);
            end
            checks = checks + 1;
            if (spin >= 200) begin
                errors = errors + 1;
                $display("  FAIL %0s: never completed", what);
            end else if (out !== want) begin
                errors = errors + 1;
                $display("  FAIL %0s", what);
                $display("      got  %08h %08h %08h %08h",
                         out[31:0], out[63:32], out[95:64], out[127:96]);
                $display("      want %08h %08h %08h %08h",
                         want[31:0], want[63:32], want[95:64], want[127:96]);
            end
        end
    endtask

    task gen(input [127:0] ctr, input [63:0] key);
        begin
            @(negedge clk);
            ctr_in = ctr; key_in = key; start = 1'b1;
            @(negedge clk);
            start = 1'b0;
            spin = 0;
            while (!out_valid && spin < 200) begin
                spin = spin + 1;
                @(negedge clk);
            end
        end
    endtask

    integer i;
    reg [127:0] prev;

    initial begin
        start = 0; key_in = 0; ctr_in = 0;
        repeat (6) @(negedge clk);
        rst = 0;
        repeat (2) @(negedge clk);

        $display("--- 1. Random123 known-answer vectors ---");
        // ctr and key are listed word 0 first; `out` packs word 0 in the low bits.
        run_kat(128'd0, 64'd0,
                {32'h9b00dbd8, 32'hbc57ac4c, 32'he169c58d, 32'h6627e8d5},
                "philox4x32-10 zeros");

        run_kat({4{32'hffffffff}}, {2{32'hffffffff}},
                {32'h6d5451fd, 32'ha20bc7c6, 32'h41c83b0e, 32'h408f276d},
                "philox4x32-10 ones");

        run_kat({32'h03707344, 32'h13198a2e, 32'h85a308d3, 32'h243f6a88},
                {32'h299f31d0, 32'ha4093822},
                {32'h24126ea1, 32'h5001e420, 32'h94fdcceb, 32'hd16cfe09},
                "philox4x32-10 pi digits");

        $display("--- 2. distinct counters give distinct streams ---");
        prev = 128'd0;
        for (i = 0; i < 8; i = i + 1) begin
            gen({96'd0, i[31:0]}, 64'h0123456789abcdef);
            checks = checks + 1;
            if (i > 0 && out === prev) begin
                errors = errors + 1;
                $display("  FAIL counter %0d repeated the previous output", i);
            end
            prev = out;
        end

        $display("--- 3. out_valid is one pulse, and busy brackets it ---");
        @(negedge clk);
        ctr_in = 128'd7; key_in = 64'd9; start = 1'b1;
        @(negedge clk); start = 1'b0;
        spin = 0;
        while (!out_valid && spin < 200) begin
            spin = spin + 1;
            @(negedge clk);
        end
        @(negedge clk);
        checks = checks + 1;
        if (out_valid) begin
            errors = errors + 1;
            $display("  FAIL out_valid held for more than one cycle");
        end
        checks = checks + 1;
        if (busy) begin
            errors = errors + 1;
            $display("  FAIL busy did not clear after completion");
        end

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
        #200000;
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire

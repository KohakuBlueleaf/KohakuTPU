// Prints the first values of $random(seed) so the two simulators can be
// compared directly. Nothing is checked here -- the output IS the result.
//
// Written because vec_cvt reported 13912 errors under the C++ simulator whose
// failing inputs decoded as 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFC, 0xFFFFFFF8 ...
// -- a sequence, not a random draw. This isolates the generator from the DUT.

`timescale 1ns/1ps

module vlt_random_probe;
    integer seed = 32'h1234_5678;
    integer i;
    reg [31:0] v;
    integer ones = 0;

    initial begin
        $display("--- first 16 of $random(seed), seed=%h ---", 32'h1234_5678);
        for (i = 0; i < 16; i = i + 1) begin
            v = {$random(seed)};
            $display("  [%0d] %h", i, v);
        end

        // A healthy 32-bit generator averages 16 set bits per draw. A sequence
        // saturating towards all-ones shows up here as a number near 32.
        ones = 0;
        for (i = 0; i < 1000; i = i + 1) begin
            v = {$random(seed)};
            ones = ones + $countones(v);
        end
        $display("--- mean set bits over 1000 draws: %0d.%03d (healthy ~16) ---",
                 ones / 1000, ((ones % 1000) * 1000) / 1000);
        $finish;
    end
endmodule

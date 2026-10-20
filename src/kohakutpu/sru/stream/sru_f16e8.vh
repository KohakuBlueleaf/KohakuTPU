// FP16 -> E8M15, exact. A subnormal (m * 2^-24, lead one at p) normalises to
// exponent field p + 103; inf and NaN keep class and payload.
function [23:0] f16e8;
    input [15:0] h;
    integer    fp;
    reg [3:0]  lp;
    reg [14:0] mn;
    begin
        lp = 4'd0;
        for (fp = 0; fp < 10; fp = fp + 1) begin
            if (h[fp]) lp = fp;
        end
        mn = {h[9:0], 5'd0} << (4'd10 - lp);
        if (h[14:10] == 5'd31) begin
            f16e8 = {h[15], 8'hFF, h[9:0], 5'd0};
        end else if (h[14:10] != 5'd0) begin
            f16e8 = {h[15], {3'd0, h[14:10]} + 8'd112, h[9:0], 5'd0};
        end else if (h[9:0] != 10'd0) begin
            f16e8 = {h[15], {4'd0, lp} + 8'd103, mn};
        end else begin
            f16e8 = {h[15], 23'd0};
        end
    end
endfunction

// The clusters' accumulator FP16 -> E8M15: exponent 0 is a signed zero, 1..31
// ordinary binades (no subnormal, inf or NaN).
function [23:0] f16e8_acc;
    input [15:0] h;
    begin
        f16e8_acc = (h[14:10] == 5'd0) ? {h[15], 23'd0}
                                       : {h[15], {3'd0, h[14:10]} + 8'd112, h[9:0], 5'd0};
    end
endfunction

// IEEE (ieee != 0) or the accumulator form, by a constant.
function [23:0] f16e8k;
    input [15:0] h;
    input integer ieee;
    begin
        f16e8k = (ieee != 0) ? f16e8(h) : f16e8_acc(h);
    end
endfunction

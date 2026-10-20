// kht_score_tb -- the scoreboard against a reference array: random sets, clears
// and checks every cycle, `hit`, `woke` and `any` compared each cycle.

`timescale 1ns/1ps
`default_nettype none

module kht_score_tb;
    localparam integer WAVES = 16;
    localparam integer WID   = 4;

    reg clk = 1'b0, resetn = 1'b0;
    always #2 clk = ~clk;

    reg  [WID-1:0] chk_wave, set_wave, clr_wave;
    reg  [4:0]     chk_rs1, chk_rs2, chk_rd, set_rd, clr_rd;
    reg  [2:0]     chk_use;
    reg            set_v, clr_v;
    wire           hit, any;
    wire [WAVES-1:0] woke, wpend;

    kht_score #(.WAVES(WAVES)) dut (
        .clk(clk), .resetn(resetn),
        .chk_wave(chk_wave), .chk_rs1(chk_rs1), .chk_rs2(chk_rs2), .chk_rd(chk_rd),
        .chk_use(chk_use), .hit(hit),
        .set_v(set_v), .set_wave(set_wave), .set_rd(set_rd),
        .clr_v(clr_v), .clr_wave(clr_wave), .clr_rd(clr_rd),
        .woke(woke), .wpend(wpend), .any(any)
    );

    reg [31:0] ref_p [0:WAVES-1];
    integer errors = 0, checks = 0, hits = 0, i, n;
    reg want_hit, want_any;

    initial begin
        for (i = 0; i < WAVES; i = i + 1) ref_p[i] = 32'd0;
        set_v = 0; clr_v = 0; chk_use = 0;
        chk_wave = 0; chk_rs1 = 0; chk_rs2 = 0; chk_rd = 0;
        set_wave = 0; set_rd = 0; clr_wave = 0; clr_rd = 0;
        repeat (4) @(posedge clk);
        resetn = 1;
        @(posedge clk);

        for (n = 0; n < 40000; n = n + 1) begin
            #1;
            // Mostly few waves and few registers, so sets and checks collide; a
            // quarter of the time any of the 32, so every bit of a row is used.
            chk_wave = {$random} % 4;
            chk_rs1  = (($random & 3) == 0) ? $random : {$random} % 6;
            chk_rs2  = (($random & 3) == 0) ? $random : {$random} % 6;
            chk_rd   = (($random & 3) == 0) ? $random : {$random} % 6;
            chk_use  = $random;
            set_v    = ({$random} % 3) == 0;
            set_wave = {$random} % 4;
            set_rd   = (($random & 3) == 0) ? $random : {$random} % 6;
            clr_v    = ({$random} % 3) == 0;
            clr_wave = {$random} % 4;
            clr_rd   = (($random & 3) == 0) ? $random : {$random} % 6;
            // A correct core never sets a pending bit or sets and clears one bit
            // together; the stimulus obeys the same rule the asserts state.
            if (set_v && ref_p[set_wave][set_rd]) set_v = 0;
            if (set_v && clr_v && (set_wave == clr_wave) && (set_rd == clr_rd)) clr_v = 0;
            #0.5;
            want_hit = (chk_use[0] && chk_rs1 != 0 && ref_p[chk_wave][chk_rs1])
                    || (chk_use[1] && chk_rs2 != 0 && ref_p[chk_wave][chk_rs2])
                    || (chk_use[2] && chk_rd  != 0 && ref_p[chk_wave][chk_rd]);
            checks = checks + 2;
            hits = hits + want_hit;
            if (hit !== want_hit) begin
                errors = errors + 1;
                if (errors < 10) $display("  FAIL hit %b want %b (wave %0d rs %0d/%0d rd %0d use %b)",
                                          hit, want_hit, chk_wave, chk_rs1, chk_rs2, chk_rd, chk_use);
            end
            for (i = 0; i < WAVES; i = i + 1) begin
                if (woke[i] !== (clr_v && clr_wave == i)) begin
                    errors = errors + 1;
                    if (errors < 10) $display("  FAIL woke[%0d] %b", i, woke[i]);
                end
                if (wpend[i] !== (ref_p[i] != 0)) begin
                    errors = errors + 1;
                    if (errors < 10) $display("  FAIL wpend[%0d] %b", i, wpend[i]);
                end
            end
            @(posedge clk);
            if (clr_v) ref_p[clr_wave][clr_rd] = 1'b0;
            if (set_v && set_rd != 0) ref_p[set_wave][set_rd] = 1'b1;
            #1;
            want_any = 0;
            for (i = 0; i < WAVES; i = i + 1) if (ref_p[i] != 0) want_any = 1;
            @(posedge clk);
            #0.5;
            checks = checks + 1;
            if (any !== want_any) begin
                errors = errors + 1;
                if (errors < 10) $display("  FAIL any %b want %b", any, want_any);
            end
            set_v = 0; clr_v = 0;
        end

        $display("    %0d checks found a pending register", hits);
        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end
endmodule

`default_nettype wire

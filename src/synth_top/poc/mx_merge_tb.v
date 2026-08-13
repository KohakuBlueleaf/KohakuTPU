// Minimal directed test for fix A's merge: drives mx_acu_fp directly, so a
// failure is the merge and nothing else.

// Scales 0xA0A0A0A0 (E=SBIAS=20, M=0) with anchor=40 give mm=64 and
// exp = 20+20-40-6 = -6, so each lane's answer is exactly p0 + p1.

`default_nettype none
`timescale 1ns/1ps

module mx_merge_tb;
    localparam integer MW = 14;
    localparam [31:0] S1 = 32'hA0A0A0A0;   // scale 2^0 on all four lanes
    localparam [7:0]  ANCHOR = 8'd40;

    reg clk = 0, clk2x = 0, rst = 1;
    always #2 clk   = ~clk;
    always #1 clk2x = ~clk2x;

    reg  [383:0] part_in, part_in2;
    reg  [2:0]   op;
    reg  [7:0]   tile_addr;
    reg          cmd_valid, single;
    wire [255:0] emit_out;
    wire         emit_valid, busy;

    localparam [2:0] OP_LOAD = 3'd1, OP_EMIT = 3'd5;

    // Per-case scales: with equal scales e1==e2 and sh is always 0, so the
    // alignment shift -- the whole point of the merge -- goes untested.
    reg [31:0] sa_r = S1, sb_r = S1, sa2_r = S1, sb2_r = S1;

    mx_acu_fp #(.DEPTH(256), .ACC_MW(MW), .TILE_PRIM("block"), .DUAL(1)) dut (
        .clk(clk), .clk2x(clk2x), .rst(rst), .en(1'b1),
        .part_in(part_in), .sa(sa_r), .sb(sb_r), .anchor(ANCHOR),
        .part_in2(part_in2), .sa2(sa2_r), .sb2(sb2_r), .single(single),
        .op(op), .tile_addr(tile_addr), .cmd_valid(cmd_valid),
        .peer_in({16*(MW+8){1'b0}}), .peer_out(), .peer_valid(),
        .emit_out(emit_out), .emit_valid(emit_valid), .busy(busy)
    );

    integer errors = 0, checks = 0;

    // Lane L lives in 48-bit word ((L/8)*4 + (L%4)); even rows take the low
    // 19-bit field, odd rows the high 22-bit.
    function [383:0] pack_lane(input integer L, input integer v);
        integer gs, gt, F;
        begin
            gs = L / 4; gt = L % 4;
            F  = ((gs/2)*4 + gt) * 48;
            pack_lane = 384'd0;
            if (gs % 2 == 0) pack_lane[F +: 19]    = v[18:0];
            else             pack_lane[F+19 +: 22] = v[21:0];
        end
    endfunction

    function real fp16_to_real(input [15:0] h);
        integer e, m; real s;
        begin
            e = h[14:10]; m = h[9:0];
            s = h[15] ? -1.0 : 1.0;
            if (e == 0)       fp16_to_real = s * (m / 1024.0) * (2.0 ** -14);
            else if (e == 31) fp16_to_real = s * 1.0e30;
            else              fp16_to_real = s * (1.0 + m / 1024.0) * (2.0 ** (e - 15));
        end
    endfunction

    task do_case(input integer L, input integer p0, input integer p1,
                 input sing, input [255:0] label);
        real got, want;
        begin
            @(negedge clk);
            part_in2  = pack_lane(L, p0);      // phase 0 pairs with sa2/sb2
            part_in   = pack_lane(L, p1);      // phase 1 pairs with sa/sb
            single    = sing;
            op        = OP_LOAD;
            tile_addr = 8'd0;
            cmd_valid = 1'b1;
            @(negedge clk); cmd_valid = 1'b0;

            wait (!busy); repeat (4) @(negedge clk);

            @(negedge clk);
            op = OP_EMIT; tile_addr = 8'd0; cmd_valid = 1'b1; single = 1'b0;
            @(negedge clk); cmd_valid = 1'b0;
            wait (emit_valid); @(posedge clk);

            got  = fp16_to_real(emit_out[(L%16)*16 +: 16]);
            want = sing ? p0 : (p0 + p1);
            checks = checks + 1;
            if (got != want) begin
                errors = errors + 1;
                $display("  FAIL %0s lane %0d: p0=%0d p1=%0d sing=%0b got %0f want %0f",
                         label, L, p0, p1, sing, got, want);
            end
            wait (!busy); repeat (4) @(negedge clk);
        end
    endtask

    // Back-to-back LOADs to different tiles, no gap: the real sweep issues a
    // command per cycle, which do_case's wait(!busy) never exercises.
    task btb(input integer n);
        integer i;
        real got, want;
        begin
            for (i = 0; i < n; i = i + 1) begin
                @(negedge clk);
                part_in2  = pack_lane(0, 100 + i);
                part_in   = pack_lane(0, 10 * (i + 1));
                single    = 1'b0;
                op        = OP_LOAD;
                tile_addr = i[7:0];
                cmd_valid = 1'b1;
            end
            @(negedge clk); cmd_valid = 1'b0;
            wait (!busy); repeat (8) @(negedge clk);

            for (i = 0; i < n; i = i + 1) begin
                @(negedge clk);
                op = OP_EMIT; tile_addr = i[7:0]; cmd_valid = 1'b1;
                @(negedge clk); cmd_valid = 1'b0;
                wait (emit_valid); @(posedge clk);
                got  = fp16_to_real(emit_out[0 +: 16]);
                want = (100 + i) + 10 * (i + 1);
                checks = checks + 1;
                if (got != want) begin
                    errors = errors + 1;
                    $display("  FAIL back-to-back tile %0d: got %0f want %0f",
                             i, got, want);
                end
                wait (!busy); repeat (4) @(negedge clk);
            end
        end
    endtask

    // LOAD then ADD into the same tile, with `single` differing between them --
    // what an odd-NK sweep does across its two K iterations.
    localparam [2:0] OP_ADD = 3'd2;
    task load_then_add(input integer p0a, input integer p1a, input sa_sing,
                       input integer p0b, input integer p1b, input sb_sing);
        real got, want;
        begin
            @(negedge clk);
            part_in2 = pack_lane(0, p0a); part_in = pack_lane(0, p1a);
            single = sa_sing; op = OP_LOAD; tile_addr = 8'd7; cmd_valid = 1'b1;
            @(negedge clk); cmd_valid = 1'b0;
            wait (!busy); repeat (4) @(negedge clk);

            @(negedge clk);
            part_in2 = pack_lane(0, p0b); part_in = pack_lane(0, p1b);
            single = sb_sing; op = OP_ADD; tile_addr = 8'd7; cmd_valid = 1'b1;
            @(negedge clk); cmd_valid = 1'b0;
            wait (!busy); repeat (4) @(negedge clk);

            @(negedge clk);
            op = OP_EMIT; tile_addr = 8'd7; cmd_valid = 1'b1; single = 1'b0;
            @(negedge clk); cmd_valid = 1'b0;
            wait (emit_valid); @(posedge clk);

            got  = fp16_to_real(emit_out[0 +: 16]);
            want = (sa_sing ? p0a : p0a + p1a) + (sb_sing ? p0b : p0b + p1b);
            checks = checks + 1;
            if (got != want)begin
                errors = errors + 1;
                $display("  FAIL load+add: got %0f want %0f", got, want);
            end
            wait (!busy); repeat (4) @(negedge clk);
        end
    endtask

    // Both partials at their own exponent: value = p0*2^(ea2+eb2-40)
    // + p1*2^(ea+eb-40), since M=0 makes mm=64 and cancels the -6.
    task do_exp(input integer p0, input integer ea2b,
                input integer p1, input integer eab);
        real got, want;
        begin
            @(negedge clk);
            sa2_r = {4{ea2b[4:0], 3'b000}};  sb2_r = {4{5'd20, 3'b000}};
            sa_r  = {4{eab[4:0],  3'b000}};  sb_r  = {4{5'd20, 3'b000}};
            part_in2 = pack_lane(0, p0); part_in = pack_lane(0, p1);
            single = 1'b0; op = OP_LOAD; tile_addr = 8'd9; cmd_valid = 1'b1;
            @(negedge clk); cmd_valid = 1'b0;
            wait (!busy); repeat (4) @(negedge clk);

            @(negedge clk);
            op = OP_EMIT; tile_addr = 8'd9; cmd_valid = 1'b1;
            @(negedge clk); cmd_valid = 1'b0;
            wait (emit_valid); @(posedge clk);

            got  = fp16_to_real(emit_out[0 +: 16]);
            want = p0 * (2.0 ** (ea2b + 20 - 40)) + p1 * (2.0 ** (eab + 20 - 40));
            checks = checks + 1;
            if (got != want) begin
                errors = errors + 1;
                $display("  FAIL exp p0=%0d@E%0d p1=%0d@E%0d: got %0f want %0f",
                         p0, ea2b, p1, eab, got, want);
            end
            wait (!busy); repeat (4) @(negedge clk);
            sa_r = S1; sb_r = S1; sa2_r = S1; sb2_r = S1;
        end
    endtask

    initial begin
        part_in = 0; part_in2 = 0; op = 0; tile_addr = 0;
        cmd_valid = 0; single = 0;
        repeat (20) @(negedge clk); rst = 0; repeat (4) @(negedge clk);

        do_case(0,  1000,   200, 1'b0, "same sign");
        do_case(0,   200,  1000, 1'b0, "same sign, p1 larger");
        do_case(0,  1000,  -200, 1'b0, "opposite sign");
        do_case(0,  -200,  1000, 1'b0, "opposite, p0 smaller");
        // Near-total cancellation: the case pre-summing creates and the
        // per-block reference path never sees.
        do_case(0,  1000,  -999, 1'b0, "cancellation");
        do_case(0,  1000, -1000, 1'b0, "exact cancellation");
        do_case(0,  1234,     0, 1'b1, "single keeps p0");
        do_case(1,  1000,   200, 1'b0, "hi field lane");
        do_case(5,  1000,  -300, 1'b0, "hi field, opposite");
        do_case(15,  777,   111, 1'b0, "last lane");
        btb(4);
        load_then_add(1000,  200, 1'b0,  300,   50, 1'b0);
        load_then_add(1000,  200, 1'b0,  300,    0, 1'b1);   // odd NK tail
        load_then_add( 500, -100, 1'b0, -200,   80, 1'b0);

        // The untested axis: the two partials at DIFFERENT exponents.
        do_exp( 100, 20,  100, 20);      // sh = 0, the only case tested before
        do_exp( 100, 20,  100, 21);      // p1 one octave up
        do_exp( 100, 21,  100, 20);      // p0 one octave up
        do_exp( 100, 20,  100, 23);      // sh = 3
        do_exp( 100, 23,  100, 20);
        do_exp( 100, 20, -100, 21);      // opposite signs, shifted
        do_exp(-100, 23,  100, 20);

        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $finish;
    end

    initial begin
        #2000000;
        $display("  FAIL -- watchdog");
        $finish;
    end
endmodule

`default_nettype wire

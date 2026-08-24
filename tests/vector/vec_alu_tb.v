// The vector ALU against real arithmetic.
//
// Two kinds of claim are being tested and they are checked differently:
//
//   EXACT    mov/neg/abs/max/min/select, multiplication by a power of two,
//            exp2 of an integer, log2/inv/rsqrt of a power of two, and the
//            cancellation a*b - a*b. Equalities on the BITS; a tolerance would
//            hide what the design claims.
//   ROUNDED  the FMA and the four seeds, against a double-precision reference
//            in ULPS OF THE RESULT, reported as a measured maximum rather than
//            pass/fail. Checks the < 1 ulp claim in
//            docs/compute/vector-core.md s4.2.
//
// The alignment sweep walks the exponent difference across every
// barrel-shifter position, which is the only way to reach s == 0 (where P[47]
// is a value bit, not a sign bit) and the bypass at s < 0. Neither is reachable
// from random operands with any useful probability.
//
// Everything is STREAMED at one instruction per cycle through a scoreboard, so
// this also proves II = 1 -- a design needing gaps between instructions passes
// a one-at-a-time bench and fails in a core.
//
// MX_MODEL 1 = vec_dsp behavioural, 0 = real DSP48E2 (needs -L unisims_ver).

`ifndef MX_MODEL
`define MX_MODEL 1
`endif

`default_nettype none
`timescale 1ns/1ps

module vec_alu_tb;

    localparam integer MODEL = `MX_MODEL;
    localparam integer MAXT  = 60000;

    // Must match vec_alu.v; nothing checks that they do.
    localparam [4:0] OP_MOV   = 5'd0,  OP_NEG   = 5'd1,  OP_ABS   = 5'd2;
    localparam [4:0] OP_ADD   = 5'd3,  OP_SUB   = 5'd4,  OP_MUL   = 5'd5;
    localparam [4:0] OP_FMA   = 5'd6,  OP_FNMA  = 5'd7;
    localparam [4:0] OP_MAX   = 5'd8,  OP_MIN   = 5'd9,  OP_SEL   = 5'd10;
    localparam [4:0] OP_CMPLT = 5'd11, OP_CMPGT = 5'd12, OP_CMPEQ = 5'd13;
    localparam [4:0] OP_EXP2  = 5'd16, OP_LOG2  = 5'd17;
    localparam [4:0] OP_INV   = 5'd18, OP_RSQRT = 5'd19;

    localparam [23:0] E8_ONE  = 24'h3F8000;
    localparam [23:0] E8_ZERO = 24'h000000;
    localparam [23:0] E8_INF  = 24'h7F8000;
    localparam [23:0] E8_NINF = 24'hFF8000;
    localparam [23:0] E8_NAN  = 24'h7FC000;

    reg         clk = 1'b0, rst = 1'b1;
    reg         in_valid = 1'b0;
    reg  [4:0]  op = 5'd0;
    reg  [23:0] a = 24'd0, b = 24'd0, c = 24'd0;
    wire        out_valid, out_pred;
    wire [23:0] out;

    always begin
        #1 clk = ~clk;
    end

    vec_alu #(.MODEL(MODEL)) dut (
        .clk(clk), .rst(rst), .in_valid(in_valid), .op(op),
        .a(a), .b(b), .c(c),
        .out_valid(out_valid), .out(out), .out_pred(out_pred)
    );

    // =====================================================================
    // format helpers
    // =====================================================================
    function real e8_real(input [23:0] f);
        real m;
        integer e;
        begin
            if (f[22:15] == 8'd0) begin
                e8_real = 0.0;
            end
            else if (f[22:15] == 8'hFF) begin  // inf stand-in
                e8_real = 1.0e300;
            end
            else begin
                m = 1.0 + $itor({1'b0, f[14:0]}) / 32768.0;
                e = f[22:15] - 127;
                e8_real = m * (2.0 ** e);
                if (f[23]) begin
                    e8_real = -e8_real;
                end
            end
        end
    endfunction

    function [23:0] e8_of(input real v);
        real m;
        integer e, fr;
        reg s;
        // SIZED. `{s, (e+127), fr[14:0]}` with an integer in the middle
        // contributes 32 bits, not 8, and pushes the sign clean out of the word.
        reg [7:0]  eb;
        reg [14:0] fb;
        begin
            if (v == 0.0) begin
                e8_of = 24'd0;
            end
            else begin
                s = (v < 0.0);
                m = s ? -v : v;
                e = 0;
                while (m >= 2.0) begin m = m / 2.0; e = e + 1; end
                while (m <  1.0) begin m = m * 2.0; e = e - 1; end
                fr = $rtoi((m - 1.0) * 32768.0 + 0.5);
                if (fr >= 32768) begin fr = 0; e = e + 1; end
                eb = e + 127;
                fb = fr;
                if (e > 127) begin
                    e8_of = {s, 8'hFF, 15'd0};
                end
                else if (e < -126) begin
                    e8_of = {s, 23'd0};
                end
                else begin
                    e8_of = {s, eb, fb};
                end
            end
        end
    endfunction

    // ulp of a result, from its own exponent
    function real ulp_of(input [23:0] f);
        begin
            if (f[22:15] == 8'd0) begin  // smallest normal ulp
                ulp_of = 2.0 ** (-141);
            end
            else begin
                ulp_of = 2.0 ** ($itor(f[22:15]) - 142.0);
            end
        end
    endfunction

    // =====================================================================
    // scoreboard -- everything is streamed, so expectations are queued
    // =====================================================================
    // K_ABS is for log2 alone, and it is not a weaker check: log2 needs BOTH
    // bounds because neither works alone.
    //
    //   near x = 1   the RESULT approaches zero while its absolute error does
    //                not, so one ulp of the result shrinks without bound.
    //   at large |x| the result is a few decades wide, so its ulp is ~2^-10 and
    //                an absolute bound of 2^-18 is far below one ulp.
    //
    // A sample passes within 0.99 ulp OR within 2^-18 absolute, and the reported
    // figure is the worst ratio to whichever limit applied -- 1.0 is exactly at
    // the limit.
    localparam integer K_REAL = 0, K_BITS = 1, K_ABS = 2;

    reg [23:0] q_bits [0:MAXT-1];
    real       q_val  [0:MAXT-1];
    real       q_tol  [0:MAXT-1];      // in ulps
    reg [1:0]  q_kind [0:MAXT-1];
    reg [7:0]  q_grp  [0:MAXT-1];

    integer nsub = 0, nret = 0, errors = 0;
    real    grp_max [0:31];            // worst ulp error seen per group
    integer grp_n   [0:31];
    reg [8*14-1:0] grp_nm [0:31];

    task push_bits(input [23:0] want, input [7:0] g);
        begin
            q_kind[nsub] = K_BITS; q_bits[nsub] = want; q_grp[nsub] = g;
            q_val[nsub] = 0.0; q_tol[nsub] = 0.0;
            nsub = nsub + 1;
        end
    endtask

    task push_real(input real want, input real tol_ulp, input [7:0] g);
        begin
            q_kind[nsub] = K_REAL; q_val[nsub] = want; q_tol[nsub] = tol_ulp;
            q_grp[nsub] = g; q_bits[nsub] = 24'd0;
            nsub = nsub + 1;
        end
    endtask

    task push_abs(input real want, input real tol_abs, input [7:0] g);
        begin
            q_kind[nsub] = K_ABS; q_val[nsub] = want; q_tol[nsub] = tol_abs;
            q_grp[nsub] = g; q_bits[nsub] = 24'd0;
            nsub = nsub + 1;
        end
    endtask

    // One instruction per cycle, driven on the negative edge.
    task drv(input [4:0] o, input [23:0] xa, input [23:0] xb, input [23:0] xc);
        begin
            @(negedge clk);
            in_valid = 1'b1; op = o; a = xa; b = xb; c = xc;
        end
    endtask

    task idle(input integer n);
        integer i;
        begin
            for (i = 0; i < n; i = i + 1) begin
                @(negedge clk); in_valid = 1'b0;
            end
        end
    endtask

    // ---- latency probe ---------------------------------------------------
    // Measures when `out` ITSELF settles, independently of out_valid, and then
    // checks out_valid against it. Repipelining vec_alu means re-deriving a
    // dozen vec_delay depths by hand, and getting out_valid's tap wrong by one
    // shifts the whole scoreboard so that every group fails at once with no
    // hint as to which stage moved. This turns that into one printed number.
    integer meas_lat;
    reg     meas_go, meas_vld;
    localparam [23:0] MEAS_VAL = 24'h41C800;         // a value nothing else emits

    always @(posedge clk) begin
        if (meas_go) begin
            meas_lat <= meas_lat + 1;
            if (out_valid) begin
                meas_vld <= 1'b1;
            end
        end
    end

    // the checker
    real got_r, err_u, ulpv;
    integer g;
    always @(posedge clk) begin
        if (!rst && out_valid) begin
            g = q_grp[nret];
            grp_n[g] = grp_n[g] + 1;
            if (q_kind[nret] == K_BITS) begin
                if (out !== q_bits[nret]) begin
                    errors = errors + 1;
                    if (errors <= 15) begin
                        $display("  FAIL %0s #%0d: got %06h want %06h (exact)",
                                 grp_nm[g], nret, out, q_bits[nret]);
                    end
                end
            end else if (q_kind[nret] == K_ABS) begin
                got_r = e8_real(out);
                err_u = got_r - q_val[nret];
                if (err_u < 0.0) begin
                    err_u = -err_u;
                end
                // The more permissive of the two limits, per the note above.
                // 0.99 and not 0.51: log2 is a SEED, and the seeds are
                // specified faithful, exactly as inv, rsqrt and exp2 are
                // checked. 0.51 is the FMA's bound, and the FMA meets it.
                ulpv = 0.99 * ulp_of(out);
                if (q_tol[nret] > ulpv) begin
                    ulpv = q_tol[nret];
                end
                err_u = err_u / ulpv;
                if (err_u > grp_max[g]) begin
                    grp_max[g] = err_u;
                end
                if (err_u > 1.0) begin
                    errors = errors + 1;
                    if (errors <= 15) begin
                        $display("  FAIL %0s #%0d: got %0.9g want %0.9g  %0.3fx limit",
                                 grp_nm[g], nret, got_r, q_val[nret], err_u);
                    end
                end
            end else begin
                got_r = e8_real(out);
                ulpv  = ulp_of(out);
                err_u = (got_r - q_val[nret]) / ulpv;
                if (err_u < 0.0) begin
                    err_u = -err_u;
                end
                if (err_u > grp_max[g]) begin
                    grp_max[g] = err_u;
                end
                if (err_u > q_tol[nret]) begin
                    errors = errors + 1;
                    if (errors <= 15) begin
                        $display("  FAIL %0s #%0d: got %0.9g want %0.9g  %0.3f ulp (limit %0.2f)",
                                 grp_nm[g], nret, got_r, q_val[nret], err_u, q_tol[nret]);
                    end
                end
            end
            nret = nret + 1;
        end
    end

    // =====================================================================
    // the run
    // =====================================================================
    integer i, j, ei, seed;
    real    xr, yr, zr, want;
    reg [23:0] xa, xb, xc, xd;

    initial begin
        for (i = 0; i < 32; i = i + 1) begin
            grp_max[i] = 0.0; grp_n[i] = 0; grp_nm[i] = "?";
        end
        grp_nm[0]="latency"; grp_nm[1]="move/cmp";  grp_nm[2]="exact mul";
        grp_nm[3]="fma";     grp_nm[4]="align";     grp_nm[5]="cancel";
        grp_nm[6]="exp2";    grp_nm[7]="log2";      grp_nm[8]="inv";
        grp_nm[9]="rsqrt";   grp_nm[10]="ident";    grp_nm[11]="specials";
        grp_nm[12]="stream";

        seed = 32'h5EED_1234;
        meas_lat = -1;
        meas_go  = 1'b0;
        // glbl holds GSR for the first 100 ns and unisim registers ignore
        // EVERYTHING until it releases, whatever the design's own reset says.
        // An eight-cycle reset ends at 24 ns and the first ~30 results then
        // differ between MODEL=0 and MODEL=1. Held past 100 ns instead.
        #200;
        @(negedge clk);
        rst = 1'b0;
        repeat (4) @(negedge clk);

        // -----------------------------------------------------------------
        $display("--- 0. pipeline latency, and out_valid against it ---");
        push_bits(MEAS_VAL, 0);
        // -1, because meas_go goes high one edge before drv's negedge presents
        // the operands, so the counter takes one increment before the input
        // register has captured anything.
        meas_lat = -1; meas_vld = 1'b0; meas_go = 1'b1;
        drv(OP_MOV, MEAS_VAL, E8_ZERO, E8_ZERO);
        @(negedge clk); in_valid = 1'b0;
        while (out !== MEAS_VAL && meas_lat < 60) begin
            @(negedge clk);
        end
        meas_go = 1'b0;
        $display("    out settles %0d cycles after the input register", meas_lat);
        if (meas_lat >= 60) begin
            errors = errors + 1;
            $display("  FAIL the result never appeared at all");
        end else if (meas_vld) begin
            errors = errors + 1;
            $display("  FAIL out_valid was already high -- the vpipe tap is one "
                     );
            $display("       cycle EARLY, so every group below fails at once");
        end
        idle(30);

        // -----------------------------------------------------------------
        $display("--- 1. format round trip (bench helpers, not the DUT) ---");
        for (i = 0; i < 200; i = i + 1) begin
            xa = {$random(seed)} & 24'hFFFFFF;
            if (xa[22:15] != 8'd0 && xa[22:15] != 8'hFF) begin
                if (e8_of(e8_real(xa)) !== xa) begin
                    errors = errors + 1;
                    if (errors <= 15) begin
                        $display("  FAIL fmt: %06h -> %0.9g -> %06h",
                                 xa, e8_real(xa), e8_of(e8_real(xa)));
                    end
                end
            end
        end

        // -----------------------------------------------------------------
        // Data movement and ordering must be BIT exact. They ride through the
        // multiplier as `winner * 1.0 + 0`, so this is also the test that
        // multiplying by one and adding a true zero really do not round.
        $display("--- 2. move, compare, select -- exact ---");
        for (i = 0; i < 400; i = i + 1) begin
            xa = {$random(seed)} & 24'hFFFFFF;
            xb = {$random(seed)} & 24'hFFFFFF;
            if (xa[22:15] == 8'hFF) begin
                xa[22:15] = 8'd130;
            end
            if (xb[22:15] == 8'hFF) begin
                xb[22:15] = 8'd130;
            end
            if (xa[22:15] == 8'd0) begin
                xa[22:15] = 8'd120;
            end
            if (xb[22:15] == 8'd0) begin
                xb[22:15] = 8'd120;
            end

            push_bits(xa, 1);                       drv(OP_MOV, xa, xb, E8_ZERO);
            push_bits({~xa[23], xa[22:0]}, 1);      drv(OP_NEG, xa, xb, E8_ZERO);
            push_bits({1'b0, xa[22:0]}, 1);         drv(OP_ABS, xa, xb, E8_ZERO);
            push_bits((e8_real(xa) >= e8_real(xb)) ? xa : xb, 1);
            drv(OP_MAX, xa, xb, E8_ZERO);
            push_bits((e8_real(xa) <= e8_real(xb)) ? xa : xb, 1);
            drv(OP_MIN, xa, xb, E8_ZERO);
            push_bits(xa, 1);                       drv(OP_SEL, xa, xb, E8_ONE);
            push_bits(xb, 1);                       drv(OP_SEL, xa, xb, E8_ZERO);
            push_bits((e8_real(xa) <  e8_real(xb)) ? E8_ONE : E8_ZERO, 1);
            drv(OP_CMPLT, xa, xb, E8_ZERO);
            push_bits((e8_real(xa) >  e8_real(xb)) ? E8_ONE : E8_ZERO, 1);
            drv(OP_CMPGT, xa, xb, E8_ZERO);
            push_bits((e8_real(xa) == e8_real(xb)) ? E8_ONE : E8_ZERO, 1);
            drv(OP_CMPEQ, xa, xb, E8_ZERO);
        end
        // max(x,x) == x. The equal-magnitude case is the one that breaks if the
        // comparator is written as a bare unsigned compare with a sign XOR.
        for (i = 0; i < 8; i = i + 1) begin
            xa = e8_of(-3.5 * (i + 1));
            push_bits(xa, 1); drv(OP_MAX, xa, xa, E8_ZERO);
            push_bits(xa, 1); drv(OP_MIN, xa, xa, E8_ZERO);
        end

        // -----------------------------------------------------------------
        // A product of powers of two is exact, and so is scaling a significand
        // by one. If either rounds, the multiplier is misaligned by a bit.
        $display("--- 3. exact products and sums ---");
        for (i = 0; i < 60; i = i + 1) begin
            ei = (i % 40) - 20;
            xa = e8_of(2.0 ** ei);
            xb = e8_of(2.0 ** (3 - (i % 7)));
            push_bits(e8_of(e8_real(xa) * e8_real(xb)), 2);
            drv(OP_MUL, xa, xb, E8_ZERO);
            push_bits(xa, 2);  drv(OP_ADD, xa, E8_ONE, E8_ZERO);
            push_bits(xa, 2);  drv(OP_MUL, xa, E8_ONE, E8_ZERO);
            push_bits(xa, 2);  drv(OP_FMA, xa, E8_ONE, E8_ZERO);
        end

        // -----------------------------------------------------------------
        // THE ALIGNMENT SWEEP. Walks the exponent difference across the whole
        // barrel shifter: s == 48 (addend gone), s == 17 (equal exponents),
        // s == 0 (addend at [47:32], where P[47] is a value bit) and s < 0
        // (the bypass). Random operands never land on those.
        $display("--- 4. alignment sweep, every shifter position ---");
        for (i = -40; i <= 40; i = i + 1) begin
            for (j = 0; j < 4; j = j + 1) begin
                xa = e8_of(1.0 + 0.37 * j);
                xb = e8_of(1.0 + 0.11 * j);
                xc = e8_of((1.0 + 0.53 * j) * (2.0 ** i));
                want = e8_real(xa) * e8_real(xb) + e8_real(xc);
                push_real(want, 0.51, 4);
                drv(OP_FMA, xa, xb, xc);
                // and the subtracting direction, which exercises the 33-bit
                // magnitude recovery rather than the 48-bit add
                want = e8_real(xa) * e8_real(xb) - e8_real(xc);
                push_real(want, 0.51, 4);
                drv(OP_FMA, xa, xb, {~xc[23], xc[22:0]});
            end
        end

        // -----------------------------------------------------------------
        // a*b - a*b is zero only when a*b is REPRESENTABLE, so b is a power of
        // two here. With an arbitrary b the addend is the rounded product and
        // the difference is the rounding error -- a real number, correctly
        // returned, and a bench bug rather than a circuit one.
        // The sign matters as much as the magnitude: x - x is +0.
        $display("--- 5. exact cancellation ---");
        for (i = 0; i < 64; i = i + 1) begin
            xa = e8_of((1.0 + 0.013 * i) * (i[0] ? -1.0 : 1.0));
            xb = e8_of(2.0 ** ((i % 9) - 4));
            push_bits(24'd0, 5);
            drv(OP_FNMA, xa, xb, e8_of(e8_real(xa) * e8_real(xb)));
            push_bits(24'd0, 5);
            drv(OP_SUB, xa, E8_ONE, xa);
        end

        // -----------------------------------------------------------------
        $display("--- 6. FMA against double, random operands ---");
        for (i = 0; i < 6000; i = i + 1) begin
            xa = {$random(seed)} & 24'hFFFFFF; xa[22:15] = 100 + (xa[22:15] % 55);
            xb = {$random(seed)} & 24'hFFFFFF; xb[22:15] = 100 + (xb[22:15] % 55);
            xc = {$random(seed)} & 24'hFFFFFF; xc[22:15] = 100 + (xc[22:15] % 55);
            want = e8_real(xa) * e8_real(xb) + e8_real(xc);
            push_real(want, 0.51, 3);
            drv(OP_FMA, xa, xb, xc);
        end

        // -----------------------------------------------------------------
        // The identity cases. These are the ones the coefficient table is NOT
        // fitted through -- vec_alu substitutes the exact value at the segment
        // origin instead, because constraining the polynomial there costs 1.5
        // bits everywhere else. See scripts/py/vec_tables.py.
        $display("--- 7. transcendental identities -- exact ---");
        for (i = -30; i <= 30; i = i + 1) begin
            push_bits(e8_of(2.0 ** i), 10);
            drv(OP_EXP2, e8_of($itor(i)), E8_ZERO, E8_ZERO);
            push_bits(e8_of($itor(i)), 10);
            drv(OP_LOG2, e8_of(2.0 ** i), E8_ZERO, E8_ZERO);
            push_bits(e8_of(2.0 ** (-i)), 10);
            drv(OP_INV,  e8_of(2.0 ** i), E8_ZERO, E8_ZERO);
            push_bits(e8_of(2.0 ** (-i)), 10);
            drv(OP_RSQRT, e8_of(2.0 ** (2 * i)), E8_ZERO, E8_ZERO);
        end

        // -----------------------------------------------------------------
        // Dense mantissa sweeps. Every 64th mantissa across several octaves is
        // 512 samples per octave, which lands inside every one of the 32
        // segments many times over.
        $display("--- 8. seeds against double, dense sweep ---");
        for (ei = 124; ei <= 130; ei = ei + 1) begin
            for (i = 0; i < 32768; i = i + 64) begin
                xa = {1'b0, ei[7:0], i[14:0]};
                xr = e8_real(xa);
                push_abs($ln(xr) / $ln(2.0), 2.0 ** -18, 7);
                drv(OP_LOG2,  xa, 0, 0);
                push_real(1.0 / xr,           0.99, 8);  drv(OP_INV,   xa, 0, 0);
                push_real(1.0 / $sqrt(xr),    0.99, 9);  drv(OP_RSQRT, xa, 0, 0);
            end
        end
        // exp2 over its whole useful argument range, both signs
        for (i = -12000; i <= 12000; i = i + 13) begin
            xr = $itor(i) / 100.0;                       // -120 .. +120
            xa = e8_of(xr);
            push_real(2.0 ** e8_real(xa), 0.99, 6);
            drv(OP_EXP2, xa, 0, 0);
        end

        // -----------------------------------------------------------------
        $display("--- 9. specials ---");
        push_bits(E8_INF,  11); drv(OP_MUL,  E8_INF,  E8_ONE,  E8_ZERO);
        push_bits(E8_NAN,  11); drv(OP_MUL,  E8_INF,  E8_ZERO, E8_ZERO);
        push_bits(E8_NAN,  11); drv(OP_FMA,  E8_INF,  E8_ONE,  E8_NINF);
        push_bits(E8_INF,  11); drv(OP_FMA,  E8_INF,  E8_ONE,  E8_INF);
        push_bits(E8_NAN,  11); drv(OP_ADD,  E8_NAN,  E8_ONE,  E8_ZERO);
        push_bits(24'd0,   11); drv(OP_MUL,  E8_ZERO, e8_of(7.5), E8_ZERO);
        push_bits(e8_of(2.5), 11); drv(OP_FMA, E8_ZERO, e8_of(9.0), e8_of(2.5));
        push_bits(E8_INF,  11); drv(OP_EXP2, E8_INF,  0, 0);
        push_bits(24'd0,   11); drv(OP_EXP2, E8_NINF, 0, 0);
        push_bits(E8_INF,  11); drv(OP_EXP2, e8_of(500.0), 0, 0);
        push_bits(24'd0,   11); drv(OP_EXP2, e8_of(-500.0), 0, 0);
        push_bits(E8_ONE,  11); drv(OP_EXP2, E8_ZERO, 0, 0);
        push_bits(E8_NINF, 11); drv(OP_LOG2, E8_ZERO, 0, 0);
        push_bits(E8_NAN,  11); drv(OP_LOG2, e8_of(-2.0), 0, 0);
        push_bits(E8_INF,  11); drv(OP_LOG2, E8_INF,  0, 0);
        push_bits(E8_INF,  11); drv(OP_INV,  E8_ZERO, 0, 0);
        push_bits(E8_NINF, 11); drv(OP_INV,  {1'b1, 23'd0}, 0, 0);
        push_bits(24'd0,   11); drv(OP_INV,  E8_INF,  0, 0);
        push_bits(E8_INF,  11); drv(OP_RSQRT, E8_ZERO, 0, 0);
        push_bits(E8_NAN,  11); drv(OP_RSQRT, e8_of(-4.0), 0, 0);
        push_bits(24'd0,   11); drv(OP_RSQRT, E8_INF, 0, 0);

        // -----------------------------------------------------------------
        // Back-to-back mixed opcodes. Nothing new is computed here; the point
        // is that consecutive instructions of DIFFERENT classes -- which use
        // DSP-M for different things and reach it by different routes -- do not
        // disturb each other at II = 1.
        $display("--- 10. mixed opcodes back to back, II = 1 ---");
        for (i = 0; i < 3000; i = i + 1) begin
            xa = {$random(seed)} & 24'hFFFFFF; xa[23] = 1'b0;
            xa[22:15] = 118 + (xa[22:15] % 18);
            xb = {$random(seed)} & 24'hFFFFFF; xb[22:15] = 118 + (xb[22:15] % 18);
            xc = {$random(seed)} & 24'hFFFFFF; xc[22:15] = 118 + (xc[22:15] % 18);
            case (i % 5)
              0: begin push_real(e8_real(xa)*e8_real(xb)+e8_real(xc), 0.51, 12);
                       drv(OP_FMA, xa, xb, xc); end
              // exp2 rather than log2 here: log2's II=1 behaviour is already
              // covered by the dense sweep above, which is itself streamed, and
              // exp2's error is measurable in ulps without the near-1 caveat.
              // Its own operand, because exp2 of the shared one overflows on
              // purpose and would be testing the overflow path a third time.
              1: begin
                     xd = e8_of(($itor(i % 401) - 200.0) / 16.0);   // +-12.5
                     push_real(2.0 ** e8_real(xd), 0.99, 12);
                     drv(OP_EXP2, xd, xb, xc);
                 end
              2: begin push_real(1.0/e8_real(xa), 0.99, 12);
                       drv(OP_INV, xa, xb, xc); end
              3: begin push_bits(xa, 12); drv(OP_MOV, xa, xb, xc); end
              4: begin push_real(1.0/$sqrt(e8_real(xa)), 0.99, 12);
                       drv(OP_RSQRT, xa, xb, xc); end
            endcase
        end

        idle(40);

        // -----------------------------------------------------------------
        $display("");
        $display("    group           n     worst");
        for (i = 0; i < 13; i = i + 1) begin
            if (grp_n[i] > 0) begin
                if (i == 7) begin
                    $display("    %0s %6d   %0.3fx limit  (0.99 ulp or 2^-18 abs)",
                             {grp_nm[i], "         "}, grp_n[i], grp_max[i]);
                end
                else if (grp_max[i] > 0.0) begin
                    $display("    %0s %6d   %0.3f ulp",
                             {grp_nm[i], "         "}, grp_n[i], grp_max[i]);
                end
                else begin
                    $display("    %0s %6d   exact",
                             {grp_nm[i], "         "}, grp_n[i]);
                end
            end
        end
        $display("");
        if (nret != nsub) begin
            errors = errors + 1;
            $display("  FAIL retired %0d of %0d -- pipeline dropped or duplicated",
                     nret, nsub);
        end
        $display("    MODEL=%0d  %0d checks  %0d errors", MODEL, nret, errors);
        if (errors == 0) begin
            $display("  PASS");
        end
        else begin
            $display("  FAIL");
        end
        $finish;
    end

    initial begin
        #40000000;
        $display("  FAIL watchdog -- bench did not finish");
        $finish;
    end

endmodule

`default_nettype wire

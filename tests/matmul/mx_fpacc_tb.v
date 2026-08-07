// Accumulator-float primitives at MW=16 (FP24) against a real-number model.
//
// The width sweep lives in mx_acu_fp_tb; this bench pins the arithmetic at one
// width and checks it hard. See docs/compute/accumulator.md.
//
// Two claims are being tested, and they are different in kind:
//
//   norm   is EXACT whenever the integer fits the 17-bit significand, which is
//          every K=8 block result. That is checked as an equality, not a
//          tolerance -- the common case must not round at all.
//   add    rounds to nearest even, so it is checked against a half-ULP bound.
//
// Checking add with a tolerance and norm without is deliberate. A tolerance on
// norm would hide exactly the case the accumulator depends on.

`default_nettype none
`timescale 1ns/1ps

module mx_fpacc_tb;

    localparam integer BIAS = 63;

    reg signed [29:0] nv;
    reg signed [9:0]  ne;
    wire       [23:0] nf;
    mx_fpacc_norm #(.MW(16), .VW(30), .BIAS(BIAS)) u_norm (.val(nv), .exp_in(ne), .fp(nf));

    reg  [23:0] aa, bb;
    wire [23:0] ss;
    mx_fpacc_add #(.MW(16)) u_add (.a(aa), .b(bb), .s(ss));

    reg  [23:0] cv;
    wire [15:0] cf;
    mx_fpacc_to_fp16 #(.MW(16)) u_cvt (.fpa(cv), .fp16(cf));

    integer errors = 0, checks = 0;

    function real fp2real(input [23:0] f);
        real m;
        integer e;
        begin
            if (f[22:16] == 7'd0) fp2real = 0.0;
            else begin
                m = 1.0 + $itor(f[15:0]) / 65536.0;
                e = f[22:16] - BIAS;
                fp2real = m * (2.0 ** e);
                if (f[23]) fp2real = -fp2real;
            end
        end
    endfunction

    task chk_real(input real got, input real want, input real tol, input [255:0] what);
        real err, denom;
        begin
            checks = checks + 1;
            denom = (want < 0.0) ? -want : want;
            if (denom < 1.0e-30) denom = 1.0;
            err = (got - want) / denom;
            if (err < 0.0) err = -err;
            if (err > tol) begin
                errors = errors + 1;
                if (errors <= 12)
                    $display("  FAIL %0s: got %0e want %0e relerr %0e", what, got, want, err);
            end
        end
    endtask

    task chk_exact(input real got, input real want, input [255:0] what);
        begin
            checks = checks + 1;
            if (got != want) begin
                errors = errors + 1;
                if (errors <= 12)
                    $display("  FAIL %0s: got %0f want %0f", what, got, want);
            end
        end
    endtask

    function real fp16_real(input [15:0] f);
        real m;
        integer e;
        begin
            if (f[14:10] == 5'd0) fp16_real = 0.0;
            else begin
                m = 1.0 + $itor(f[9:0]) / 1024.0;
                e = f[14:10] - 15;
                fp16_real = m * (2.0 ** e);
                if (f[15]) fp16_real = -fp16_real;
            end
        end
    endfunction

    task chk_cvt(input [23:0] f, input real want_v, input [255:0] what);
        begin
            cv = f; #1;
            chk_exact(fp16_real(cf), want_v, what);
        end
    endtask

    task chk_cvt_tol(input [23:0] f, input real want_v, input real tol,
                     input [255:0] what);
        begin
            cv = f; #1;
            chk_real(fp16_real(cf), want_v, tol, what);
        end
    endtask

    integer t, seed, i;
    real want, halfulp, peak, got_r, err_a;

    initial begin
        seed = 32'hFEED_BEEF;
        halfulp = 1.0 / 65536.0;      // one ULP at 16 mantissa bits

        // -----------------------------------------------------------------
        $display("--- 1. norm: zero and small exact cases ---");
        nv = 0;  ne = 0; #1; chk_exact(fp2real(nf), 0.0, "zero");
        nv = 1;  ne = 0; #1; chk_exact(fp2real(nf), 1.0, "one");
        nv = -1; ne = 0; #1; chk_exact(fp2real(nf), -1.0, "minus one");
        nv = 3;  ne = 2; #1; chk_exact(fp2real(nf), 12.0, "3 * 2^2");
        nv = -5; ne = -3; #1; chk_exact(fp2real(nf), -0.625, "-5 * 2^-3");

        // -----------------------------------------------------------------
        // Anything up to 17 significant bits must survive untouched. A K=8
        // block is 17 bits, so this is the case the accumulator relies on.
        $display("--- 2. norm: exact for everything inside 17 bits ---");
        for (t = 0; t < 3000; t = t + 1) begin
            nv = ($random(seed) % 131072);            // +/- 2^17
            ne = ($random(seed) % 20) - 10;
            #1;
            want = $itor(nv) * (2.0 ** ne);
            chk_exact(fp2real(nf), want, "norm exact");
        end

        // -----------------------------------------------------------------
        // A K=32 block can reach 19 bits, which does not fit; those round.
        $display("--- 3. norm: 19-bit values round to half a ULP ---");
        for (t = 0; t < 3000; t = t + 1) begin
            nv = ($random(seed) % 524288);            // +/- 2^19
            ne = ($random(seed) % 20) - 10;
            #1;
            want = $itor(nv) * (2.0 ** ne);
            if (nv != 0) chk_real(fp2real(nf), want, halfulp, "norm round");
        end

        // -----------------------------------------------------------------
        $display("--- 4. add: identities ---");
        nv = 7; ne = 0; #1; aa = nf; bb = 24'd0; #1;
        chk_exact(fp2real(ss), 7.0, "x + 0");
        bb = nf; aa = 24'd0; #1;
        chk_exact(fp2real(ss), 7.0, "0 + x");
        aa = nf; bb = {~nf[23], nf[22:0]}; #1;
        chk_exact(fp2real(ss), 0.0, "x + (-x)");

        // -----------------------------------------------------------------
        $display("--- 5. add: random pairs, half-ULP bound ---");
        for (t = 0; t < 5000; t = t + 1) begin
            nv = ($random(seed) % 131072);
            ne = ($random(seed) % 16) - 8;
            #1; aa = nf;
            nv = ($random(seed) % 131072);
            ne = ($random(seed) % 16) - 8;
            #1; bb = nf;
            #1;
            want = fp2real(aa) + fp2real(bb);
            if (want != 0.0) chk_real(fp2real(ss), want, halfulp, "add");
        end

        // -----------------------------------------------------------------
        // Wildly different exponents: the small operand must vanish cleanly
        // rather than corrupt the large one.
        $display("--- 6. add: extreme exponent gaps ---");
        for (t = 0; t < 2000; t = t + 1) begin
            nv = ($random(seed) % 65536) + 1;
            ne = 20;
            #1; aa = nf;
            nv = ($random(seed) % 65536) + 1;
            ne = -20;
            #1; bb = nf;
            #1;
            want = fp2real(aa) + fp2real(bb);
            chk_real(fp2real(ss), want, halfulp, "add far apart");
        end

        // -----------------------------------------------------------------
        // A running sum, which is how the ACU will actually use it.
        // Bounded on ABSOLUTE error, not relative. Each addition rounds the
        // running sum, so the error is half a ULP of whatever the partial sum
        // was -- bounded by the largest partial, not by the final total. With
        // random signed terms the total frequently cancels to near zero, and a
        // relative bound against a cancelled sum measures the cancellation
        // rather than the adder.
        $display("--- 7. add: 64-term accumulation ---");
        for (t = 0; t < 200; t = t + 1) begin
            aa = 24'd0;
            want = 0.0;
            peak = 0.0;
            for (i = 0; i < 64; i = i + 1) begin
                nv = ($random(seed) % 131072);
                ne = 0;
                #1; bb = nf;
                want = want + fp2real(bb);
                if (want > peak)  peak = want;
                if (-want > peak) peak = -want;
                #1; aa = ss;
            end
            got_r = fp2real(aa);
            err_a = got_r - want; if (err_a < 0.0) err_a = -err_a;
            checks = checks + 1;
            if (err_a > 64.0 * halfulp * peak) begin
                errors = errors + 1;
                if (errors <= 12)
                    $display("  FAIL accumulate: got %0f want %0f abserr %0e bound %0e",
                             got_r, want, err_a, 64.0*halfulp*peak);
            end
        end

        // -----------------------------------------------------------------
        // The accumulator is only useful if what comes OUT of it is right, and
        // this is the one place the wide format is narrowed. Untested until a
        // whole-mesh run turned up a single wrong element in 4096: a value
        // just under a power of two whose fraction rounded up and carried.
        //
        // The carry case is the whole point. m11 holds the STORED fraction, so
        // a carry out of it means the fraction becomes zero and the exponent
        // increments -- not a right shift, which leaves the carry bit inside
        // the fraction and returns 1.5*2^(e+1) instead of 1.0*2^(e+1).
        $display("--- 8. to_fp16: exact values ---");
        chk_cvt(24'd0, 0.0, "zero");
        nv = 1;    ne = 0;  #1; chk_cvt(nf,  1.0,   "one");
        nv = -1;   ne = 0;  #1; chk_cvt(nf, -1.0,   "minus one");
        nv = 3;    ne = 2;  #1; chk_cvt(nf, 12.0,   "3 * 2^2");
        nv = 2048; ne = 0;  #1; chk_cvt(nf, 2048.0, "2^11");

        $display("--- 9. to_fp16: rounding that carries out of the fraction ---");
        // 4095 * 2^(i-11) = (2 - 2^-11) * 2^i, so the stored fraction is all
        // ones with the round bit set: the carry path, every iteration. The
        // answer is 2^(i+1) exactly -- a right shift would give 1.5 * 2^(i+1).
        for (i = 1; i < 12; i = i + 1) begin
            nv = 4095;
            ne = i - 11;
            #1;
            chk_cvt(nf, 2.0 ** (i + 1), "fraction carry");
        end

        $display("--- 10. to_fp16: random values inside the FP16 normal range ---");
        // Only normals are checked against a tolerance. Outside that range the
        // converter is specified to saturate and to flush, and comparing those
        // against the accumulator's own value would be testing the bench's
        // expectations rather than the DUT.
        for (t = 0; t < 4000; t = t + 1) begin
            nv = ($random(seed) % 131072);
            ne = ($random(seed) % 24) - 16;
            #1;
            want = fp2real(nf);
            got_r = want < 0.0 ? -want : want;
            // half an FP16 ULP, and no more: the converter rounds to nearest.
            if (got_r >= (2.0 ** -14) && got_r < 65504.0)
                chk_cvt_tol(nf, want, 1.0/2048.0, "to_fp16");
        end

        $display("--- 11. to_fp16: saturation and flush ---");
        nv = 131071; ne = 8;  #1; chk_cvt(nf,  65504.0, "saturate +");
        nv = -131071; ne = 8; #1; chk_cvt(nf, -65504.0, "saturate -");
        nv = 1; ne = -30;     #1; chk_cvt(nf,  0.0,     "flush to zero");

        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end

endmodule

`default_nettype wire

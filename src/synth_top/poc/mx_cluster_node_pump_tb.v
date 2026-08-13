// Cluster node: manager + TCU cascade + accumulator, L1 preloaded, no NoC.
//
// This is the step that proves the LOOP STRUCTURE before any interconnect is
// involved: does a GEMM instruction issue the right operands in the right
// order, and land the right accumulator command on the right sub-tile?
//
// IT SWEEPS TILINGS, and that is the point. A single large case hides two
// whole classes of bug, both found end-to-end and both cheap to catch here:
//
//   Gm*Gn small     `gemm_busy` has to stay asserted until the ~19-deep
//                   cascade behind the manager has settled. A short sweep
//                   finishes before its own results land, and DRAIN -- which
//                   seizes the accumulator's control mux the cycle it starts
//                   -- throws them away. Every sub-tile drains as zero.
//
//   Gm*Gn < 5, NK>1 K is the outer loop, so an output address recurs every
//                   Gm*Gn issues. Below the accumulator's REUSE_MIN the second
//                   command reads a tile the first has not written back yet
//                   and accumulates onto a stale value.
//
// So this bench DRAINS THE CYCLE `gemm_busy` DROPS, with no settling delay.
// Padding there is what let both bugs through: it makes the bench weaker than
// mx_cluster_cu, which drains three cycles after the same signal.
//
// Checked against two independent ground truths, and the bench asserts they
// agree with each other before either is compared against hardware.

`default_nettype none
`timescale 1ns/1ps

`ifndef MX_MODEL
`define MX_MODEL 1
`endif

// Two-clock copy of tests/matmul/mx_cluster_node_tb.v.

// PUMP is selected by listing mx_pump_on.v FIRST, never by -d MX_PUMP=1:
// Vivado's .bat wrappers split arguments on '=' (tests/run_matmul_sim.ps1:83).
`ifndef MX_PUMP
`define MX_PUMP 0
`endif
`ifndef MX_PHINV
`define MX_PHINV 0
`endif

module mx_cluster_node_pump_tb;

    localparam integer MODEL = `MX_MODEL;
    // Value-less defines: xvlog.bat splits arguments on '=' and drops the value,
    // and `define does not cross files under -sv (each file is its own unit).
`ifdef MX_PUMP_A
    localparam integer PUMP = 2;
`elsif MX_PUMP_B
    localparam integer PUMP = 1;
`elsif MX_PUMP_C
    localparam integer PUMP = 3;
`else
    localparam integer PUMP = 0;
`endif

    // E5M3 block scale: field = {E[4:0], M[2:0]}, scale = 2^(E-SBIAS)*(1+M/8).
    // This bench wants pure powers of two, so M stays 0 and only E carries the
    // exponent. ANCHOR is 2*SBIAS to cancel both stored biases, plus 8 of
    // headroom so the drained tile stays inside FP16.
    localparam integer SBIAS  = 20;
    localparam integer HEADROOM = 8;
    // MAXNK bounds the OPERAND ARRAYS, not the hardware. A run_case with a
    // larger nk indexes past them and reads garbage, which looks like a
    // datapath failure.
    localparam integer MAXGM  = 8, MAXGN = 8, MAXNK = 8;
    localparam integer MAXM   = MAXGM*4, MAXN = MAXGN*4, MAXK = MAXNK*32;
    localparam integer MW     = 14;

    // clk2x leads by half a clk period and both edges coincide at every clk
    // rise, which is what the hardware's one-MMCM pair does.
    reg clk = 0, clk2x = 0, rst = 1;
    always #2 clk = ~clk;
`ifdef MX_SAMECLK
    // Same period and phase, driven identically -- `always @(*) clk2x = clk`
    // gives a delta-delayed copy that races the clk-domain registers.
    always #2 clk2x = ~clk2x;
`else
    always #1 clk2x = ~clk2x;
`endif

    reg          l1_we = 0, l1_sel = 0;
    reg [15:0]   l1_addr = 0;
    reg [927:0]  l1_data = 0;
    reg          gemm_start = 0;
    reg [7:0]    gemm_gm = 0, gemm_gn = 0, gemm_nk = 0, gemm_anchor = 0;
    reg          gemm_acc = 0;
    wire         gemm_busy;
    reg          drain_start = 0;
    reg [15:0]   drain_n = 0;
    wire         drain_busy, drain_valid;
    wire [255:0] drain_data;
    wire [15:0]  drain_idx;

    mx_cluster_node #(.TILES(256), .GA(32), .GB(32),
                      .ACC_MW(MW), .MODEL(MODEL), .PUMP(PUMP),
`ifdef MX_PHINV
                      .PH_INV(1)) dut (
`else
                      .PH_INV(0)) dut (
`endif
        .clk1x(clk), .clk2x(clk2x), .rst(rst),
        .l1_we(l1_we), .l1_sel(l1_sel), .l1_addr(l1_addr), .l1_data(l1_data),
        .gemm_start(gemm_start), .gemm_gm(gemm_gm), .gemm_gn(gemm_gn),
        .gemm_nk(gemm_nk), .gemm_anchor(gemm_anchor), .gemm_acc(gemm_acc),
        // L1 is addressable and the drain can be fused; this bench exercises
        // neither, so both are held at their identity values. They are DRIVEN
        // rather than left off: an unconnected input is z, and `gemm_emit`
        // reaches the accumulator's opcode -- so every command came out x and
        // every result was a plausible 1.0.
        .gemm_aoff(8'd0), .gemm_boff(8'd0), .gemm_emit(1'b0),
        .gemm_abank(1'b0), .gemm_bbank(1'b0),
        .gemm_busy(gemm_busy), .sweep_busy(),
        .drain_start(drain_start), .drain_n(drain_n), .drain_fused(1'b0),
        // OP_EMIT, not OP_SEND. Left unconnected this is z, the accumulator's
        // opcode goes x, and every sub-tile drains as a plausible zero.
        .drain_send(1'b0),
        .drain_busy(drain_busy),
        .drain_data(drain_data), .drain_idx(drain_idx), .drain_valid(drain_valid),
        // The collector takes one sub-tile per cycle it is offered -- which is
        // what the sampling loop below does. Leaving it unconnected is z, so
        // the queue never popped and every sub-tile read back as the same
        // word: a plausible constant, not an error.
        .drain_take(1'b1),
        // Peer transfer is mx_cluster_cu's to drive; DRIVEN here for the same
        // reason as the two above.
        .peer_open(1'b0), .peer_cmd(1'b0), .peer_idx(16'd0),
        .peer_data({(16*(MW+8)){1'b0}}), .peer_ready(),
        .peer_out(), .peer_out_valid()
    );

    // ---- the problem ----
    integer signed A  [0:MAXM-1][0:MAXK-1];
    integer signed B  [0:MAXK-1][0:MAXN-1];
    // Wall-clock, not cycles: once the domains differ, cycle counts are not
    // comparable between builds. `gemm_ns` accumulates only the sweep itself.
    real gemm_ns = 0.0;
    real t_sweep;
    integer n_sweep = 0;

    integer        SA [0:MAXM-1][0:MAXNK-1];   // one scale per lane PER K BLOCK
    integer        SB [0:MAXN-1][0:MAXNK-1];
    integer        ANCHOR;

    real  fp64_c [0:MAXM-1][0:MAXN-1];
    real  exact_c[0:MAXM-1][0:MAXN-1];
    real  hw_c   [0:MAXM-1][0:MAXN-1];
    reg   got    [0:MAXGM*MAXGN-1];

    integer errors = 0, checks = 0;
    real    worst_rel = 0.0;
    integer cur_gn = 1;

    function real fp16_to_real(input [15:0] f);
        real m; integer e;
        begin
            if (f[14:10] == 5'd0) fp16_to_real = 0.0;
            else begin
                m = 1.0 + $itor(f[9:0]) / 1024.0;
                e = f[14:10] - 15;
                fp16_to_real = m * (2.0 ** e);
                if (f[15]) fp16_to_real = -fp16_to_real;
            end
        end
    endfunction

    integer i, j, k, g, h, t, kb, seed;
    integer signed bsum;
    reg [927:0] ent;
    real want_r, got_r, err, tol;

    // capture drained sub-tiles as they come out
    always @(posedge clk) begin
        if (drain_valid) begin
            got[drain_idx] = 1'b1;
            for (i = 0; i < 4; i = i + 1)
                for (j = 0; j < 4; j = j + 1)
                    hw_c[(drain_idx / cur_gn)*4 + i][(drain_idx % cur_gn)*4 + j]
                        = fp16_to_real(drain_data[(i*4+j)*16 +: 16]);
        end
    end

    // ------------------------------------------------------------------
    // One tiling, end to end. `gm`x`gn` output sub-tiles over `nk` K blocks.
    // `chunks` splits the SAME K sweep across that many GEMM instructions,
    // chained with gemm_acc. The answer must not depend on how the sweep was
    // divided -- that is the whole contract the accumulate bit exists to
    // provide, and it is what lets K exceed L1 without the partial result
    // making a round trip through memory.
    task run_case(input integer gm, input integer gn, input integer nk,
                  input integer chunks);
        integer m, n, kk, nt, ii, jj, kkk, gg, hh, bb, tt, ch, nkc, kb0;
        integer signed acc;
        real want, peak;
        begin
            m = gm*4; n = gn*4; kk = nk*32; nt = gm*gn;
            cur_gn = gn;
            for (tt = 0; tt < nt; tt = tt + 1) got[tt] = 1'b0;

            // ---- operands. int7 values and a per-block power-of-two scale,
            // which is exactly what an MXFP7 L1 entry holds.
            for (ii = 0; ii < m; ii = ii + 1)
                for (kkk = 0; kkk < kk; kkk = kkk + 1)
                    A[ii][kkk] = ($random(seed) & 127) - 64;
            for (kkk = 0; kkk < kk; kkk = kkk + 1)
                for (jj = 0; jj < n; jj = jj + 1)
                    B[kkk][jj] = ($random(seed) & 127) - 64;
            for (ii = 0; ii < m; ii = ii + 1)
                for (bb = 0; bb < nk; bb = bb + 1) SA[ii][bb] = ($random(seed) & 3);
            for (jj = 0; jj < n; jj = jj + 1)
                for (bb = 0; bb < nk; bb = bb + 1) SB[jj][bb] = ($random(seed) & 3);

            // ---- two ground truths: an exact scaled-integer sum per K block,
            // and the same thing in FP64. They must agree before either is
            // allowed to judge the hardware.
            for (ii = 0; ii < m; ii = ii + 1)
                for (jj = 0; jj < n; jj = jj + 1) begin
                    exact_c[ii][jj] = 0.0;
                    fp64_c[ii][jj]  = 0.0;
                    for (bb = 0; bb < nk; bb = bb + 1) begin
                        acc = 0;
                        for (kkk = 0; kkk < 32; kkk = kkk + 1)
                            acc = acc + A[ii][bb*32+kkk]*B[bb*32+kkk][jj];
                        exact_c[ii][jj] = exact_c[ii][jj]
                            + $itor(acc <<< (SA[ii][bb] + SB[jj][bb]));
                        fp64_c[ii][jj] = fp64_c[ii][jj]
                            + $itor(acc) * (2.0 ** (SA[ii][bb] + SB[jj][bb]));
                    end
                    // only HEADROOM, not ANCHOR: the other 2*SBIAS of the
                    // anchor cancels the bias stored in each scale field, so
                    // it never reaches the result
                    exact_c[ii][jj] = exact_c[ii][jj] * (2.0 ** -HEADROOM);
                    fp64_c[ii][jj]  = fp64_c[ii][jj]  * (2.0 ** -HEADROOM);
                    checks = checks + 1;
                    if (exact_c[ii][jj] != fp64_c[ii][jj]) begin
                        errors = errors + 1;
                        if (errors <= 4)
                            $display("  FAIL models disagree at [%0d][%0d]", ii, jj);
                    end
                end

            // ---- one chunk at a time: refill L1 with that chunk's K blocks,
            // then GEMM into the SAME resident tile. L1 addressing stays
            // 0-based, so the manager needs no notion of which chunk this is
            // -- only whether to load the tile or add to it.
            nkc = nk / chunks;
            for (ch = 0; ch < chunks; ch = ch + 1) begin
                // A entries for K blocks [ch*nkc, (ch+1)*nkc)
                for (gg = 0; gg < gm; gg = gg + 1)
                    for (bb = 0; bb < nkc; bb = bb + 1) begin
                        kb0 = ch*nkc + bb;
                        ent = 928'd0;
                        for (ii = 0; ii < 4; ii = ii + 1)
                            for (kkk = 0; kkk < 32; kkk = kkk + 1)
                                ent[(ii*32 + kkk)*7 +: 7] =
                                    A[gg*4+ii][kb0*32+kkk][6:0];
                        for (ii = 0; ii < 4; ii = ii + 1)
                            ent[896 + ii*8 +: 8] = (SA[gg*4+ii][kb0] + SBIAS) << 3;
                        @(negedge clk);
                        l1_we <= 1'b1; l1_sel <= 1'b0;
                        l1_addr <= gg*nkc + bb; l1_data <= ent;
                        @(negedge clk); l1_we <= 1'b0;
                    end
                for (hh = 0; hh < gn; hh = hh + 1)
                    for (bb = 0; bb < nkc; bb = bb + 1) begin
                        kb0 = ch*nkc + bb;
                        ent = 928'd0;
                        for (kkk = 0; kkk < 32; kkk = kkk + 1)
                            for (jj = 0; jj < 4; jj = jj + 1)
                                ent[(kkk*4 + jj)*7 +: 7] =
                                    B[kb0*32+kkk][hh*4+jj][6:0];
                        for (jj = 0; jj < 4; jj = jj + 1)
                            ent[896 + jj*8 +: 8] = (SB[hh*4+jj][kb0] + SBIAS) << 3;
                        @(negedge clk);
                        l1_we <= 1'b1; l1_sel <= 1'b1;
                        l1_addr <= hh*nkc + bb; l1_data <= ent;
                        @(negedge clk); l1_we <= 1'b0;
                    end

                @(negedge clk);
                gemm_gm <= gm[7:0]; gemm_gn <= gn[7:0];
                gemm_nk <= nkc[7:0];
                gemm_anchor <= ANCHOR[7:0];
                gemm_acc <= (ch != 0);      // chain into what the last one left
                gemm_start <= 1'b1;
                t_sweep = $realtime;
                @(negedge clk); gemm_start <= 1'b0;
                tt = 0;
                while (gemm_busy && tt < 100000) begin
                    @(posedge clk); tt = tt + 1;
                end
                gemm_ns = gemm_ns + ($realtime - t_sweep);
                n_sweep = n_sweep + 1;
            end

            // ---- DRAIN IMMEDIATELY. No settling delay: `gemm_busy` dropping
            // is the whole contract, and a bench that waits is not testing it.
            @(negedge clk);
            drain_n <= nt[15:0]; drain_start <= 1'b1;
            @(negedge clk); drain_start <= 1'b0;

            tt = 0;
            while (drain_busy && tt < 200000) begin @(posedge clk); tt = tt + 1; end
            repeat (20) @(posedge clk);

            // ---- check
            for (tt = 0; tt < nt; tt = tt + 1) begin
                checks = checks + 1;
                if (!got[tt]) begin
                    errors = errors + 1;
                    if (errors <= 8)
                        $display("  FAIL gm=%0d gn=%0d nk=%0d sub-tile %0d never emitted",
                                 gm, gn, nk, tt);
                end
            end

            // Measured against the tile's PEAK, not per element. K blocks carry
            // independent scales, so a single output can cancel from partials
            // three orders of magnitude larger than itself; its own relative
            // error is then meaningless while the datapath is behaving. Peak
            // relative is what the end-to-end test reports, for the same reason.
            peak = 0.0;
            for (ii = 0; ii < m; ii = ii + 1)
                for (jj = 0; jj < n; jj = jj + 1) begin
                    want = fp64_c[ii][jj];
                    if (want < 0.0) want = -want;
                    if (want > peak) peak = want;
                end
            if (peak == 0.0) peak = 1.0;

            // Each K block contributes its own rounding, so the bound grows
            // with NK rather than being a single constant.
            tol = 4.0/1024.0 * $itor(nk);
            for (ii = 0; ii < m; ii = ii + 1)
                for (jj = 0; jj < n; jj = jj + 1) begin
                    got_r = hw_c[ii][jj]; want = fp64_c[ii][jj];
                    checks = checks + 1;
                    err = (got_r - want) / peak;
                    if (err < 0.0) err = -err;
                    if (err > worst_rel) worst_rel = err;
                    if (err > tol) begin
                        errors = errors + 1;
                        if (errors <= 8)
                            $display("  FAIL gm=%0d gn=%0d nk=%0d C[%0d][%0d] got %0f want %0f err/peak %0e",
                                     gm, gn, nk, ii, jj, got_r, want, err);
                    end
                end

            $display("    gm=%0d gn=%0d nk=%0d  C[%0d,%0d] over K=%0d  %0d sub-tiles  C[0][0] hw %0f fp64 %0f",
                     gm, gn, nk, m, n, kk, nt, hw_c[0][0], fp64_c[0][0]);
        end
    endtask

    integer err_before;

    initial begin
        seed = 32'h1234ABCD;
        ANCHOR = 2*SBIAS + HEADROOM;

        // PAST 100 ns, not past 20. `glbl` holds GSR for the first 100 ns and
        // every unisim register ignores its own reset until then -- so at
        // MODEL=0 the first tiling used to run against DSP48E2s that were still
        // globally reset, and only the first one, which reads as an
        // arithmetic bug in the datapath rather than as a bench that started
        // too early.
        #120; @(negedge clk); rst = 0; @(negedge clk);

`ifdef MX_SPEED
        // Large NK so the K loop dominates the ~19-deep cascade. Bounded by
        // L1: gm*nk <= GA and gn*nk <= GB, both 32 in this bench.
        run_case(4, 4, 8, 1);
        $display("  SPEED -- %0d sweeps, %0.1f ns total, %0.2f ns/sweep",
                 n_sweep, gemm_ns, gemm_ns / n_sweep);
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $finish;
`elsif MX_FAST
  `ifdef MX_FAST4
        run_case(2, 2, 3, 1);       // four sub-tiles, odd NK > 1
  `elsif MX_FAST3
        run_case(2, 1, 2, 1);       // two sub-tiles, even NK
  `elsif MX_FAST2
        run_case(2, 1, 1, 1);       // two sub-tiles, odd NK
  `else
        // One even-NK sweep: the smallest case that exercises a full pump pair.
        run_case(1, 1, 2, 1);
  `endif
        $display("  SPEED -- %0d sweeps, %0.1f ns total, %0.2f ns/sweep",
                 n_sweep, gemm_ns, gemm_ns / n_sweep);
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $finish;
`endif

        $display("--- tilings that finish before the cascade does ---");
        err_before = errors;
        run_case(1, 1, 1, 1);       // 1 sub-tile
        run_case(2, 1, 1, 1);       // 2 sub-tiles
        run_case(1, 2, 1, 1);
        run_case(3, 1, 1, 1);
        if (errors == err_before) $display("    short sweeps drain intact");

        $display("--- tilings that revisit an address inside REUSE_MIN ---");
        err_before = errors;
        run_case(1, 1, 2, 1);       // one address, back to back over K
        run_case(2, 1, 2, 1);
        run_case(1, 4, 4, 1);
        run_case(2, 2, 3, 1);
        if (errors == err_before) $display("    paced K sweeps accumulate correctly");

        // The SAME K, split across several GEMM instructions chained with
        // gemm_acc. The answer must not depend on the division -- that is the
        // contract the accumulate bit exists to provide, and it is what makes
        // a K larger than L1 expressible at all.
        $display("--- K split across chained GEMM instructions ---");
        err_before = errors;
        run_case(1, 1, 2, 2);       // 2 chunks of 1 K block
        run_case(2, 2, 4, 2);       // 2 chunks of 2
        run_case(4, 4, 4, 4);       // 4 chunks of 1
        run_case(8, 8, 4, 2);       // full tile, 2 chunks
        run_case(2, 2, 6, 3);       // 3 chunks of 2
        if (errors == err_before)
            $display("    chained K chunks match a single sweep");

        $display("--- ordinary tilings ---");
        run_case(4, 4, 2, 1);
        run_case(8, 8, 1, 1);       // C[32,32] = A[32,32] x B[32,32]
        run_case(8, 4, 4, 1);

        $display("");
        $display("    worst err / tile peak %0e   (FP16 ULP %0e)", worst_rel, 1.0/1024.0);
        $display("========================================");
        $display("  SPEED -- %0d sweeps, %0.1f ns total, %0.2f ns/sweep",
                 n_sweep, gemm_ns, gemm_ns / n_sweep);
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors  (MODEL=%0d)", checks, MODEL);
        else             $display("  FAIL -- %0d checks, %0d errors  (MODEL=%0d)", checks, errors, MODEL);
        $display("========================================");
        $finish;
    end

    initial begin
        #50000000;
        $display("  FAIL -- watchdog (gemm_busy=%0d drain_busy=%0d)", gemm_busy, drain_busy);
        $finish;
    end

endmodule

`default_nettype wire

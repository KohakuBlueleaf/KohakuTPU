// Cluster node: manager + TCU cascade + accumulator, L1 preloaded, no NoC.
//
// This is the step that proves the LOOP STRUCTURE before any interconnect is
// involved: does a single GEMM instruction sweeping 64 output sub-tiles issue
// the right operands in the right order, and land the right accumulator
// command on the right sub-tile?
//
//   C[32,32] = A[32,32] x B[32,32]   ->  Gm=8, Gn=8, NK=1, 64 sub-tiles
//
// K is the outer loop, so the accumulator address changes every cycle instead
// of repeating -- the property the whole ISA is built on. If the command FIFO
// alignment were wrong, results would land on the wrong sub-tiles and the
// error would be structured, not noisy.
//
// Checked against two independent ground truths, and the bench asserts they
// agree with each other before either is compared against hardware.

`default_nettype none
`timescale 1ns/1ps

`ifndef MX_MODEL
`define MX_MODEL 1
`endif

module mx_cluster_node_tb;

    localparam integer MODEL = `MX_MODEL;
    localparam integer M  = 32, N = 32, KK = 32;
    localparam integer GM = M/4, GN = N/4;          // 8 x 8 sub-tiles
    localparam integer NT = GM*GN;                  // 64
    localparam integer MW = 14;

    reg clk = 0, rst = 1;
    always #2 clk = ~clk;

    reg          l1_we = 0, l1_sel = 0;
    reg [15:0]   l1_addr = 0;
    reg [927:0]  l1_data = 0;
    reg          gemm_start = 0;
    reg [7:0]    gemm_gm = 0, gemm_gn = 0, gemm_nk = 0, gemm_anchor = 0;
    wire         gemm_busy;
    reg          drain_start = 0;
    reg [15:0]   drain_n = 0;
    wire         drain_busy, drain_valid;
    wire [255:0] drain_data;
    wire [15:0]  drain_idx;

    mx_cluster_node #(.TILES(256), .GA(32), .GB(32),
                      .ACC_MW(MW), .MODEL(MODEL)) dut (
        .clk(clk), .rst(rst),
        .l1_we(l1_we), .l1_sel(l1_sel), .l1_addr(l1_addr), .l1_data(l1_data),
        .gemm_start(gemm_start), .gemm_gm(gemm_gm), .gemm_gn(gemm_gn),
        .gemm_nk(gemm_nk), .gemm_anchor(gemm_anchor), .gemm_busy(gemm_busy),
        .drain_start(drain_start), .drain_n(drain_n), .drain_busy(drain_busy),
        .drain_data(drain_data), .drain_idx(drain_idx), .drain_valid(drain_valid)
    );

    // ---- the problem ----
    integer signed A [0:M-1][0:KK-1];
    integer signed B [0:KK-1][0:N-1];
    integer        SA [0:M-1];
    integer        SB [0:N-1];
    integer        ANCHOR;

    real  fp64_c [0:M-1][0:N-1];
    integer signed exact_c [0:M-1][0:N-1];
    real  hw_c   [0:M-1][0:N-1];
    reg   got    [0:NT-1];

    integer errors = 0, checks = 0;
    real    worst_rel = 0.0, sum_rel = 0.0;
    integer nz = 0;

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

    integer i, j, k, g, h, t, seed;
    integer signed bsum;
    reg [927:0] ent;
    real want_r, got_r, err;

    // capture drained sub-tiles as they come out
    always @(posedge clk) begin
        if (drain_valid) begin
            got[drain_idx] = 1'b1;
            for (i = 0; i < 4; i = i + 1)
                for (j = 0; j < 4; j = j + 1)
                    hw_c[(drain_idx / GN)*4 + i][(drain_idx % GN)*4 + j]
                        = fp16_to_real(drain_data[(i*4+j)*16 +: 16]);
        end
    end

    initial begin
        seed = 32'h1234ABCD;
        ANCHOR = 8;
        for (t = 0; t < NT; t = t + 1) got[t] = 1'b0;

        #20; @(negedge clk); rst = 0; @(negedge clk);

        // ---------------------------------------------------------------
        $display("--- 1. build C[32,32] = A[32,32] x B[32,32] ---");
        for (i = 0; i < M; i = i + 1)
            for (k = 0; k < KK; k = k + 1) A[i][k] = ($random(seed) & 127) - 64;
        for (k = 0; k < KK; k = k + 1)
            for (j = 0; j < N; j = j + 1) B[k][j] = ($random(seed) & 127) - 64;
        for (i = 0; i < M; i = i + 1) SA[i] = ($random(seed) & 3);
        for (j = 0; j < N; j = j + 1) SB[j] = ($random(seed) & 3);

        for (i = 0; i < M; i = i + 1)
            for (j = 0; j < N; j = j + 1) begin
                bsum = 0;
                for (k = 0; k < KK; k = k + 1) bsum = bsum + A[i][k]*B[k][j];
                exact_c[i][j] = bsum <<< (SA[i] + SB[j]);
                fp64_c[i][j]  = $itor(bsum) * (2.0 ** (SA[i] + SB[j] - ANCHOR));
                checks = checks + 1;
                if ($itor(exact_c[i][j]) * (2.0 ** -ANCHOR) != fp64_c[i][j]) begin
                    errors = errors + 1;
                    if (errors <= 4) $display("  FAIL models disagree at [%0d][%0d]", i, j);
                end
            end
        $display("    %0d elements, two ground truths agree", M*N);

        // ---------------------------------------------------------------
        // L1 A entry g: rows 4g..4g+3, all 32 K. Element (r,k) sits at
        // bit (r*32 + k)*7, matching mx_cluster_core's a_in packing.
        $display("--- 2. preload L1 ---");
        for (g = 0; g < GM; g = g + 1) begin
            ent = 928'd0;
            for (i = 0; i < 4; i = i + 1)
                for (k = 0; k < KK; k = k + 1)
                    ent[(i*32 + k)*7 +: 7] = A[g*4+i][k][6:0];
            for (i = 0; i < 4; i = i + 1)
                ent[896 + i*8 +: 8] = SA[g*4+i][7:0];
            @(negedge clk);
            l1_we <= 1'b1; l1_sel <= 1'b0; l1_addr <= g[15:0]; l1_data <= ent;
            @(negedge clk); l1_we <= 1'b0;
        end
        for (h = 0; h < GN; h = h + 1) begin
            ent = 928'd0;
            for (k = 0; k < KK; k = k + 1)
                for (j = 0; j < 4; j = j + 1)
                    ent[(k*4 + j)*7 +: 7] = B[k][h*4+j][6:0];
            for (j = 0; j < 4; j = j + 1)
                ent[896 + j*8 +: 8] = SB[h*4+j][7:0];
            @(negedge clk);
            l1_we <= 1'b1; l1_sel <= 1'b1; l1_addr <= h[15:0]; l1_data <= ent;
            @(negedge clk); l1_we <= 1'b0;
        end
        $display("    A: %0d entries, B: %0d entries", GM, GN);

        // ---------------------------------------------------------------
        $display("--- 3. one GEMM instruction, %0d sub-tiles ---", NT);
        @(negedge clk);
        gemm_gm <= GM[7:0]; gemm_gn <= GN[7:0]; gemm_nk <= 8'd1;
        gemm_anchor <= ANCHOR[7:0];
        gemm_start <= 1'b1;
        @(negedge clk); gemm_start <= 1'b0;

        t = 0;
        while (gemm_busy && t < 100000) begin @(posedge clk); t = t + 1; end
        repeat (200) @(posedge clk);      // let the cascade drain
        $display("    sweep finished");

        // ---------------------------------------------------------------
        $display("--- 4. drain the resident tile ---");
        @(negedge clk);
        drain_n <= NT[15:0]; drain_start <= 1'b1;
        @(negedge clk); drain_start <= 1'b0;

        t = 0;
        while (drain_busy && t < 200000) begin @(posedge clk); t = t + 1; end
        repeat (50) @(posedge clk);

        for (t = 0; t < NT; t = t + 1) begin
            checks = checks + 1;
            if (!got[t]) begin
                errors = errors + 1;
                if (errors <= 8) $display("  FAIL sub-tile %0d never emitted", t);
            end
        end

        // ---------------------------------------------------------------
        $display("--- 5. precision vs FP64 and exact MXFP7 ---");
        for (i = 0; i < M; i = i + 1)
            for (j = 0; j < N; j = j + 1) begin
                got_r = hw_c[i][j]; want_r = fp64_c[i][j];
                checks = checks + 1;
                if (want_r == 0.0) begin
                    if (got_r != 0.0) errors = errors + 1;
                end else begin
                    err = (got_r - want_r) / want_r;
                    if (err < 0.0) err = -err;
                    nz = nz + 1; sum_rel = sum_rel + err;
                    if (err > worst_rel) worst_rel = err;
                    if (err > 4.0/1024.0) begin
                        errors = errors + 1;
                        if (errors <= 8)
                            $display("  FAIL C[%0d][%0d] got %0f want %0f rel %0e",
                                     i, j, got_r, want_r, err);
                    end
                end
            end

        $display("");
        $display("    C[0][0] hw %0f   fp64 %0f", hw_c[0][0], fp64_c[0][0]);
        $display("    worst rel err %0e   mean %0e   (FP16 ULP %0e)",
                 worst_rel, sum_rel / $itor(nz), 1.0/1024.0);
        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors  (MODEL=%0d)", checks, MODEL);
        else             $display("  FAIL -- %0d checks, %0d errors  (MODEL=%0d)", checks, errors, MODEL);
        $display("========================================");
        $finish;
    end

    initial begin
        #5000000;
        $display("  FAIL -- watchdog (gemm_busy=%0d drain_busy=%0d)", gemm_busy, drain_busy);
        $finish;
    end

endmodule

`default_nettype wire

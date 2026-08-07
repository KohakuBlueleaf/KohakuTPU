// Correctness bench for one cluster: 4 tensor CUs + 1 accumulator CU,
// computing 4 x 32 x 4 per cycle.
//
// Everything here is exact integer arithmetic, so every check is bit-for-bit
// against a model computed in the bench. There is no tolerance and no rounding
// to hide behind -- a mismatch means the datapath is wrong.
//
// What each section is actually trying to break:
//
//   1  smoke        a single 1 in A picks out one row of B; catches gross
//                   wiring errors (row/column swap, chain misindex)
//   2  packing      w_hi = w_lo = -64 with a = -64. This is the case that
//                   rules out S = 20: the packed operand -64*2^S - 64 must
//                   still fit 27 bits signed. If the packing is wrong at all,
//                   it is wrong here first.
//   3  full scale   every element at an extreme, so the K=32 sum reaches
//                   +/-131,072 and exercises the 5 guard bits completely
//   4  random       200 tiles against the model
//   5  borrow       forces the lower field negative on every chain, which is
//                   the only thing the +P[18] borrow correction fixes
//   6  streaming    tiles back to back, one per cycle, to prove the systolic
//                   skew and the cross-TCU W path hold up under a real
//                   pipeline rather than one isolated tile
//   7  scales       non-uniform power-of-two scale per row and column,
//                   accumulated over several K=32 blocks
//
// mx_cluster pairs the core with the EXACT accumulator, whose scale is a plain
// 8-bit exponent rather than the machine's E5M3 -- see mx_acu.v. The scale
// mantissa is checked in mx_acu_fp_tb; what this bench pins down is the
// datapath underneath it.

`default_nettype none
`timescale 1ns/1ps

// Behavioural by default so the arithmetic can be checked with no primitive
// library at all. Compile a file containing `define MX_MODEL 0 ahead of this
// one to run against the real DSP48E2 (needs -L unisims_ver).
`ifndef MX_MODEL
`define MX_MODEL 1
`endif

module mx_cluster_tb;

    localparam integer ACCW  = 48;
    localparam integer MODEL = `MX_MODEL;    // 1 = behavioural, 0 = DSP48E2
    localparam integer LAT   = 19;           // operands -> acc_valid

    reg clk = 0, rst = 1, en = 1;
    always #2 clk = ~clk;

    reg  [895:0] a_in, b_in;
    reg  [31:0]  sa, sb;
    reg  [7:0]   anchor;
    reg          in_valid, acc_clear;
    wire [16*ACCW-1:0] acc_out;
    wire         acc_valid;

    mx_cluster #(.ACCW(ACCW), .MODEL(MODEL)) dut (
        .clk(clk), .rst(rst), .en(en),
        .a_in(a_in), .b_in(b_in),
        .sa(sa), .sb(sb), .anchor(anchor),
        .in_valid(in_valid), .acc_clear(acc_clear),
        .acc_out(acc_out), .acc_valid(acc_valid)
    );

    // ------------------------------------------------------------- model side
    integer signed A [0:3][0:31];
    integer signed B [0:31][0:3];
    integer signed model_acc [0:15];
    integer signed expect_q  [0:511][0:15];   // expected results, in issue order
    integer q_wr, q_rd;

    // Results are captured the moment they appear, by a monitor rather than by
    // the main thread. Under streaming the cluster retires one tile per cycle
    // while the bench is still issuing, so anything that only looks at acc_valid
    // between issues silently drops the first LAT results.
    reg [16*ACCW-1:0] cap_q [0:511];
    integer cap_wr;

    always @(negedge clk) begin
        if (!rst && acc_valid) begin
            cap_q[cap_wr] = acc_out;
            cap_wr = cap_wr + 1;
        end
    end

    integer errors = 0, checks = 0;

    task chk(input integer got, input integer want, input [255:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                if (errors <= 12)
                    $display("  FAIL %0s: got %0d want %0d", what, got, want);
            end
        end
    endtask

    // pack the model arrays into the flat operand busses
    task pack;
        integer i, j, k;
        begin
            for (i = 0; i < 4; i = i + 1)
                for (k = 0; k < 32; k = k + 1)
                    a_in[(i*32+k)*7 +: 7] = A[i][k][6:0];
            for (k = 0; k < 32; k = k + 1)
                for (j = 0; j < 4; j = j + 1)
                    b_in[(k*4+j)*7 +: 7] = B[k][j][6:0];
        end
    endtask

    // exact 4x32x4, then scaled and accumulated exactly as the ACU should
    task model_step(input integer clear);
        integer i, j, k, dot, sh;
        begin
            for (i = 0; i < 4; i = i + 1)
                for (j = 0; j < 4; j = j + 1) begin
                    dot = 0;
                    for (k = 0; k < 32; k = k + 1) dot = dot + A[i][k]*B[k][j];
                    sh = sa[i*8 +: 8] + sb[j*8 +: 8] - anchor;
                    if (clear) model_acc[i*4+j] = dot <<< sh;
                    else       model_acc[i*4+j] = model_acc[i*4+j] + (dot <<< sh);
                end
        end
    endtask

    // Issue one tile and queue what the model says should come out.
    //
    // Driven on the NEGATIVE edge. Setting a signal and clearing it around
    // @(posedge clk) races the DUT: the bench's blocking assignment and the
    // DUT's non-blocking sample both land in the same time step, so whether the
    // pulse is seen at all is undefined. Everything in this bench drives and
    // samples on the negedge so the DUT only ever sees settled values.
    task issue(input integer clear);
        integer m;
        begin
            pack;
            model_step(clear);
            @(negedge clk);
            acc_clear = clear[0];
            in_valid  = 1'b1;
            for (m = 0; m < 16; m = m + 1) expect_q[q_wr][m] = model_acc[m];
            q_wr = q_wr + 1;
            @(negedge clk);
            in_valid  = 1'b0;
            acc_clear = 1'b0;
        end
    endtask

    // Compare the next captured result against the model queue. Waits for the
    // monitor rather than polling acc_valid, so it works identically whether
    // tiles are issued one at a time or back to back.
    task retire(input [255:0] what);
        integer m;
        integer signed got;
        begin
            while (cap_wr <= q_rd) @(negedge clk);
            for (m = 0; m < 16; m = m + 1) begin
                got = $signed(cap_q[q_rd][m*ACCW +: ACCW]);
                chk(got, expect_q[q_rd][m], what);
            end
            q_rd = q_rd + 1;
        end
    endtask

    task fill(input integer av, input integer bv);
        integer i, j, k;
        begin
            for (i = 0; i < 4; i = i + 1)
                for (k = 0; k < 32; k = k + 1) A[i][k] = av;
            for (k = 0; k < 32; k = k + 1)
                for (j = 0; j < 4; j = j + 1) B[k][j] = bv;
        end
    endtask

    task flat_scales;
        begin
            sa = {8'd0, 8'd0, 8'd0, 8'd0};
            sb = {8'd0, 8'd0, 8'd0, 8'd0};
            anchor = 8'd0;
        end
    endtask

    integer i, j, k, t, seed;

    initial begin
        seed = 32'h1234_5678;
        q_wr = 0; q_rd = 0; cap_wr = 0;
        a_in = 0; b_in = 0; in_valid = 0; acc_clear = 0;
        flat_scales;
        // glbl asserts GSR for the first 100 ns, so every unisim register is
        // held in reset regardless of our own rst. Start well clear of it or
        // the first tile silently produces nothing.
        #200;
        repeat (8) @(posedge clk);
        rst = 0;
        repeat (4) @(posedge clk);

        // ---------------------------------------------------------------
        $display("--- 1. smoke: one non-zero in A selects a row of B ---");
        fill(0, 0);
        for (k = 0; k < 32; k = k + 1)
            for (j = 0; j < 4; j = j + 1) B[k][j] = (k + j) % 17 - 8;
        A[2][5] = 3;
        issue(1);
        retire("smoke");

        // ---------------------------------------------------------------
        $display("--- 2. packing worst case: w_hi = w_lo = act = -64 ---");
        fill(-64, -64);
        issue(1);
        retire("all -64");

        // ---------------------------------------------------------------
        $display("--- 3. full-scale sums, all sign combinations ---");
        fill(-64,  63); issue(1); retire("-64 x +63");
        fill( 63, -64); issue(1); retire("+63 x -64");
        fill( 63,  63); issue(1); retire("+63 x +63");

        // ---------------------------------------------------------------
        $display("--- 4. random tiles vs model ---");
        for (t = 0; t < 200; t = t + 1) begin
            for (i = 0; i < 4; i = i + 1)
                for (k = 0; k < 32; k = k + 1)
                    A[i][k] = ($random(seed) & 127) - 64;
            for (k = 0; k < 32; k = k + 1)
                for (j = 0; j < 4; j = j + 1)
                    B[k][j] = ($random(seed) & 127) - 64;
            issue(1);
            retire("random");
        end

        // ---------------------------------------------------------------
        // Every chain's lower field negative, so the borrow into the upper
        // field is exercised on all eight chains at once. Even rows of A are
        // negative (lower field), odd rows positive (upper field).
        $display("--- 5. borrow correction: lower field negative everywhere ---");
        for (t = 0; t < 20; t = t + 1) begin
            for (i = 0; i < 4; i = i + 1)
                for (k = 0; k < 32; k = k + 1)
                    A[i][k] = (i[0] == 0) ? -(($random(seed) & 31) + 1)
                                          :  (($random(seed) & 31) + 1);
            for (k = 0; k < 32; k = k + 1)
                for (j = 0; j < 4; j = j + 1)
                    B[k][j] = ($random(seed) & 31) + 1;
            issue(1);
            retire("borrow");
        end

        // ---------------------------------------------------------------
        // Back to back, one tile per cycle. Results are retired in order as
        // they emerge, which is what proves the pipeline rather than a single
        // isolated tile.
        $display("--- 6. streaming, one tile per cycle ---");
        for (t = 0; t < 32; t = t + 1) begin
            for (i = 0; i < 4; i = i + 1)
                for (k = 0; k < 32; k = k + 1)
                    A[i][k] = ($random(seed) & 127) - 64;
            for (k = 0; k < 32; k = k + 1)
                for (j = 0; j < 4; j = j + 1)
                    B[k][j] = ($random(seed) & 127) - 64;
            // pack AFTER the edge, so the operands and in_valid change in the
            // same instant. Packing first lets the next iteration overwrite
            // a_in/b_in while in_valid is still high for this tile, which pairs
            // every tile's valid with the following tile's data.
            model_step(t == 0);
            @(negedge clk);
            pack;
            acc_clear = (t == 0);
            in_valid  = 1'b1;
            for (i = 0; i < 16; i = i + 1) expect_q[q_wr][i] = model_acc[i];
            q_wr = q_wr + 1;
        end
        @(negedge clk);
        in_valid = 1'b0; acc_clear = 1'b0;
        while (q_rd < q_wr) retire("streamed");

        // ---------------------------------------------------------------
        // Non-uniform power-of-two scale per row and column, accumulated across
        // four K=32 blocks. anchor is chosen so no shift goes negative.
        $display("--- 7. per-row / per-column scales, 4 blocks accumulated ---");
        sa = {8'd5, 8'd4, 8'd3, 8'd2};      // sa[3..0] = 5,4,3,2
        sb = {8'd6, 8'd5, 8'd4, 8'd3};
        anchor = 8'd5;                       // min(sa)+min(sb) = 2+3
        for (t = 0; t < 4; t = t + 1) begin
            for (i = 0; i < 4; i = i + 1)
                for (k = 0; k < 32; k = k + 1)
                    A[i][k] = ($random(seed) & 31) - 16;
            for (k = 0; k < 32; k = k + 1)
                for (j = 0; j < 4; j = j + 1)
                    B[k][j] = ($random(seed) & 31) - 16;
            issue(t == 0);
            retire("scaled accum");
        end

        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors  (MODEL=%0d)", checks, MODEL);
        else             $display("  FAIL -- %0d checks, %0d errors  (MODEL=%0d)", checks, errors, MODEL);
        $display("========================================");
        $finish;
    end

    initial begin
        #2000000;
        $display("WATCHDOG TIMEOUT -- acc_valid never arrived (q_wr=%0d q_rd=%0d)", q_wr, q_rd);
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire

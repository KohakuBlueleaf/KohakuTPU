// One tensor CU in isolation: 4 x 8 x 4, no cluster chaining.
//
// Exists so a cluster failure is attributable. If this passes and the cluster
// bench does not, the fault is in the cross-TCU W path or the operand skew
// between TCUs, not in the DSP packing, the cascade, or the field layout.
//
// Checks the RAW packed partials rather than extracted values, so a fault in
// the ACU cannot mask or cause a fault here:
//
//     part_out[c]  =  U * 2^19  +  L        c = p*4 + j
//     L = sum over k of A[2p  ][k] * B[k][j]
//     U = sum over k of A[2p+1][k] * B[k][j]

`default_nettype none
`timescale 1ns/1ps

`ifndef MX_MODEL
`define MX_MODEL 1
`endif

module mx_tcu_tb;

    localparam integer MODEL = `MX_MODEL;
    localparam integer LAT   = 11;      // operands -> part_out

    reg clk = 0, rst = 1, en = 1;
    always begin
        #2 clk = ~clk;
    end

    reg  [223:0] a_in, b_in;
    wire [383:0] part_out;

    mx_tcu #(.FIRST(1), .MODEL(MODEL)) dut (
        .clk(clk), .rst(rst), .en(en),
        .a_in(a_in), .b_in(b_in),
        .part_in(384'd0),
        .part_out(part_out)
    );

    integer signed A [0:3][0:7];
    integer signed B [0:7][0:3];

    integer errors = 0, checks = 0;

    task chk(input integer signed got, input integer signed want, input [255:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                if (errors <= 16) begin
                    $display("  FAIL %0s: got %0d want %0d", what, got, want);
                end
            end
        end
    endtask

    task pack;
        integer i, j, k;
        begin
            for (i = 0; i < 4; i = i + 1) begin
                for (k = 0; k < 8; k = k + 1) begin
                    a_in[(i*8+k)*7 +: 7] = A[i][k][6:0];
                end
            end
            for (k = 0; k < 8; k = k + 1) begin
                for (j = 0; j < 4; j = j + 1) begin
                    b_in[(k*4+j)*7 +: 7] = B[k][j][6:0];
                end
            end
        end
    endtask

    // drive one tile, wait out the pipeline, check every chain
    task run_tile(input [255:0] what);
        integer i, j, k, p, c, n;
        integer signed dot_lo, dot_hi, got_lo, got_hi;
        reg [47:0] w;
        begin
            pack;
            @(negedge clk);
            // hold the operands stable for the whole fill so the skew chain
            // sees a clean tile rather than a one-cycle pulse
            for (n = 0; n < LAT + 2; n = n + 1) begin
                @(negedge clk);
            end

            for (p = 0; p < 2; p = p + 1) begin
                for (j = 0; j < 4; j = j + 1) begin
                    c = p*4 + j;
                    w = part_out[c*48 +: 48];
                    dot_lo = 0; dot_hi = 0;
                    for (k = 0; k < 8; k = k + 1) begin
                        dot_lo = dot_lo + A[2*p  ][k]*B[k][j];
                        dot_hi = dot_hi + A[2*p+1][k]*B[k][j];
                    end
                    got_lo = $signed(w[18:0]);
                    got_hi = $signed(w[47:19]) + $signed({1'b0, w[18]});
                    chk(got_lo, dot_lo, what);
                    chk(got_hi, dot_hi, what);
                end
            end
        end
    endtask

    task fill(input integer av, input integer bv);
        integer i, j, k;
        begin
            for (i = 0; i < 4; i = i + 1) begin
                for (k = 0; k < 8; k = k + 1) begin
                    A[i][k] = av;
                end
            end
            for (k = 0; k < 8; k = k + 1) begin
                for (j = 0; j < 4; j = j + 1) begin
                    B[k][j] = bv;
                end
            end
        end
    endtask

    integer i, j, k, t, seed;

    // ------------------------------------------------------------- streaming
    // Push NST tiles back to back and check every one. Expected values are
    // queued at issue time and compared LAT cycles later, so the check follows
    // the pipeline instead of waiting for it to drain.
    localparam integer NST = 40;
    integer signed sA [0:NST-1][0:3][0:7];
    integer signed sB [0:NST-1][0:7][0:3];

    task stream_test;
        integer n, i2, j2, k2, p2, c2, idx;
        integer signed dot_lo, dot_hi, got_lo, got_hi;
        reg [47:0] w;
        begin
            for (n = 0; n < NST; n = n + 1) begin
                for (i2 = 0; i2 < 4; i2 = i2 + 1) begin
                    for (k2 = 0; k2 < 8; k2 = k2 + 1) begin
                        sA[n][i2][k2] = ($random(seed) & 127) - 64;
                    end
                end
                for (k2 = 0; k2 < 8; k2 = k2 + 1) begin
                    for (j2 = 0; j2 < 4; j2 = j2 + 1) begin
                        sB[n][k2][j2] = ($random(seed) & 127) - 64;
                    end
                end
            end

            for (n = 0; n < NST + LAT + 2; n = n + 1) begin
                @(negedge clk);
                // drive tile n
                if (n < NST) begin
                    for (i2 = 0; i2 < 4; i2 = i2 + 1) begin
                        for (k2 = 0; k2 < 8; k2 = k2 + 1) begin
                            a_in[(i2*8+k2)*7 +: 7] = sA[n][i2][k2][6:0];
                        end
                    end
                    for (k2 = 0; k2 < 8; k2 = k2 + 1) begin
                        for (j2 = 0; j2 < 4; j2 = j2 + 1) begin
                            b_in[(k2*4+j2)*7 +: 7] = sB[n][k2][j2][6:0];
                        end
                    end
                end
                // check the tile that entered LAT cycles ago
                idx = n - LAT;
                if (idx >= 0 && idx < NST) begin
                    for (p2 = 0; p2 < 2; p2 = p2 + 1) begin
                        for (j2 = 0; j2 < 4; j2 = j2 + 1) begin
                            c2 = p2*4 + j2;
                            w = part_out[c2*48 +: 48];
                            dot_lo = 0; dot_hi = 0;
                            for (k2 = 0; k2 < 8; k2 = k2 + 1) begin
                                dot_lo = dot_lo + sA[idx][2*p2  ][k2]*sB[idx][k2][j2];
                                dot_hi = dot_hi + sA[idx][2*p2+1][k2]*sB[idx][k2][j2];
                            end
                            got_lo = $signed(w[18:0]);
                            got_hi = $signed(w[47:19]) + $signed({1'b0, w[18]});
                            chk(got_lo, dot_lo, "stream lo");
                            chk(got_hi, dot_hi, "stream hi");
                        end
                    end
                end
            end
        end
    endtask

    initial begin
        seed = 32'h0BAD_F00D;
        a_in = 0; b_in = 0;
        // glbl asserts GSR for the first 100 ns, so every unisim register is
        // held in reset regardless of our own rst. Start well clear of it or
        // the first tile silently produces nothing.
        #200;
        repeat (8) @(posedge clk);
        rst = 0;
        repeat (4) @(posedge clk);

        $display("--- 1. single element ---");
        fill(0, 0);
        A[0][0] = 5; B[0][0] = 7;
        run_tile("single");

        $display("--- 2. packing worst case, all -64 ---");
        fill(-64, -64);
        run_tile("all -64");

        $display("--- 3. sign combinations ---");
        fill(-64, 63); run_tile("-64 x 63");
        fill(63, -64); run_tile("63 x -64");
        fill(63,  63); run_tile("63 x 63");

        $display("--- 4. random ---");
        for (t = 0; t < 50; t = t + 1) begin
            for (i = 0; i < 4; i = i + 1) begin
                for (k = 0; k < 8; k = k + 1) begin
                    A[i][k] = ($random(seed) & 127) - 64;
                end
            end
            for (k = 0; k < 8; k = k + 1) begin
                for (j = 0; j < 4; j = j + 1) begin
                    B[k][j] = ($random(seed) & 127) - 64;
                end
            end
            run_tile("random");
        end

        // Streaming: a new tile every cycle. run_tile holds its operands stable
        // for the whole pipeline fill, so it cannot see a skew error -- every
        // stage happens to be looking at the same tile. Only varying operands
        // every cycle exercises the per-stage delay.
        $display("--- 5. streaming, a new tile every cycle ---");
        stream_test;

        $display("========================================");
        if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors  (MODEL=%0d)", checks, MODEL);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors  (MODEL=%0d)", checks, errors, MODEL);
        end
        $display("========================================");
        $finish;
    end

    initial begin
        #500000;
        $display("WATCHDOG TIMEOUT");
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire

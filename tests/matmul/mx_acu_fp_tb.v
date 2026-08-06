// Accumulator CU: operations, resident tile, peer transfer, and precision.
//
// Two ground truths are used, and the distinction matters:
//
//   EXACT INT   what the cluster's integer datapath produces, computed in the
//               bench with plain integer arithmetic. The ACU's job is to carry
//               this into floating point without losing more than it must.
//   FP64        the same sum computed in `real`. This is what the hardware is
//               ultimately approximating.
//
// Checking against FP64 alone would confuse the ACU's rounding with the
// quantisation already baked into the integers it is handed. Checking against
// the exact integer alone would say nothing about whether the FP24 pipeline is
// accurate enough to be worth using.

`default_nettype none
`timescale 1ns/1ps

module mx_acu_fp_tb;

    localparam integer DEPTH = 16;
    localparam integer TAW   = 4;
`ifndef ACC_MW
`define ACC_MW 16
`endif
    localparam integer MW = `ACC_MW;      // accumulator mantissa bits
    localparam integer AW = MW + 8;

    localparam [2:0] OP_NOP=0, OP_LOAD=1, OP_ADD=2, OP_ADD_PEER=3,
                     OP_SEND=4, OP_EMIT=5, OP_FWD=6;

    reg clk = 0, rst = 1, en = 1;
    always #2 clk = ~clk;

    reg  [383:0] part_in;
    reg  [31:0]  sa, sb;
    reg  [7:0]   anchor;
    reg  [2:0]   op;
    reg  [TAW-1:0] tile_addr;
    reg          cmd_valid;
    reg  [16*AW-1:0] peer_in;
    wire [16*AW-1:0] peer_out;
    wire         peer_valid;
    wire [255:0] emit_out;
    wire         emit_valid;
    wire         busy;

    mx_acu_fp #(.DEPTH(DEPTH), .ACC_MW(MW)) dut (
        .clk(clk), .rst(rst), .en(en),
        .part_in(part_in), .sa(sa), .sb(sb), .anchor(anchor),
        .op(op), .tile_addr(tile_addr), .cmd_valid(cmd_valid),
        .peer_in(peer_in), .peer_out(peer_out), .peer_valid(peer_valid),
        .emit_out(emit_out), .emit_valid(emit_valid), .busy(busy)
    );

    integer errors = 0, checks = 0;
    real    worst_rel = 0.0;

    // ------------------------------------------------------------- helpers
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

    function real fp24_to_real(input [63:0] f);
        real m; integer e;
        begin
            if (f[AW-2 -: 7] == 7'd0) fp24_to_real = 0.0;
            else begin
                m = 1.0 + $itor(f[MW-1:0]) / (2.0 ** MW);
                e = f[AW-2 -: 7] - 63;
                fp24_to_real = m * (2.0 ** e);
                if (f[AW-1]) fp24_to_real = -fp24_to_real;
            end
        end
    endfunction

    // Pack 16 integer results into the 8 packed chains the cluster produces:
    //   chain c = p*4 + j   holds row 2p in [18:0] and row 2p+1 in [47:19]
    integer signed iv [0:15];
    task pack_parts;
        integer p, j;
        reg signed [47:0] w;
        begin
            for (p = 0; p < 2; p = p + 1)
                for (j = 0; j < 4; j = j + 1) begin
                    w = ($signed(iv[(2*p+1)*4+j]) <<< 19) + $signed(iv[(2*p)*4+j]);
                    part_in[(p*4+j)*48 +: 48] = w;
                end
        end
    endtask

    task do_op(input [2:0] o, input [TAW-1:0] addr);
        begin
            @(negedge clk);
            op = o; tile_addr = addr; cmd_valid = 1'b1;
            @(negedge clk);
            cmd_valid = 1'b0; op = OP_NOP;
            // pipeline is 6 deep, and EMIT/SEND fold three banks in two passes
            // three cycles apart -- give it room rather than tracking exactly
            repeat (16) @(negedge clk);
        end
    endtask

    task chk_rel(input real got, input real want, input real tol, input [255:0] what);
        real err, den;
        begin
            checks = checks + 1;
            den = (want < 0.0) ? -want : want;
            if (den < 1.0e-30) den = 1.0;
            err = (got - want) / den;
            if (err < 0.0) err = -err;
            if (err > worst_rel) worst_rel = err;
            if (err > tol) begin
                errors = errors + 1;
                if (errors <= 10)
                    $display("  FAIL %0s: got %0e want %0e relerr %0e",
                             what, got, want, err);
            end
        end
    endtask

    integer i, j, t, b, seed;
    real    model [0:15];          // FP64 ground truth
    real    fp16_eps, want_r, got_r;
    integer signed exact [0:15];   // exact integer ground truth

    task rand_block;
        integer ii, jj;
        begin
            for (ii = 0; ii < 4; ii = ii + 1)
                for (jj = 0; jj < 4; jj = jj + 1)
                    iv[ii*4+jj] = ($random(seed) % 131072);   // a K=32 block, 18 bits
            pack_parts;
        end
    endtask

    initial begin
        seed = 32'h5EED_1234;
        fp16_eps = 1.0 / 1024.0;      // one FP16 ULP
        part_in = 0; sa = 0; sb = 0; anchor = 0; op = OP_NOP;
        tile_addr = 0; cmd_valid = 0; peer_in = 0;
        #200;
        repeat (8) @(posedge clk);
        rst = 0;
        repeat (4) @(posedge clk);

        // -----------------------------------------------------------------
        $display("--- 1. LOAD then EMIT, flat scales ---");
        for (i = 0; i < 16; i = i + 1) iv[i] = (i + 1) * 100;
        pack_parts;
        sa = 0; sb = 0; anchor = 0;
        do_op(OP_LOAD, 0);
        do_op(OP_EMIT, 0);
        for (i = 0; i < 16; i = i + 1)
            chk_rel(fp16_to_real(emit_out[i*16 +: 16]), $itor((i+1)*100),
                    fp16_eps, "load/emit");

        // -----------------------------------------------------------------
        $display("--- 2. per-row / per-column scales applied ---");
        sa = {8'd4, 8'd3, 8'd2, 8'd1};   // sa[3..0]
        sb = {8'd3, 8'd2, 8'd1, 8'd0};
        anchor = 8'd1;
        for (i = 0; i < 16; i = i + 1) iv[i] = (i % 7) + 1;
        pack_parts;
        do_op(OP_LOAD, 1);
        do_op(OP_EMIT, 1);
        for (i = 0; i < 4; i = i + 1)
            for (j = 0; j < 4; j = j + 1) begin
                want_r = $itor((i*4+j) % 7 + 1)
                       * (2.0 ** (sa[i*8 +: 8] + sb[j*8 +: 8] - anchor));
                chk_rel(fp16_to_real(emit_out[(i*4+j)*16 +: 16]), want_r,
                        fp16_eps, "scaled");
            end

        // -----------------------------------------------------------------
        // Sweep K as 32 blocks into one resident sub-tile, exactly as a real
        // matmul would. Both ground truths are tracked.
        // A K=1024 sweep (32 blocks of K=32) with int7 operands reaches ~4.2M,
        // which FP16 cannot hold at all. The anchor is what brings it into
        // range: the accumulator is anchor-relative, so the emitted FP16 is the
        // result divided by 2^anchor and the host scales back. Choosing an
        // anchor is therefore not optional bookkeeping -- it is what makes an
        // FP16 result representable.
        $display("--- 3. 32-block accumulation vs FP64 and exact int ---");
        sa = 0; sb = 0; anchor = 8;
        for (i = 0; i < 16; i = i + 1) begin
            model[i] = 0.0; exact[i] = 0;
        end
        for (b = 0; b < 32; b = b + 1) begin
            rand_block;
            for (i = 0; i < 16; i = i + 1) begin
                model[i] = model[i] + $itor(iv[i]) * (2.0 ** -8);
                exact[i] = exact[i] + iv[i];
            end
            do_op(b == 0 ? OP_LOAD : OP_ADD, 2);
        end
        do_op(OP_EMIT, 2);
        for (i = 0; i < 16; i = i + 1) begin
            // FP16 out, so one FP16 ULP plus the FP24 accumulation error
            chk_rel(fp16_to_real(emit_out[i*16 +: 16]), model[i],
                    fp16_eps * 4.0, "accum vs fp64");
            // and the FP64 model must equal the exact integer sum scaled by
            // the anchor: any gap here would mean the bench itself had drifted
            if ($itor(exact[i]) * (2.0 ** -8) != model[i]) begin
                errors = errors + 1;
                $display("  FAIL bench model drift at %0d", i);
            end
            checks = checks + 1;
        end

        // -----------------------------------------------------------------
        $display("--- 4. resident tile: 16 sub-tiles kept independently ---");
        sa = 0; sb = 0; anchor = 0;
        for (t = 0; t < DEPTH; t = t + 1) begin
            for (i = 0; i < 16; i = i + 1) iv[i] = (t + 1) * (i + 1);
            pack_parts;
            do_op(OP_LOAD, t[TAW-1:0]);
        end
        for (t = 0; t < DEPTH; t = t + 1) begin
            do_op(OP_EMIT, t[TAW-1:0]);
            for (i = 0; i < 16; i = i + 1)
                chk_rel(fp16_to_real(emit_out[i*16 +: 16]),
                        $itor((t+1)*(i+1)), fp16_eps, "resident tile");
        end

        // -----------------------------------------------------------------
        // SEND hands a tile out as FP24; ADD_PEER folds one in. This is how a
        // matmul split across clusters reduces.
        $display("--- 5. peer transfer: SEND then ADD_PEER ---");
        sa = 0; sb = 0; anchor = 0;
        for (i = 0; i < 16; i = i + 1) iv[i] = (i + 1) * 7;
        pack_parts;
        do_op(OP_LOAD, 3);
        do_op(OP_SEND, 3);
        for (i = 0; i < 16; i = i + 1)
            chk_rel(fp24_to_real({{(64-AW){1'b0}}, peer_out[i*AW +: AW]}), $itor((i+1)*7),
                    4.0/(2.0**MW), "peer send");

        peer_in = peer_out;               // loop it straight back in
        for (i = 0; i < 16; i = i + 1) iv[i] = (i + 1) * 3;
        pack_parts;
        do_op(OP_LOAD, 4);
        do_op(OP_ADD_PEER, 4);
        do_op(OP_EMIT, 4);
        for (i = 0; i < 16; i = i + 1)
            chk_rel(fp16_to_real(emit_out[i*16 +: 16]), $itor((i+1)*10),
                    fp16_eps, "peer add");

        // -----------------------------------------------------------------
        // The converter must clamp, not wrap. A wrapped exponent would turn a
        // large positive result into a small one silently.
        $display("--- 6. FP16 emission saturates rather than wrapping ---");
        sa = 0; sb = 0; anchor = 0;
        for (i = 0; i < 16; i = i + 1) iv[i] = 131071;
        pack_parts;
        do_op(OP_LOAD, 6);
        for (b = 0; b < 8; b = b + 1) do_op(OP_ADD, 6);   // ~1.05e6, way past FP16
        do_op(OP_EMIT, 6);
        for (i = 0; i < 16; i = i + 1) begin
            got_r = fp16_to_real(emit_out[i*16 +: 16]);
            checks = checks + 1;
            if (got_r != 65504.0) begin
                errors = errors + 1;
                if (errors <= 10)
                    $display("  FAIL saturate: got %0f want 65504", got_r);
            end
        end

        $display("--- 7. FWD passes the chain straight out ---");
        sa = 0; sb = 0; anchor = 0;
        for (i = 0; i < 16; i = i + 1) iv[i] = (i + 1) * 11;
        pack_parts;
        do_op(OP_FWD, 5);
        for (i = 0; i < 16; i = i + 1)
            chk_rel(fp24_to_real({{(64-AW){1'b0}}, peer_out[i*AW +: AW]}), $itor((i+1)*11),
                    4.0/(2.0**MW), "fwd");

        $display("========================================");
        $display("  ACC_MW=%0d (%0d-bit accumulator)  worst rel err %0e  (FP16 ULP %0e)",
                 MW, AW, worst_rel, fp16_eps);
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end

    initial begin
        #3000000;
        $display("WATCHDOG TIMEOUT");
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire

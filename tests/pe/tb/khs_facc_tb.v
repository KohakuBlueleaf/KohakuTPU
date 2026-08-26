// khs_facc_tb -- the rotating accumulator, checked partial by partial.
//
// One slot, because the rotation is per accumulator and not per slot: SLOTS > 1
// is replication and would only make a failure harder to read.
//
// What it proves is the thing a whole-kernel test would hide: after N
// accumulates, partial k holds exactly the operations whose index was k mod
// NPART, in order. A write index off by one still produces a plausible total
// and the wrong partials, so the check is per partial and never on the sum.

`timescale 1ns/1ps
`default_nettype none

`ifndef PE_DIR
 `define PE_DIR "../../tests/pe/build"
`endif

module khs_facc_tb;
    localparam integer NPART = 16;
    localparam integer ALAT  = 6;
    localparam integer NOPS  = 200;
    localparam integer MAXV  = 4096;

    reg clk = 0, resetn = 0;
    always begin
        #1 clk = ~clk;
    end

    reg  [31:0] va [0:MAXV-1];
    reg  [31:0] vb [0:MAXV-1];
    reg  [31:0] vexp [0:NPART-1];
    integer nvec;

    reg         acc_valid = 0;
    wire [31:0] rd_part;
    wire [3:0]  rd_idx;

    wire [31:0] lane_out;
    wire        lane_ovld;

    reg  [31:0] a = 0, b = 0;
    reg         in_valid = 0;

    // EVERY INPUT DRIVEN, and the outputs named even when unused. An
    // unconnected input is `z`, the op decode muxes on it, and every result
    // comes back X -- which reads as a dead RAM rather than a missing pin.
    localparam [4:0] FOP_FMA = 5'd6;

    rv_fpu u_lane (
        .clk(clk), .rst(!resetn),
        .in_valid(in_valid), .op(FOP_FMA),
        .a(a), .b(b), .c(rd_part),
        .out_valid(lane_ovld), .y(lane_out), .out_pred()
    );

    reg  [3:0] fold_idx = 0;
    reg        do_zero = 0;
    wire [31:0] fold_part;
    wire        busy_sweep;

    khs_facc #(.SLOTS(1), .NACC(1), .NPART(NPART), .ALAT(ALAT)) u_facc (
        .clk(clk), .resetn(resetn),
        .acc_valid(acc_valid), .acc_sel(1'b0),
        .rd_part(rd_part), .rd_idx(rd_idx),
        .wb_valid(lane_ovld), .wb_data(lane_out),
        .do_zero(do_zero), .do_seed(1'b0), .ctl_sel(1'b0), .seed_data(32'd0),
        .fold_sel(1'b0), .fold_idx(fold_idx), .fold_part(fold_part),
        .busy_sweep(busy_sweep), .sweep_idx()
    );

    integer f, code, i, errors;
    reg [31:0] wa, wb_, wy;
    initial begin
        // TWO FILES, not one. `$fscanf("%h %h")` treats a newline as ordinary
        // whitespace, so a single-value expectation line following the operand
        // pairs gets read AS an operand pair -- the operand loop runs past the
        // end and the expectations read back as X. One record shape per file.
        f = $fopen({`PE_DIR, "/khd/fp32/facc_ops.txt"}, "r");
        if (f == 0) begin
            $display("FAIL: no vectors at %s/khd/fp32 -- run python tests/pe/tools/khs_facc_vec.py",
                     `PE_DIR);
            $finish;
        end
        nvec = 0; code = 2;
        while ((nvec < MAXV) && (code == 2)) begin
            code = $fscanf(f, "%h %h\n", wa, wb_);
            if (code == 2) begin
                va[nvec] = wa; vb[nvec] = wb_;
                nvec = nvec + 1;
            end
        end
        $fclose(f);
        f = $fopen({`PE_DIR, "/khd/fp32/facc_exp.txt"}, "r");
        if (f == 0) begin $display("FAIL: no expectation file"); $finish; end
        for (i = 0; i < NPART; i = i + 1) begin
            code = $fscanf(f, "%h\n", wy);
            vexp[i] = wy;
        end
        $fclose(f);
        $display("--- %0d ops, %0d partials ---", NOPS, NPART);

        errors = 0;
        #200;
        @(posedge clk); resetn = 1;
        repeat (2) @(posedge clk);

        // ZERO FIRST. The partials are a memory and a memory has no reset, so
        // `vfaccz` is what makes them zero -- reset only cleared them while
        // they were a flop array.
        @(negedge clk); do_zero = 1;
        @(negedge clk); do_zero = 0;
        // RISE, THEN FALL. `busy_sweep` is `sweep`, which does not rise until
        // the posedge after `do_zero` is sampled -- so a bare `wait
        // (!busy_sweep)` is ALREADY TRUE and returns before the sweep starts.
        wait (busy_sweep);
        wait (!busy_sweep);
        repeat (2) @(posedge clk);

        // Back to back, which is the whole point: a deep lane accumulating at
        // one per cycle only works because the partial moves every cycle.
        for (i = 0; i < NOPS; i = i + 1) begin
            @(negedge clk);
            acc_valid = 1; in_valid = 1;
            a = va[i]; b = vb[i];
        end
        @(negedge clk);
        acc_valid = 0; in_valid = 0;

        repeat (ALAT + 8) @(posedge clk);

        for (i = 0; i < NPART; i = i + 1) begin
            @(negedge clk);
            fold_idx = i[3:0];
            @(posedge clk);
            if (fold_part !== vexp[i]) begin
                errors = errors + 1;
                $display("  PARTIAL %0d: %08h want %08h", i, fold_part, vexp[i]);
            end
        end

        if (errors == 0) begin
            $display("PASS -- %0d partials all correct", NPART);
        end
        else begin
            $display("FAIL -- %0d of %0d partials wrong", errors, NPART);
        end
        $finish;
    end

endmodule

`default_nettype wire

// khs_ffold_tb -- accumulate, then fold, and check the value a kernel would
// actually read.
//
// khs_facc_tb proves the partials are right. This proves the thing built ON
// them: NPART dependent steps through the same lane, in index order, producing
// one value. Both are needed -- correct partials combined in the wrong order
// give a wrong answer that still looks like a float.

`timescale 1ns/1ps
`default_nettype none

`ifndef PE_DIR
 `define PE_DIR "../../tests/pe/build"
`endif

module khs_ffold_tb;
    localparam integer NPART = 16;
    localparam integer ALAT  = 6;
    localparam integer NOPS  = 200;
    localparam integer MAXV  = 4096;
    localparam [31:0] F32_ONE = 32'h3F80_0000;

    reg clk = 0, resetn = 0;
    always begin
        #1 clk = ~clk;
    end

    reg  [31:0] va [0:MAXV-1];
    reg  [31:0] vb [0:MAXV-1];
    reg  [31:0] vfold;
    integer nvec;

    reg         acc_valid = 0;
    reg         in_valid  = 0;
    reg  [31:0] a = 0, b = 0;
    reg         folding = 0;
    reg  [31:0] total = 0;

    wire [31:0] rd_part, fold_part;
    wire [3:0]  rd_idx;
    wire [31:0] lane_out;
    wire        lane_ovld;

    wire        f_busy, f_done, f_iss;
    wire [3:0]  f_idx;
    reg         f_start = 0;
    reg         do_zero = 0;
    wire        sweep_busy;

    // During a fold the lane's addend is the running total; otherwise it is the
    // partial the accumulator selected.
    wire [31:0] lane_c = folding ? total : rd_part;

    // EVERY INPUT DRIVEN. An unconnected input is `z`, the op decode muxes on
    // it, and every result comes back X.
    localparam [4:0] FOP_FMA = 5'd6;

    rv_fpu u_lane (
        .clk(clk), .rst(!resetn),
        .in_valid(in_valid | f_iss), .op(FOP_FMA),
        .a(f_iss ? fold_part : a), .b(f_iss ? F32_ONE : b), .c(lane_c),
        .out_valid(lane_ovld), .y(lane_out), .out_pred()
    );

    // The accumulator must not capture FOLD results: they are not accumulates,
    // and writing them back would overwrite the partials being read.
    wire acc_wb = lane_ovld && !folding;

    khs_facc #(.SLOTS(1), .NACC(1), .NPART(NPART), .ALAT(ALAT)) u_facc (
        .clk(clk), .resetn(resetn),
        .acc_valid(acc_valid), .acc_sel(1'b0),
        .rd_part(rd_part), .rd_idx(rd_idx),
        .wb_valid(acc_wb), .wb_data(lane_out),
        // THE PARTIALS ARE A RAM AND RESET DOES NOT CLEAR THEM. Tied to 0 here,
        // nothing ever initialised them, so the fold summed X.
        .do_zero(do_zero), .do_seed(1'b0), .ctl_sel(1'b0), .seed_data(32'd0),
        .fold_sel(1'b0), .fold_idx(f_idx), .fold_part(fold_part),
        .busy_sweep(sweep_busy), .sweep_idx()
    );

    khs_ffold #(.NPART(NPART), .ALAT(ALAT)) u_fold (
        .clk(clk), .resetn(resetn),
        .start(f_start), .busy(f_busy), .done(f_done),
        .part_idx(f_idx), .iss_valid(f_iss), .iss_raw()
    );

    always @(posedge clk) begin
        if (resetn && folding && lane_ovld) begin
            total <= lane_out;
        end
    end

    integer f, code, i, errors;
    reg [31:0] wa, wb_, wy;
    initial begin
        // One record shape per file: see khs_facc_tb.
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
        end
        code = $fscanf(f, "%h\n", wy);
        vfold = wy;
        $fclose(f);
        $display("--- %0d ops, fold of %0d partials ---", NOPS, NPART);

        errors = 0;
        #200;
        @(posedge clk); resetn = 1;
        repeat (2) @(posedge clk);

        // ZERO THE PARTIALS FIRST, and wait for the sweep to RISE then FALL:
        // `busy_sweep` does not rise until the posedge after `do_zero`, so
        // waiting only for it to be low returns before the sweep has started.
        @(negedge clk); do_zero = 1;
        @(negedge clk); do_zero = 0;
        wait (sweep_busy);
        wait (!sweep_busy);
        repeat (2) @(posedge clk);

        for (i = 0; i < NOPS; i = i + 1) begin
            @(negedge clk);
            acc_valid = 1; in_valid = 1;
            a = va[i]; b = vb[i];
        end
        @(negedge clk);
        acc_valid = 0; in_valid = 0;
        repeat (ALAT + 8) @(posedge clk);

        @(negedge clk); folding = 1; total = 32'd0; f_start = 1;
        @(negedge clk); f_start = 0;
        wait (f_done);
        repeat (2) @(posedge clk);

        if (total !== vfold) begin
            errors = 1;
            $display("  FOLD: %08h want %08h", total, vfold);
        end
        if (errors == 0) begin
            $display("PASS -- fold matches the model");
        end
        else begin
            $display("FAIL -- fold mismatch");
        end
        $finish;
    end

endmodule

`default_nettype wire

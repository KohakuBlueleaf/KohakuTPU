// khd_ffold_tb -- accumulate, then fold, and check the value a kernel would
// actually read.
//
// khd_facc_tb proves the partials are right. This proves the thing built ON
// them: NPART dependent steps through the same lane, in index order, producing
// one value. Both are needed -- correct partials combined in the wrong order
// give a wrong answer that still looks like a float.

`timescale 1ns/1ps
`default_nettype none

`ifndef PE_DIR
 `define PE_DIR "../../tests/pe/build"
`endif

module khd_ffold_tb;
    localparam integer NPART = 16;
    localparam integer ALAT  = 15;
    localparam integer NOPS  = 200;
    localparam integer MAXV  = 4096;

    reg clk = 0, resetn = 0;
    always #1 clk = ~clk;

    reg  [15:0] va [0:MAXV-1];
    reg  [15:0] vb [0:MAXV-1];
    reg  [23:0] vfold;
    integer nvec;

    reg         acc_valid = 0;
    reg         in_valid  = 0;
    reg  [15:0] a = 0, b = 0;
    reg         folding = 0;
    reg  [23:0] total = 0;

    wire [23:0] rd_part, fold_part;
    wire [3:0]  rd_idx;
    wire [23:0] lane_out;
    wire        lane_ovld;

    wire        f_busy, f_done, f_iss, f_raw;
    wire [3:0]  f_idx;
    reg         f_start = 0;

    // During a fold the lane's addend is the running total; otherwise it is the
    // partial the accumulator selected.
    wire [23:0] lane_c = folding ? total : rd_part;

`ifndef MX_MODEL
 `define MX_MODEL 1
`endif

    khd_f16_lane #(.PIPE_MUX(1), .MODEL(`MX_MODEL)) u_lane (
        .clk(clk), .rst(!resetn),
        .in_valid(in_valid | f_iss), .a(a), .b(b), .c(lane_c),
        .raw_e8(f_raw), .a_e8(fold_part),
        .out_valid(lane_ovld), .out(lane_out)
    );

    // The accumulator must not capture FOLD results: they are not accumulates,
    // and writing them back would overwrite the partials being read.
    wire acc_wb = lane_ovld && !folding;

    khd_facc #(.SLOTS(1), .NACC(1), .NPART(NPART), .ALAT(ALAT)) u_facc (
        .clk(clk), .resetn(resetn),
        .acc_valid(acc_valid), .acc_sel(1'b0),
        .rd_part(rd_part), .rd_idx(rd_idx),
        .wb_valid(acc_wb), .wb_data(lane_out),
        .do_zero(1'b0), .do_seed(1'b0), .ctl_sel(1'b0), .seed_data(24'd0),
        .fold_sel(1'b0), .fold_idx(f_idx), .fold_part(fold_part),
        .busy_sweep()
    );

    khd_ffold #(.NPART(NPART), .ALAT(ALAT)) u_fold (
        .clk(clk), .resetn(resetn),
        .start(f_start), .busy(f_busy), .done(f_done),
        .part_idx(f_idx), .iss_valid(f_iss), .iss_raw(f_raw)
    );

    always @(posedge clk) if (resetn && folding && lane_ovld) total <= lane_out;

    integer f, code, i, errors;
    reg [31:0] wa, wb_, wy;
    initial begin
        // One record shape per file: see khd_facc_tb.
        f = $fopen({`PE_DIR, "/khd/f16/facc_ops.txt"}, "r");
        if (f == 0) begin
            $display("FAIL: no vectors at %s/khd/f16 -- run python tests/pe/tools/khd_facc_vec.py",
                     `PE_DIR);
            $finish;
        end
        nvec = 0; code = 2;
        while ((nvec < MAXV) && (code == 2)) begin
            code = $fscanf(f, "%h %h\n", wa, wb_);
            if (code == 2) begin
                va[nvec] = wa[15:0]; vb[nvec] = wb_[15:0];
                nvec = nvec + 1;
            end
        end
        $fclose(f);
        f = $fopen({`PE_DIR, "/khd/f16/facc_exp.txt"}, "r");
        if (f == 0) begin $display("FAIL: no expectation file"); $finish; end
        for (i = 0; i < NPART; i = i + 1) code = $fscanf(f, "%h\n", wy);
        code = $fscanf(f, "%h\n", wy);
        vfold = wy[23:0];
        $fclose(f);
        $display("--- %0d ops, fold of %0d partials ---", NOPS, NPART);

        errors = 0;
        #200;
        @(posedge clk); resetn = 1;
        repeat (2) @(posedge clk);

        for (i = 0; i < NOPS; i = i + 1) begin
            @(negedge clk);
            acc_valid = 1; in_valid = 1;
            a = va[i]; b = vb[i];
        end
        @(negedge clk);
        acc_valid = 0; in_valid = 0;
        repeat (ALAT + 8) @(posedge clk);

        @(negedge clk); folding = 1; total = 24'd0; f_start = 1;
        @(negedge clk); f_start = 0;
        wait (f_done);
        repeat (2) @(posedge clk);

        if (total !== vfold) begin
            errors = 1;
            $display("  FOLD: %06h want %06h", total, vfold);
        end
        if (errors == 0) $display("PASS -- fold matches the model");
        else             $display("FAIL -- fold mismatch");
        $finish;
    end

endmodule

`default_nettype wire

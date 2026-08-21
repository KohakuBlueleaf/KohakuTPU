// khd_facc_tb -- the rotating accumulator, checked partial by partial.
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

module khd_facc_tb;
    localparam integer NPART = 16;
    localparam integer ALAT  = 15;
    localparam integer NOPS  = 200;
    localparam integer MAXV  = 4096;

    reg clk = 0, resetn = 0;
    always #1 clk = ~clk;

    reg  [15:0] va [0:MAXV-1];
    reg  [15:0] vb [0:MAXV-1];
    reg  [23:0] vexp [0:NPART-1];
    integer nvec;

    reg         acc_valid = 0;
    wire [23:0] rd_part;
    wire [3:0]  rd_idx;

    reg         wb_valid = 0;
    wire [23:0] lane_out;
    wire        lane_ovld;

    reg  [15:0] a = 0, b = 0;
    reg         in_valid = 0;

`ifndef MX_MODEL
 `define MX_MODEL 1
`endif

    // raw_e8/a_e8 DRIVEN, not left off: an unconnected input is `z`, `a_sel`
    // muxes on it, and every result comes back X -- which reads as a dead RAM.
    khd_f16_lane #(.PIPE_MUX(1), .MODEL(`MX_MODEL)) u_lane (
        .clk(clk), .rst(!resetn),
        .in_valid(in_valid), .a(a), .b(b), .c(rd_part),
        .raw_e8(1'b0), .a_e8(24'd0),
        .out_valid(lane_ovld), .out(lane_out)
    );

    reg  [3:0] fold_idx = 0;
    reg        do_zero = 0;
    wire [23:0] fold_part;
    wire        busy_sweep;

    khd_facc #(.SLOTS(1), .NACC(1), .NPART(NPART), .ALAT(ALAT)) u_facc (
        .clk(clk), .resetn(resetn),
        .acc_valid(acc_valid), .acc_sel(1'b0),
        .rd_part(rd_part), .rd_idx(rd_idx),
        .wb_valid(lane_ovld), .wb_data(lane_out),
        .do_zero(do_zero), .do_seed(1'b0), .ctl_sel(1'b0), .seed_data(24'd0),
        .fold_sel(1'b0), .fold_idx(fold_idx), .fold_part(fold_part),
        .busy_sweep(busy_sweep)
    );

    integer f, code, i, errors;
    reg [31:0] wa, wb_, wy;
    initial begin
        // TWO FILES, not one. `$fscanf("%h %h")` treats a newline as ordinary
        // whitespace, so a single-value expectation line following the operand
        // pairs gets read AS an operand pair -- the operand loop runs past the
        // end and the expectations read back as X. One record shape per file.
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
        for (i = 0; i < NPART; i = i + 1) begin
            code = $fscanf(f, "%h\n", wy);
            vexp[i] = wy[23:0];
        end
        $fclose(f);
        $display("--- %0d ops, %0d partials ---", NOPS, NPART);

        errors = 0;
        #200;
        @(posedge clk); resetn = 1;
        repeat (2) @(posedge clk);

        // ZERO FIRST. The partials are a memory and a memory has no reset, so
        // `vfaccz` is what makes them zero -- reset only cleared them while
        // they were a flop array. A kernel that accumulated without it would
        // read whatever the RAM powered up holding.
        @(negedge clk); do_zero = 1;
        @(negedge clk); do_zero = 0;
        wait (!busy_sweep);
        repeat (2) @(posedge clk);

        // Back to back, which is the whole point: a 15-deep lane accumulating
        // at one per cycle only works because the partial moves every cycle.
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
                $display("  PARTIAL %0d: %06h want %06h", i, fold_part, vexp[i]);
            end
        end

        if (errors == 0) $display("PASS -- %0d partials all correct", NPART);
        else             $display("FAIL -- %0d of %0d partials wrong", errors, NPART);
        $finish;
    end

endmodule

`default_nettype wire

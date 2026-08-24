// khs_float_lane_tb -- the float tier's arithmetic against the golden model,
// bit for bit.
//
// It answers the one question the model cannot answer about itself: the model's
// FMA is written from the DEFINITION (multiply exactly, add exactly, round
// once), and `vec_alu` is a fourteen-stage pipeline with its own alignment
// window and its own specials. Those two agreeing is a claim, and this is the
// test of it.
//
// THIS RUNS THE FP16 EDGE. `wide` is tied 0 here because the vectors are FP16
// triples; the FP32 edge is exercised end to end by the SIMT PE's gpu_f32
// shader, which carries the range and truncation witnesses.
//
// Vectors come from tests/pe/tools/khs_float_vec.py: a, b, c and the expected
// result, one per line, hex. A mismatch prints all four so the failing triple
// can be reproduced in Python directly.

`timescale 1ns/1ps
`default_nettype none

`ifndef PE_DIR
 `define PE_DIR "../../tests/pe/build"
`endif

module khs_float_lane_tb;
    localparam integer MAXV = 20000;
    localparam integer ALAT = 15;       // vec_alu at PIPE_MUX=1, plus nothing

    reg clk = 0, rst = 1;
    always begin
        #1 clk = ~clk;
    end

    reg  [15:0] va [0:MAXV-1];
    reg  [15:0] vb [0:MAXV-1];
    reg  [23:0] vc [0:MAXV-1];
    reg  [23:0] vy [0:MAXV-1];
    integer nvec;

    reg         in_valid = 0;
    reg  [15:0] a = 0, b = 0;
    reg  [23:0] c = 0;
    wire        out_valid;
    wire [23:0] out;

    // The same switch every other bench here uses: `xsim.py --model 0` swaps in
    // the real DSP48E2 and brings unisims and glbl with it.
`ifndef MX_MODEL
 `define MX_MODEL 1
`endif
    localparam integer MODEL = `MX_MODEL;

    // raw_e8/a_e8 DRIVEN: this bench was green until the fold's raw operand
    // path landed on the lane, and an unconnected input is `z`, so a_sel went X.
    // FMA, because these vectors are FMA triples. The elementwise group's other
    // operations are the SAME lane with a different `op`, and khs_falu is where
    // that selection lives; this bench is about the arithmetic under it.
    localparam [4:0] FOP_FMA = 5'd6;

    khs_float_lane #(.PIPE_MUX(1), .MODEL(MODEL)) u_dut (
        .clk(clk), .rst(rst),
        .in_valid(in_valid), .op(FOP_FMA), .wide(1'b0),
        .a({16'd0, a}), .b({16'd0, b}), .c(c),
        .raw_e8(1'b0), .a_e8(24'd0),
        .out_valid(out_valid), .out(out), .out_pred()
    );

    // Results correlate by ORDER, not by a delay line. The lane is in-order at
    // II = 1, so the nth valid output is the nth issued vector -- and a counter
    // cannot be off by one the way a hand-built delay of `ALAT` can, which is
    // exactly the mistake this line replaces.
    integer issued, retired, errors;

    integer f, code, i;
    reg [31:0] wa, wb, wc, wy;
    initial begin
        f = $fopen({`PE_DIR, "/khd/f16/lane.txt"}, "r");
        if (f == 0) begin
            $display("FAIL: no vectors at %s/khd/f16 -- run python tests/pe/tools/khs_float_vec.py",
                     `PE_DIR);
            $finish;
        end
        nvec = 0;
        code = 4;
        while ((nvec < MAXV) && (code == 4)) begin
            code = $fscanf(f, "%h %h %h %h\n", wa, wb, wc, wy);
            if (code == 4) begin
                va[nvec] = wa[15:0];
                vb[nvec] = wb[15:0];
                vc[nvec] = wc[23:0];
                vy[nvec] = wy[23:0];
                nvec = nvec + 1;
            end
        end
        $fclose(f);
        $display("--- %0d vectors ---", nvec);

        issued = 0; retired = 0; errors = 0;
        // 200 ns before anything issues, because a build against the real
        // DSP48E2 carries unisim's GSR: it holds every primitive's registers
        // reset until 100 ns, and vectors issued inside that window come back
        // as zero no matter what the arithmetic does.
        #200;
        @(posedge clk);
        rst = 0;
        repeat (2) @(posedge clk);

        // One per cycle: II = 1 is the property being exercised as much as the
        // arithmetic. A lane that only works when spaced out would pass a
        // one-at-a-time bench and fail every kernel.
        for (i = 0; i < nvec; i = i + 1) begin
            @(negedge clk);
            in_valid = 1;
            a = va[i]; b = vb[i]; c = vc[i];
            issued = i + 1;
        end
        @(negedge clk);
        in_valid = 0;

        repeat (ALAT + 8) @(posedge clk);

        if (retired != nvec) begin
            $display("FAIL: %0d of %0d results came back", retired, nvec);
        end
        if (errors == 0 && retired == nvec) begin
            $display("PASS -- %0d vectors, 0 mismatches", nvec);
        end
        else begin
            $display("FAIL -- %0d mismatches of %0d", errors, retired);
        end
        $finish;
    end

    integer k;
    always @(posedge clk) if (!rst && out_valid) begin
        k = retired;
        if (k < nvec) begin
            if (out !== vy[k]) begin
                errors = errors + 1;
                if (errors <= 20) begin
                    $display("  MISMATCH %0d: a=%04h b=%04h c=%06h -> %06h want %06h",
                             k, va[k], vb[k], vc[k], out, vy[k]);
                end
            end
        end
        retired = retired + 1;
    end

endmodule

`default_nettype wire

// rv_fpu against a double-precision reference, on the BITS.
//
// EQUALITY, NOT A TOLERANCE. D3D11 requires 0.5 ULP on add/sub/mul, which for a
// correctly rounded unit means the exact binary32 pattern; a tolerance would
// hide the one thing this unit exists to prove. The vectors carry the expected
// word, computed once in float64 and rounded to binary32 -- which is what fused
// means -- with denormals flushed to sign-preserved zero as the spec mandates.

`timescale 1ns / 1ps
`default_nettype none

`ifndef PE_DIR
 `define PE_DIR "../../tests/pe/build"
`endif

module rv_fpu_tb;
    // select -> exp/product -> align -> add -> lead1 -> normalise: six stages,
    // with round and pack combinational off the last.
    localparam integer LAT = 6;

    reg clk = 1'b0;
    always begin
        #1 clk = ~clk;
    end
    reg rst = 1'b1;

    integer nvec, i, errors, checks, shown;
    reg [4:0]  v_op   [0:65535];
    reg [31:0] v_a    [0:65535];
    reg [31:0] v_b    [0:65535];
    reg [31:0] v_c    [0:65535];
    reg [31:0] v_want [0:65535];

    // 34 HEX CHARS = 136 BITS, not 144: $readmemh right-aligns, so a wider
    // register silently shifts every field.
    reg [135:0] raw [0:65535];
    reg [31:0]  nraw [0:0];

    reg         in_valid;
    reg  [4:0]  op;
    reg  [31:0] a, b, c;
    wire        out_valid;
    wire [31:0] y;
    wire        out_pred;

    rv_fpu u_dut (
        .clk(clk), .rst(rst), .in_valid(in_valid), .op(op),
        .a(a), .b(b), .c(c),
        .out_valid(out_valid), .y(y), .out_pred(out_pred)
    );

    // The issue index trails the result by the pipeline depth, so a result is
    // matched to the vector that produced it rather than to the cycle.
    integer issued, retired;
    reg [31:0] exp_q [0:15];
    reg [4:0]  op_q  [0:15];
    reg [31:0] idx_q [0:15];

    task load;
        begin
            // PE_DIR arrives through a file, not `-d`: a string macro cannot
            // survive xvlog's command file and every $readmemh becomes a
            // "syntax error near '/'".
            $readmemh({`PE_DIR, "/fpu/fp32n.hex"}, nraw);
            nvec = nraw[0];
            $readmemh({`PE_DIR, "/fpu/fp32.hex"}, raw);
            for (i = 0; i < nvec; i = i + 1) begin
                v_op[i]   = raw[i][135:128];
                v_a[i]    = raw[i][127:96];
                v_b[i]    = raw[i][95:64];
                v_c[i]    = raw[i][63:32];
                v_want[i] = raw[i][31:0];
            end
        end
    endtask

    initial begin
        load;
        errors = 0; checks = 0; shown = 0;
        issued = 0; retired = 0;
        in_valid = 1'b0; op = 5'd0; a = 0; b = 0; c = 0;
        repeat (4) @(posedge clk);
        rst = 1'b0;
        @(posedge clk);

        // One vector per cycle: II=1 is the claim, so the bench issues that way.
        for (i = 0; i < nvec; i = i + 1) begin
            @(negedge clk);
            in_valid <= 1'b1;
            op <= v_op[i]; a <= v_a[i]; b <= v_b[i]; c <= v_c[i];
        end
        @(negedge clk);
        in_valid <= 1'b0;
        repeat (LAT + 8) @(posedge clk);

        $display("========================================");
        if (checks == 0) begin
            $display("  FAIL -- the bench made no checks");
        end
        else if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $display("========================================");
        $finish;
    end

    // The expected word, delayed to meet its own result.
    integer k;
    always @(posedge clk) begin
        if (!rst) begin
            exp_q[0] <= (in_valid) ? v_want[issued] : 32'hDEAD_BEEF;
            op_q[0]  <= (in_valid) ? v_op[issued]   : 5'd31;
            idx_q[0] <= issued;
            for (k = 1; k < 16; k = k + 1) begin
                exp_q[k] <= exp_q[k-1];
                op_q[k]  <= op_q[k-1];
                idx_q[k] <= idx_q[k-1];
            end
            if (in_valid) begin
                issued <= issued + 1;
            end
            if (out_valid) begin
                checks <= checks + 1;
                if (y !== exp_q[LAT-1]) begin
                    errors <= errors + 1;
                    if (shown < 12) begin
                        shown <= shown + 1;
                        $display("  FAIL vec %0d op %0d: got %08x want %08x",
                                 idx_q[LAT-1], op_q[LAT-1], y, exp_q[LAT-1]);
                    end
                end
                retired <= retired + 1;
            end
        end
    end

    initial begin
        #4000000;
        $display("  FAIL WATCHDOG -- rv_fpu bench never finished");
        $finish;
    end
endmodule

`default_nettype wire

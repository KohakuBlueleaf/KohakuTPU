// khs_fp32_sfu against the golden model, on the BITS.
//
// EQUALITY, NOT A TOLERANCE. The table and the evaluation are integer and the
// model runs the same expression, so a tolerance here would only hide the two
// things that can go wrong: a stale table, and a shift or a width that does not
// match. How far the ANSWER is from the true function is a separate number, and
// tests/pe/tools/khs_seed_emit.py --report is what measures it.

`timescale 1ns / 1ps
`default_nettype none

`ifndef PE_DIR
 `define PE_DIR "../../tests/pe/build"
`endif

module khs_fp32_sfu_tb;
    localparam integer LAT = 10;

    reg clk = 1'b0;
    always begin
        #1 clk = ~clk;
    end
    reg rst = 1'b1;

    integer nvec, i, errors, checks, shown;
    reg [1:0]  v_f    [0:65535];
    reg [31:0] v_a    [0:65535];
    reg [31:0] v_want [0:65535];

    // 18 HEX CHARS = 72 BITS: $readmemh right-aligns, so a wider register
    // silently shifts every field.
    reg [71:0] raw [0:65535];
    reg [31:0] nraw [0:0];

    reg        in_valid;
    reg [1:0]  fsel;
    reg [31:0] a;
    wire       out_valid;
    wire [31:0] y;

    khs_fp32_sfu u_dut (
        .clk(clk), .rst(rst), .in_valid(in_valid), .fsel(fsel), .a(a),
        .out_valid(out_valid), .y(y)
    );

    integer issued, retired;
    reg [31:0] exp_q [0:15];
    reg [31:0] arg_q [0:15];
    reg [1:0]  fs_q  [0:15];
    reg [31:0] idx_q [0:15];

    task load;
        begin
            $readmemh({`PE_DIR, "/fpu/sfu32n.hex"}, nraw);
            nvec = nraw[0];
            $readmemh({`PE_DIR, "/fpu/sfu32.hex"}, raw);
            for (i = 0; i < nvec; i = i + 1) begin
                v_f[i]    = raw[i][65:64];
                v_a[i]    = raw[i][63:32];
                v_want[i] = raw[i][31:0];
            end
        end
    endtask

    initial begin
        load;
        errors = 0; checks = 0; shown = 0;
        issued = 0; retired = 0;
        in_valid = 1'b0; fsel = 2'd0; a = 0;
        repeat (4) @(posedge clk);
        rst = 1'b0;
        @(posedge clk);

        for (i = 0; i < nvec; i = i + 1) begin
            @(negedge clk);
            in_valid <= 1'b1;
            fsel <= v_f[i]; a <= v_a[i];
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

    integer k;
    always @(posedge clk) begin
        if (!rst) begin
            exp_q[0] <= (in_valid) ? v_want[issued] : 32'hDEAD_BEEF;
            arg_q[0] <= (in_valid) ? v_a[issued]    : 32'd0;
            fs_q[0]  <= (in_valid) ? v_f[issued]    : 2'd0;
            idx_q[0] <= issued;
            for (k = 1; k < 16; k = k + 1) begin
                exp_q[k] <= exp_q[k-1];
                arg_q[k] <= arg_q[k-1];
                fs_q[k]  <= fs_q[k-1];
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
                        $display("  FAIL vec %0d fsel %0d a %08x: got %08x want %08x",
                                 idx_q[LAT-1], fs_q[LAT-1], arg_q[LAT-1],
                                 y, exp_q[LAT-1]);
                    end
                end
                retired <= retired + 1;
            end
        end
    end

    initial begin
        #4000000;
        $display("  FAIL WATCHDOG -- khs_fp32_sfu bench never finished");
        $finish;
    end
endmodule

`default_nettype wire

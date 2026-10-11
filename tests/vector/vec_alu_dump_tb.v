// The vector ALU on a file of operands, its results to a file: the RTL side of
// a bit-for-bit diff against a software model of the lane.
//
//   +in=FILE   one 80-bit hex word per instruction: {3'b0, op[4:0], a, b, c}
//   +out=FILE  one line per result: `out[23:0] out_pred`, in issue order
//   +n=N       instructions in FILE
//
// Streamed at one instruction per cycle, as vec_lanes issues them.

`default_nettype none
`timescale 1ns/1ps

module vec_alu_dump_tb;

    localparam integer MAXN = 1 << 21;

    reg         clk = 1'b0, rst = 1'b1;
    reg         in_valid = 1'b0;
    reg  [4:0]  op = 5'd0;
    reg  [23:0] a = 24'd0, b = 24'd0, c = 24'd0;
    wire        out_valid, out_pred;
    wire [23:0] out;

    always begin
        #1 clk = ~clk;
    end

    vec_alu #(.MODEL(1)) dut (
        .clk(clk), .rst(rst), .in_valid(in_valid), .op(op),
        .a(a), .b(b), .c(c),
        .out_valid(out_valid), .out(out), .out_pred(out_pred)
    );

    reg [79:0] mem [0:MAXN-1];
    reg [8*512-1:0] fin, fout;
    integer n, i, nout, fo;

    always @(posedge clk) begin
        if (!rst && out_valid) begin
            $fwrite(fo, "%06h %0d\n", out, out_pred);
            nout = nout + 1;
        end
    end

    initial begin
        nout = 0;
        if (!$value$plusargs("in=%s", fin) || !$value$plusargs("out=%s", fout)
            || !$value$plusargs("n=%d", n)) begin
            $display("  FAIL need +in= +out= +n=");
            $finish;
        end
        if (n > MAXN) begin
            $display("  FAIL n=%0d over %0d", n, MAXN);
            $finish;
        end
        $readmemh(fin, mem, 0, n - 1);
        fo = $fopen(fout, "w");
        repeat (4) @(negedge clk);
        rst = 1'b0;
        repeat (4) @(negedge clk);
        for (i = 0; i < n; i = i + 1) begin
            @(negedge clk);
            in_valid = 1'b1;
            {op, a, b, c} = mem[i][76:0];
        end
        @(negedge clk);
        in_valid = 1'b0;
        repeat (40) @(negedge clk);
        $fclose(fo);
        $display("    issued %0d retired %0d", n, nout);
        if (nout == n) begin
            $display("  PASS");
        end else begin
            $display("  FAIL retired %0d of %0d", nout, n);
        end
        $finish;
    end

endmodule

`default_nettype wire

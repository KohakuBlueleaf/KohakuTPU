// vec_alu: free-running RTL against the gated model, every output at every
// edge, through bursts of random opcodes and operands and constant stretches.

`timescale 1ns/1ps
`default_nettype none

module gate_vec_alu_tb;
    localparam integer CYCLES = 200000;

    reg clk = 1'b0;
    always #1 clk = ~clk;

    reg        rst = 1'b1, iv = 1'b0;
    reg [4:0]  op = 0;
    reg [23:0] a = 0, b = 0, c = 0;

    wire        v_ref, v_g, p_ref, p_g;
    wire [23:0] o_ref, o_g;

    vec_alu__rtl #(.MODEL(1)) u_ref (
        .clk(clk), .rst(rst), .in_valid(iv), .op(op), .a(a), .b(b), .c(c),
        .out_valid(v_ref), .out(o_ref), .out_pred(p_ref)
    );
    vec_alu #(.MODEL(1)) u_g (
        .clk(clk), .rst(rst), .in_valid(iv), .op(op), .a(a), .b(b), .c(c),
        .out_valid(v_g), .out(o_g), .out_pred(p_g)
    );

    // Specials as often as ordinary values: zero, +-inf, NaN, one, extremes.
    function [23:0] operand(input integer r);
        case ($unsigned(r) % 8)
            0: operand = 24'h000000;
            1: operand = 24'h7F8000;
            2: operand = 24'hFF8000;
            3: operand = 24'h7FC000;
            4: operand = 24'h3F8000;
            default: operand = $random;
        endcase
    endfunction

    integer cyc = 0, left = 0, mism = 0, withheld = 0;
    reg     busy = 1'b0;

    always @(posedge clk) begin
        cyc <= cyc + 1;
        if (!u_g.u_gate.run_l) withheld <= withheld + 1;
        if (cyc == 20) rst <= 1'b0;
        if (left == 0) begin
            busy <= !busy;
            left <= busy ? 1 + ($unsigned($random) % 150) : 1 + ($unsigned($random) % 60);
        end else begin
            left <= left - 1;
        end
        if (busy) begin
            op <= ($unsigned($random) % 4 == 0) ? $random : 5'd16 + ($unsigned($random) % 4);
            if ($unsigned($random) % 2) op <= $unsigned($random) % 14;
            a  <= operand($random);
            b  <= operand($random);
            c  <= operand($random);
            iv <= ($unsigned($random) % 10) < 7;
        end else if (($unsigned($random) % 200) == 0) begin
            c[$unsigned($random) % 24] <= ~c[$unsigned($random) % 24];
        end
        if (cyc > 30000 && cyc < 30010) rst <= 1'b1;
        if (cyc == 30010) rst <= 1'b0;
    end

    always @(negedge clk) begin
        if (cyc > 40 && (v_ref !== v_g || o_ref !== o_g || p_ref !== p_g)) begin
            if (mism < 5) $display("MISMATCH cyc=%0d out %h/%h", cyc, o_ref, o_g);
            mism <= mism + 1;
        end
        if (cyc == CYCLES) begin
            $display("%s gate vec_alu edges=%0d withheld=%0d mismatches=%0d",
                     (mism == 0 && withheld > CYCLES / 10) ? "PASS" : "FAIL",
                     cyc, withheld, mism);
            $finish;
        end
    end
endmodule

`default_nettype wire

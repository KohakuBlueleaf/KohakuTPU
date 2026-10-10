// mx_cluster_core: free-running RTL against the gated model, every output at
// every edge, through bursts of random operands and constant stretches.

`timescale 1ns/1ps
`default_nettype none

module gate_core_tb;
    localparam integer CYCLES = 200000;

    reg clk = 1'b0;
    always #1 clk = ~clk;

    reg         rst = 1'b1, en = 1'b1, iv = 1'b0, fs = 1'b0;
    reg [895:0] a = 0, b = 0;

    wire [383:0] p_ref, p_g;
    wire         v_ref, v_g, f_ref, f_g;

    mx_cluster_core__rtl #(.MODEL(1)) u_ref (
        .clk(clk), .rst(rst), .en(en), .a_in(a), .b_in(b), .in_valid(iv), .in_first(fs),
        .part_out(p_ref), .part_valid(v_ref), .part_first(f_ref)
    );
    mx_cluster_core #(.MODEL(1)) u_g (
        .clk(clk), .rst(rst), .en(en), .a_in(a), .b_in(b), .in_valid(iv), .in_first(fs),
        .part_out(p_g), .part_valid(v_g), .part_first(f_g)
    );

    integer cyc = 0, left = 0, mism = 0, withheld = 0, k;
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
            for (k = 0; k < 28; k = k + 1) begin
                a[k*32 +: 32] <= $random;
                b[k*32 +: 32] <= $random;
            end
            iv <= ($unsigned($random) % 10) < 7;
            fs <= ($unsigned($random) % 4) == 0;
            en <= ($unsigned($random) % 16) != 0;
        end else if (($unsigned($random) % 200) == 0) begin
            a[$unsigned($random) % 896] <= ~a[$unsigned($random) % 896];
        end
        if (cyc > 30000 && cyc < 30010) rst <= 1'b1;
        if (cyc == 30010) rst <= 1'b0;
    end

    always @(negedge clk) begin
        if (cyc > 40 && (p_ref !== p_g || v_ref !== v_g || f_ref !== f_g)) begin
            if (mism < 5) $display("MISMATCH cyc=%0d valid %b/%b", cyc, v_ref, v_g);
            mism <= mism + 1;
        end
        if (cyc == CYCLES) begin
            $display("%s gate mx_cluster_core edges=%0d withheld=%0d mismatches=%0d",
                     (mism == 0 && withheld > CYCLES / 10) ? "PASS" : "FAIL",
                     cyc, withheld, mism);
            $finish;
        end
    end
endmodule

`default_nettype wire

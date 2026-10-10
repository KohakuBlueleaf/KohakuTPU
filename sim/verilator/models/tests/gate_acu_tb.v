// mx_acu_fp: free-running RTL against the gated model, every output at every
// edge; commands in bursts, addresses rotating over the 16 tiles so REUSE_MIN
// holds.

`timescale 1ns/1ps
`default_nettype none

module gate_acu_tb;
    localparam integer CYCLES = 100000;
    localparam integer TW = 16 * 22;

    reg clk = 1'b0;
    always #1 clk = ~clk;

    reg          rst = 1'b1, en = 1'b1, cmd = 1'b0;
    reg [383:0]  part = 0;
    reg [31:0]   sa = 0, sb = 0;
    reg [7:0]    anchor = 0;
    reg [2:0]    op = 0;
    reg [3:0]    addr = 0;
    reg [TW-1:0] peer = 0;

    wire [TW-1:0]  po_ref, po_g;
    wire [255:0]   eo_ref, eo_g;
    wire           pv_ref, pv_g, ev_ref, ev_g, b_ref, b_g;

    mx_acu_fp__rtl #(.DEPTH(16), .ACC_MW(14)) u_ref (
        .clk(clk), .rst(rst), .en(en), .part_in(part), .sa(sa), .sb(sb), .anchor(anchor),
        .op(op), .tile_addr(addr), .cmd_valid(cmd), .peer_in(peer),
        .peer_out(po_ref), .peer_valid(pv_ref), .emit_out(eo_ref), .emit_valid(ev_ref),
        .busy(b_ref)
    );
    mx_acu_fp #(.DEPTH(16), .ACC_MW(14)) u_g (
        .clk(clk), .rst(rst), .en(en), .part_in(part), .sa(sa), .sb(sb), .anchor(anchor),
        .op(op), .tile_addr(addr), .cmd_valid(cmd), .peer_in(peer),
        .peer_out(po_g), .peer_valid(pv_g), .emit_out(eo_g), .emit_valid(ev_g),
        .busy(b_g)
    );

    integer k;
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
            cmd    <= ($unsigned($random) % 10) < 8;
            op     <= $random;
            addr   <= addr + 4'd1;
            sa     <= $random; sb <= $random;
            anchor <= $random;
            for (k = 0; k < 12; k = k + 1) part[k*32 +: 32] <= $random;
            for (k = 0; k < 11; k = k + 1) peer[k*32 +: 32] <= $random;
        end else begin
            cmd <= 1'b0;
            if (($unsigned($random) % 200) == 0) part[$unsigned($random) % 384] <= $random;
        end
        if (cyc > 30000 && cyc < 30010) rst <= 1'b1;
        if (cyc == 30010) rst <= 1'b0;
    end

    always @(negedge clk) begin
        if (cyc > 40 && (po_ref !== po_g || pv_ref !== pv_g || eo_ref !== eo_g
                         || ev_ref !== ev_g || b_ref !== b_g)) begin
            if (mism < 5) $display("MISMATCH cyc=%0d emit %b/%b peer %b/%b busy %b/%b", cyc,
                                   ev_ref, ev_g, pv_ref, pv_g, b_ref, b_g);
            mism <= mism + 1;
        end
        if (cyc == CYCLES) begin
            $display("%s gate mx_acu_fp edges=%0d withheld=%0d mismatches=%0d",
                     (mism == 0 && withheld > CYCLES / 10) ? "PASS" : "FAIL",
                     cyc, withheld, mism);
            $finish;
        end
    end
endmodule

`default_nettype wire

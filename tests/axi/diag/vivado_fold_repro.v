// Vivado 2024.2 RTL-elaboration miscompile, minimal case (the kx_pxache
// response-ready bug on the last partition).
//
//   rdy = loc ? acc : (up ? (1'b0 && !up_b) : (dn && !dn_b));
//
// `up` is (own > 2'd3), never true, so rdy must reduce to
// loc ? acc : (dn && !dn_b). With engine A instantiated BEFORE engine B (the
// default), `synth_design -rtl` already elaborates rdy as loc & acc: the dn arm
// is gone and B's valid never clears on a remote take. With the two instance
// statements swapped (-d B_FIRST) it elaborates and synthesizes correctly.
// Nothing else differs. No warning is printed either way.
//
// Measured (xcvu13p-fhgb2104-2L-e, synth_design -mode out_of_context, funcsim
// netlist in xsim, fold_repro_tb):
//   RTL, A first       PASS        netlist, A first   FAIL (b_val stuck at 1)
//   RTL, B first       PASS        netlist, B first   PASS
`default_nettype none
module fr_eng (
    input  wire clk, input wire rstn, input wire set_v, input wire [1:0] set_id,
    input  wire rdy, output reg val, output reg [1:0] id
);
    always @(posedge clk) begin
        if (!rstn) begin val <= 1'b0; id <= 2'd0; end
        else begin
            if (val && rdy) begin val <= 1'b0; end
            if (set_v && !val) begin val <= 1'b1; id <= set_id; end
        end
    end
endmodule

module fold_repro (
    input  wire       clk, rstn,
    input  wire       a_set, b_set,
    input  wire [1:0] a_id, b_id_in,
    input  wire       acc,        // local accept
    input  wire       dn,         // remote take
    output wire       b_val_o, rdy_o
);
    localparam [1:0] HERE = 2'd3;          // the last of four partitions
    wire       a_val, b_val, rdy;
    wire [1:0] a_own, b_own;
`ifdef B_FIRST
    fr_eng u_b (.clk(clk), .rstn(rstn), .set_v(b_set), .set_id(b_id_in), .rdy(rdy),  .val(b_val), .id(b_own));
    fr_eng u_a (.clk(clk), .rstn(rstn), .set_v(a_set), .set_id(a_id),    .rdy(1'b0), .val(a_val), .id(a_own));
`else
    fr_eng u_a (.clk(clk), .rstn(rstn), .set_v(a_set), .set_id(a_id),    .rdy(1'b0), .val(a_val), .id(a_own));
    fr_eng u_b (.clk(clk), .rstn(rstn), .set_v(b_set), .set_id(b_id_in), .rdy(rdy),  .val(b_val), .id(b_own));
`endif
    wire loc  = (b_own == HERE);  wire up  = (b_own > HERE);       // up: never
    wire aloc = (a_own == HERE);  wire aup = (a_own > HERE);
    wire up_b = a_val && aup;
    wire dn_b = a_val && !aup && !aloc;
`ifdef SOP
    // the same function as one OR of guarded terms (no constant-select mux)
    assign rdy = (loc && acc && b_own == HERE) || (!loc && !up && dn && !dn_b);
`else
    assign rdy = loc ? (acc && b_own == HERE) : (up ? (1'b0 && !up_b) : (dn && !dn_b));
`endif
    assign rdy_o   = rdy;
    assign b_val_o = b_val;
endmodule

// B holds a remote response (owner 2); the remote side takes it (dn = 1) while
// A is idle. b_val must drop on the next edge. The reset is held past glbl's
// 100 ns GSR so the same bench runs over a funcsim netlist.
`timescale 1ns/1ps
module fold_repro_tb;
    reg clk = 0, rstn = 0, a_set = 0, b_set = 0, acc = 0, dn = 0;
    reg [1:0] a_id = 0, b_id = 0;
    wire b_val, rdy;
    always #5 clk = ~clk;
    fold_repro dut (.clk(clk), .rstn(rstn), .a_set(a_set), .b_set(b_set), .a_id(a_id),
                    .b_id_in(b_id), .acc(acc), .dn(dn), .b_val_o(b_val), .rdy_o(rdy));
    initial begin
`ifdef DUMP
        $dumpfile("fold.vcd");
        $dumpvars(0, fold_repro_tb);
`endif
        #200 rstn = 1;
        @(negedge clk) begin b_set = 1; b_id = 2'd2; end
        @(negedge clk) b_set = 0;
        @(negedge clk) dn = 1;
        @(negedge clk) dn = 0;
        @(negedge clk);
        if (b_val === 1'b0) $display("PASS -- b_val retired on the remote take");
        else                $display("FAIL -- b_val stuck at %b after the remote take (rdy was dropped)", b_val);
        $finish;
    end
endmodule
`default_nettype wire

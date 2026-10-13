// v2_alu under a quiescence gate. A feed-forward pipeline, 15 deep at
// PIPE_MUX=1; docs/models.md.

`default_nettype none

// The RTL's own default, which v2_alu.v takes from the same macro.
`ifdef VEC_NO_PIPE_MUX
  `define VLT_V2PM_INIT 0
`else
  `define VLT_V2PM_INIT 1
`endif

module v2_alu #(
    parameter integer MODEL     = 0,
    parameter integer HAS_EXPD  = 1,
    parameter integer TAU       = 8,
    parameter integer PIPE_MUX  = `VLT_V2PM_INIT,
    parameter integer HAS_UNARY = 1,
    parameter integer HAS_FNMA  = 1,
    parameter integer HAS_SEL   = 1,
    parameter integer HAS_POLY  = 1,
    parameter integer DLY_FF    = 6,
    parameter         TAB_PRIM  = "block"
)(
    input  wire        clk,
    input  wire        rst,
    input  wire        in_valid,
    input  wire [4:0]  op,
    input  wire [23:0] a,
    input  wire [23:0] b,
    input  wire [23:0] c,
    output wire        out_valid,
    output wire [23:0] out,
    output wire        out_pred,
    output wire        out_ovf
);
    wire gclk;
    vlt_qgate #(.W(2 + 5 + 3*24), .SETTLE(32)) u_gate (
        .clk(clk), .in({rst, in_valid, op, a, b, c}), .idle(1'b1), .gclk(gclk)
    );

    v2_alu__rtl #(
        .MODEL(MODEL), .HAS_EXPD(HAS_EXPD), .TAU(TAU), .PIPE_MUX(PIPE_MUX),
        .HAS_UNARY(HAS_UNARY), .HAS_FNMA(HAS_FNMA), .HAS_SEL(HAS_SEL),
        .HAS_POLY(HAS_POLY), .DLY_FF(DLY_FF), .TAB_PRIM(TAB_PRIM)
    ) u_rtl (
        .clk(gclk), .rst(rst), .in_valid(in_valid), .op(op),
        .a(a), .b(b), .c(c),
        .out_valid(out_valid), .out(out), .out_pred(out_pred), .out_ovf(out_ovf)
    );
endmodule

`undef VLT_V2PM_INIT

`default_nettype wire

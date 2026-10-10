// mx_acu_fp under a quiescence gate, idle while no command is presented: the
// pipeline drains, the tile RAM is written only by a valid command, and
// `busy_tail` counts down to zero; docs/models.md.

`default_nettype none

module mx_acu_fp #(
    parameter integer DEPTH     = 16,
    parameter integer ACC_MW    = 14,
    parameter         TILE_PRIM = "block"
)(
    input  wire         clk,
    input  wire         rst,
    input  wire         en,
    input  wire [383:0] part_in,
    input  wire [31:0]  sa,
    input  wire [31:0]  sb,
    input  wire [7:0]   anchor,
    input  wire [2:0]   op,
    input  wire [((DEPTH <= 1) ? 1 : $clog2(DEPTH))-1:0] tile_addr,
    input  wire         cmd_valid,
    input  wire [16*(ACC_MW+8)-1:0] peer_in,
    output wire [16*(ACC_MW+8)-1:0] peer_out,
    output wire                     peer_valid,
    output wire [255:0] emit_out,
    output wire         emit_valid,
    output wire         busy
);
    localparam integer TAW = (DEPTH <= 1) ? 1 : $clog2(DEPTH);
    localparam integer TW  = 16 * (ACC_MW + 8);

    wire gclk;
    vlt_qgate #(.W(2 + 384 + 32 + 32 + 8 + 3 + TAW + 1 + TW), .SETTLE(32)) u_gate (
        .clk(clk),
        .in({rst, en, part_in, sa, sb, anchor, op, tile_addr, cmd_valid, peer_in}),
        .idle(!cmd_valid), .gclk(gclk)
    );

    mx_acu_fp__rtl #(.DEPTH(DEPTH), .ACC_MW(ACC_MW), .TILE_PRIM(TILE_PRIM)) u_rtl (
        .clk(gclk), .rst(rst), .en(en),
        .part_in(part_in), .sa(sa), .sb(sb), .anchor(anchor),
        .op(op), .tile_addr(tile_addr), .cmd_valid(cmd_valid),
        .peer_in(peer_in), .peer_out(peer_out), .peer_valid(peer_valid),
        .emit_out(emit_out), .emit_valid(emit_valid), .busy(busy)
    );
endmodule

`default_nettype wire

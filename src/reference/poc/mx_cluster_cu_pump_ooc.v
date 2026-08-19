// MEASUREMENT TOP ONLY. Never instantiated in a mesh.

// An OOC clock PORT is unbuffered while the BUFGCE_DIV beside it is global, so
// the pair measured +3.144 / -3.392 ns skewed: the network, not the circuit.

// In a mesh clk2x reaches the cluster ALREADY on a global net, so the delay is
// common-mode. This puts it back by buffering clk2x before the divider sees it.

`default_nettype none

module mx_cluster_cu_pump_ooc #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer CU_X       = 0,
    parameter integer CU_Y       = 0,
    parameter integer MEM_X      = 1,
    parameter integer MEM_Y      = 1,
    parameter integer TILES      = 256,
    parameter integer GA         = 32,
    parameter integer GB         = 32,
    parameter integer ACC_MW     = 14,
    parameter integer INST_DEPTH = 32,
    parameter integer RECV_DEPTH = 64,
    parameter integer MODEL      = 0,
    parameter         L1_PRIM    = "distributed",
    parameter         TILE_PRIM  = "block",
    parameter         RECV_MEM   = "block"
)(
    input  wire                   clk2x,
    input  wire                   div_clr,
    input  wire                   resetn,

    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,

    output wire [15:0]            fills_done,
    output wire [15:0]            gemms_done,
    output wire [15:0]            drains_done
);
    wire clk2x_g;
`ifdef SYNTHESIS
    BUFGCE #(.CE_TYPE("SYNC"), .IS_CE_INVERTED(1'b0), .IS_I_INVERTED(1'b0))
        u_bufg (.I(clk2x), .CE(1'b1), .O(clk2x_g));
`else
    assign clk2x_g = clk2x;
`endif

    mx_cluster_cu_pump #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .CU_X(CU_X), .CU_Y(CU_Y), .MEM_X(MEM_X), .MEM_Y(MEM_Y),
        .TILES(TILES), .GA(GA), .GB(GB), .ACC_MW(ACC_MW),
        .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH),
        .MODEL(MODEL), .L1_PRIM(L1_PRIM), .TILE_PRIM(TILE_PRIM),
        .RECV_MEM(RECV_MEM)
    ) u_cu (
        .clk2x(clk2x_g), .div_clr(div_clr), .resetn(resetn), .clk1x(),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid),
        .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(noc_out_busy),
        .fills_done(fills_done), .gemms_done(gemms_done),
        .drains_done(drains_done)
    );

endmodule

`default_nettype wire

// MEASUREMENT TOP ONLY: the pumped CU with BOTH clocks as PORTS.

// An UNPLACED global net is estimated as fabric routing -- 2.584 ns at fanout
// 15,641 -- so the BUFGCE_DIV arm needs place_design; two ports do not.

`default_nettype none

module mx_cluster_cu_pump2_ooc #(
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
    input  wire                   clk1x,
    input  wire                   clk2x,
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
    mx_cluster_cu #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .CU_X(CU_X), .CU_Y(CU_Y), .MEM_X(MEM_X), .MEM_Y(MEM_Y),
        .TILES(TILES), .GA(GA), .GB(GB), .ACC_MW(ACC_MW),
        .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH),
        .MODEL(MODEL), .L1_PRIM(L1_PRIM), .TILE_PRIM(TILE_PRIM),
        .RECV_MEM(RECV_MEM), .PUMP(1)
    ) u_cu (
        .clk(clk1x), .clk2x(clk2x), .unit_clk(clk1x), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid),
        .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(noc_out_busy),
        .fills_done(fills_done), .gemms_done(gemms_done),
        .drains_done(drains_done)
    );

endmodule

`default_nettype wire

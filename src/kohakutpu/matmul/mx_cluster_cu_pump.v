// A matmul cluster given ONE clock -- clk2x -- that divides it locally.

// L1 and the 4-TCU cascade run at clk2x, everything else at clk1x. BUFGCE_DIV
// is skew-matched to its source: the crossing is an ENABLE, not a FIFO.

// CLR IS NOT TIED OFF: dividers agree on a phase only if they leave reset
// together, and the router driving this CU's port must agree with them.

`default_nettype none

module mx_cluster_cu_pump #(
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
    // Synchronous to clk2x and common to every divider in the mesh.
    input  wire                   div_clr,
    input  wire                   resetn,

    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,

    // The derived clock, so the router feeding this CU's port can be driven
    // from the same net rather than from a second divider that may not agree.
    output wire                   clk1x,

    output wire [15:0]            fills_done,
    output wire [15:0]            gemms_done,
    output wire [15:0]            drains_done
);
`ifdef SYNTHESIS
    BUFGCE_DIV #(
        .BUFGCE_DIVIDE(2),
        .IS_CE_INVERTED(1'b0), .IS_CLR_INVERTED(1'b0), .IS_I_INVERTED(1'b0)
    ) u_div (
        .I(clk2x), .CE(1'b1), .CLR(div_clr), .O(clk1x)
    );
`else
    // The primitive's own behaviour: O rises WITH an I edge, never a delta
    // after it, or every clk1x register samples what clk2x has already updated.
    reg  ph;
    initial ph = 1'b0;
    always @(negedge clk2x) ph <= div_clr ? 1'b0 : ~ph;
    assign clk1x = clk2x & ph;
`endif

    mx_cluster_cu #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .CU_X(CU_X), .CU_Y(CU_Y), .MEM_X(MEM_X), .MEM_Y(MEM_Y),
        .TILES(TILES), .GA(GA), .GB(GB), .ACC_MW(ACC_MW),
        .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH),
        .MODEL(MODEL), .L1_PRIM(L1_PRIM), .TILE_PRIM(TILE_PRIM),
        .RECV_MEM(RECV_MEM), .PUMP(1)
    ) u_cu (
        // UNIT_CDC 0, so `unit_clk` aliases `clk` inside and reaches nothing.
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

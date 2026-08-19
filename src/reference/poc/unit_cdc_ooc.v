// MEASUREMENT TOPS ONLY. Never instantiated in a mesh.

// SHIP PARAMETERS ARE WRITTEN HERE, not passed as -generic: a string generic
// that fails to apply is silent and measures a configuration nobody asked for.

// UNIT_CDC is the ONLY knob, so the two arms are the same file. At 0 `unit_clk`
// reaches nothing and the netlist is the one that ships.

// Both clocks are OOC PORTS, so both are unbuffered and the pair is symmetric.
// Run with OOC_ASYNC=1 or the gray pointers are timed synchronously.

`default_nettype none

module mx_cluster_cu_ooc #(
    parameter integer UNIT_CDC  = 0,
    parameter integer CDC_DEPTH = 16
)(
    input  wire         clk,
    input  wire         unit_clk,
    input  wire         resetn,

    input  wire [287:0] noc_in_data,
    input  wire         noc_in_valid,
    output wire         noc_in_busy,
    output wire [287:0] noc_out_data,
    output wire         noc_out_valid,
    input  wire         noc_out_busy,

    output wire [15:0]  fills_done,
    output wire [15:0]  gemms_done,
    output wire [15:0]  drains_done
);
    mx_cluster_cu #(
        .FLIT_WIDTH(288), .POS_WIDTH(4), .CU_X(1), .CU_Y(1),
        .MEM_X(1), .MEM_Y(1),
        .TILES(4096), .GA(512), .GB(512),
        .INST_DEPTH(512), .RECV_DEPTH(512), .RECV_MEM("block"),
        .MODEL(0), .L1_PRIM("block"), .TILE_PRIM("ultra"),
        .UNIT_CDC(UNIT_CDC), .CDC_DEPTH(CDC_DEPTH)
    ) u_cu (
        .clk(clk), .clk2x(1'b0), .unit_clk(unit_clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid),
        .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(noc_out_busy),
        .fills_done(fills_done), .gemms_done(gemms_done),
        .drains_done(drains_done)
    );
endmodule

module vec_cu_ooc #(
    parameter integer UNIT_CDC  = 0,
    parameter integer CDC_DEPTH = 16
)(
    input  wire         clk,
    input  wire         unit_clk,
    input  wire         resetn,

    input  wire [287:0] noc_in_data,
    input  wire         noc_in_valid,
    output wire         noc_in_busy,
    output wire [287:0] noc_out_data,
    output wire         noc_out_valid,
    input  wire         noc_out_busy,

    output wire [31:0]  dbg_cycles,
    output wire         dbg_fault
);
    vec_cu #(
        .FLIT_WIDTH(288), .POS_WIDTH(4), .POS_X(1), .POS_Y(0),
        .MEM_X(1), .MEM_Y(1),
        .INST_DEPTH(512), .RECV_DEPTH(512), .RECV_MEM("block"),
        .MODEL(0), .L1_DEPTH(512), .L1_PRIM("block"),
        .UNIT_CDC(UNIT_CDC), .CDC_DEPTH(CDC_DEPTH)
    ) u_cu (
        .clk(clk), .unit_clk(unit_clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid),
        .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(noc_out_busy),
        .dbg_cycles(dbg_cycles), .dbg_fault(dbg_fault)
    );
endmodule

`default_nettype wire

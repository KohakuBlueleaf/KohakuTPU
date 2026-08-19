// Two routers wired east-west, for timing measurement only. Never instantiated
// in a real design.
//
// A single router synthesised out-of-context does not measure the link between
// routers, and out-of-context defaults assume every input arrives at t=0. For the
// DATA direction that is harmless: a flit leaves through OutPortSwitch's port_out
// register and arrives directly at the next router's FIFO write port, so the link
// is genuinely register-to-register with nothing combinational between.
//
// The BACKPRESSURE direction is not symmetric. busy originates at the downstream
// FIFO's full flag and feeds combinationally into the upstream arbiter -- through
// sel, grants, and back into the input port's load/rd_en. That path crosses the
// link, so a single-router measurement cannot see it and the t=0 assumption makes
// it look free. This wrapper exists to measure it for real.
//
// Every port that is not part of the A<->B link is brought out to the top level.
// Tying them off would let synthesis prune most of both routers and report a
// flattering number for a router that no longer exists.

module noc_pair_synth #(
    parameter DATA_WIDTH  = 288,
    parameter FIFO_DEPTH  = 32,
    parameter MEMORY_TYPE = "distributed",
    parameter POS_WIDTH   = 4,
    parameter GRID_LO     = 1,
    parameter GRID_HI     = 14
)(
    input clk,
    input rst,

    // ---- router A (1,1): north, south, west, local ----
    input  wire [DATA_WIDTH-1:0] a_north_in_data,  input wire a_north_in_valid,  output wire a_north_in_busy,
    output wire [DATA_WIDTH-1:0] a_north_out_data, output wire a_north_out_valid, input wire a_north_out_busy,
    input  wire [DATA_WIDTH-1:0] a_south_in_data,  input wire a_south_in_valid,  output wire a_south_in_busy,
    output wire [DATA_WIDTH-1:0] a_south_out_data, output wire a_south_out_valid, input wire a_south_out_busy,
    input  wire [DATA_WIDTH-1:0] a_west_in_data,   input wire a_west_in_valid,   output wire a_west_in_busy,
    output wire [DATA_WIDTH-1:0] a_west_out_data,  output wire a_west_out_valid,  input wire a_west_out_busy,
    input  wire [DATA_WIDTH-1:0] a_local_in_data,  input wire a_local_in_valid,  output wire a_local_in_busy,
    output wire [DATA_WIDTH-1:0] a_local_out_data, output wire a_local_out_valid, input wire a_local_out_busy,

    // ---- router B (2,1): north, south, east, local ----
    input  wire [DATA_WIDTH-1:0] b_north_in_data,  input wire b_north_in_valid,  output wire b_north_in_busy,
    output wire [DATA_WIDTH-1:0] b_north_out_data, output wire b_north_out_valid, input wire b_north_out_busy,
    input  wire [DATA_WIDTH-1:0] b_south_in_data,  input wire b_south_in_valid,  output wire b_south_in_busy,
    output wire [DATA_WIDTH-1:0] b_south_out_data, output wire b_south_out_valid, input wire b_south_out_busy,
    input  wire [DATA_WIDTH-1:0] b_east_in_data,   input wire b_east_in_valid,   output wire b_east_in_busy,
    output wire [DATA_WIDTH-1:0] b_east_out_data,  output wire b_east_out_valid,  input wire b_east_out_busy,
    input  wire [DATA_WIDTH-1:0] b_local_in_data,  input wire b_local_in_valid,  output wire b_local_in_busy,
    output wire [DATA_WIDTH-1:0] b_local_out_data, output wire b_local_out_valid, input wire b_local_out_busy
);
    // the link under measurement: A east <-> B west
    wire [DATA_WIDTH-1:0] a2b_data, b2a_data;
    wire                  a2b_valid, b2a_valid;
    wire                  a2b_busy,  b2a_busy;

    NoCRouter #(
        .DATA_WIDTH(DATA_WIDTH), .FIFO_DEPTH(FIFO_DEPTH), .MEMORY_TYPE(MEMORY_TYPE),
        .POS_WIDTH(POS_WIDTH), .POS_X(1), .POS_Y(1),
        .GRID_LO(GRID_LO), .GRID_HI(GRID_HI)
    ) router_a (
        .clk(clk), .rst(rst),
        .north_in_data(a_north_in_data), .north_in_valid(a_north_in_valid), .north_in_busy(a_north_in_busy),
        .north_out_data(a_north_out_data), .north_out_valid(a_north_out_valid), .north_out_busy(a_north_out_busy),
        .east_in_data(b2a_data), .east_in_valid(b2a_valid), .east_in_busy(b2a_busy),
        .east_out_data(a2b_data), .east_out_valid(a2b_valid), .east_out_busy(a2b_busy),
        .south_in_data(a_south_in_data), .south_in_valid(a_south_in_valid), .south_in_busy(a_south_in_busy),
        .south_out_data(a_south_out_data), .south_out_valid(a_south_out_valid), .south_out_busy(a_south_out_busy),
        .west_in_data(a_west_in_data), .west_in_valid(a_west_in_valid), .west_in_busy(a_west_in_busy),
        .west_out_data(a_west_out_data), .west_out_valid(a_west_out_valid), .west_out_busy(a_west_out_busy),
        .local_in_data(a_local_in_data), .local_in_valid(a_local_in_valid), .local_in_busy(a_local_in_busy),
        .local_out_data(a_local_out_data), .local_out_valid(a_local_out_valid), .local_out_busy(a_local_out_busy)
    );

    NoCRouter #(
        .DATA_WIDTH(DATA_WIDTH), .FIFO_DEPTH(FIFO_DEPTH), .MEMORY_TYPE(MEMORY_TYPE),
        .POS_WIDTH(POS_WIDTH), .POS_X(2), .POS_Y(1),
        .GRID_LO(GRID_LO), .GRID_HI(GRID_HI)
    ) router_b (
        .clk(clk), .rst(rst),
        .north_in_data(b_north_in_data), .north_in_valid(b_north_in_valid), .north_in_busy(b_north_in_busy),
        .north_out_data(b_north_out_data), .north_out_valid(b_north_out_valid), .north_out_busy(b_north_out_busy),
        .east_in_data(b_east_in_data), .east_in_valid(b_east_in_valid), .east_in_busy(b_east_in_busy),
        .east_out_data(b_east_out_data), .east_out_valid(b_east_out_valid), .east_out_busy(b_east_out_busy),
        .south_in_data(b_south_in_data), .south_in_valid(b_south_in_valid), .south_in_busy(b_south_in_busy),
        .south_out_data(b_south_out_data), .south_out_valid(b_south_out_valid), .south_out_busy(b_south_out_busy),
        .west_in_data(a2b_data), .west_in_valid(a2b_valid), .west_in_busy(a2b_busy),
        .west_out_data(b2a_data), .west_out_valid(b2a_valid), .west_out_busy(b2a_busy),
        .local_in_data(b_local_in_data), .local_in_valid(b_local_in_valid), .local_in_busy(b_local_in_busy),
        .local_out_data(b_local_out_data), .local_out_valid(b_local_out_valid), .local_out_busy(b_local_out_busy)
    );

endmodule

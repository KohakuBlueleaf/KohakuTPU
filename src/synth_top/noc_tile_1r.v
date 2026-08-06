// One router carrying all five endpoints it can hold. Measurement only.
//
// Exists to make the router/endpoint split solvable rather than assumed. The 2x2
// tile has a router:endpoint ratio of 4:12; this one is 1:5. Two tiles, two
// ratios, two equations -- so R and C fall out of measurement instead of being
// estimated from a standalone synthesis run, where the optimisation context is
// different and the numbers do not transfer.

`default_nettype none

module noc_tile_1r #(
    parameter DATA_WIDTH  = 288,
    parameter FIFO_DEPTH  = 32,
    parameter MEMORY_TYPE = "distributed",
    parameter POS_WIDTH   = 4,
    parameter INST_DEPTH  = 32,
    parameter CU_MEM_TYPE = "distributed",
    parameter GRID_LO     = 1,
    parameter GRID_HI     = 14
)(
    input  wire        clk,
    input  wire        rst,
    input  wire [4:0]  ext_kick,
    input  wire [7:0]  ext_dst,
    input  wire [31:0] ext_data,
    output wire [31:0] sig_out,
    output wire [15:0] space_out,
    output wire [4:0]  busy
);
    localparam N = 5;

    wire [N*DATA_WIDTH-1:0] r2c_data, c2r_data;
    wire [N-1:0]            r2c_valid, c2r_valid, r2c_busy, c2r_busy;

    // index 0 local, 1 north, 2 east, 3 south, 4 west
    NoCRouter #(.DATA_WIDTH(DATA_WIDTH), .FIFO_DEPTH(FIFO_DEPTH),
                .MEMORY_TYPE(MEMORY_TYPE), .POS_WIDTH(POS_WIDTH),
                .POS_X(1), .POS_Y(1), .GRID_LO(GRID_LO), .GRID_HI(GRID_HI)) r (
        .clk(clk), .rst(rst),
        .local_in_data (c2r_data[0*DATA_WIDTH +: DATA_WIDTH]), .local_in_valid (c2r_valid[0]), .local_in_busy (c2r_busy[0]),
        .local_out_data(r2c_data[0*DATA_WIDTH +: DATA_WIDTH]), .local_out_valid(r2c_valid[0]), .local_out_busy(r2c_busy[0]),
        .north_in_data (c2r_data[1*DATA_WIDTH +: DATA_WIDTH]), .north_in_valid (c2r_valid[1]), .north_in_busy (c2r_busy[1]),
        .north_out_data(r2c_data[1*DATA_WIDTH +: DATA_WIDTH]), .north_out_valid(r2c_valid[1]), .north_out_busy(r2c_busy[1]),
        .east_in_data  (c2r_data[2*DATA_WIDTH +: DATA_WIDTH]), .east_in_valid  (c2r_valid[2]), .east_in_busy  (c2r_busy[2]),
        .east_out_data (r2c_data[2*DATA_WIDTH +: DATA_WIDTH]), .east_out_valid (r2c_valid[2]), .east_out_busy (r2c_busy[2]),
        .south_in_data (c2r_data[3*DATA_WIDTH +: DATA_WIDTH]), .south_in_valid (c2r_valid[3]), .south_in_busy (c2r_busy[3]),
        .south_out_data(r2c_data[3*DATA_WIDTH +: DATA_WIDTH]), .south_out_valid(r2c_valid[3]), .south_out_busy(r2c_busy[3]),
        .west_in_data  (c2r_data[4*DATA_WIDTH +: DATA_WIDTH]), .west_in_valid  (c2r_valid[4]), .west_in_busy  (c2r_busy[4]),
        .west_out_data (r2c_data[4*DATA_WIDTH +: DATA_WIDTH]), .west_out_valid (r2c_valid[4]), .west_out_busy (r2c_busy[4])
    );

    //                        4     3     2     1     0
    localparam [N*8-1:0] PX = {8'd0, 8'd1, 8'd2, 8'd1, 8'd1};
    localparam [N*8-1:0] PY = {8'd1, 8'd2, 8'd1, 8'd0, 8'd1};

    wire [31:0] cu_sig   [0:N-1];
    wire [15:0] cu_space [0:N-1];

    genvar i;
    generate
        for (i = 0; i < N; i = i + 1) begin : g_cu
            noc_cu_null #(
                .FLIT_WIDTH(DATA_WIDTH), .POS_WIDTH(POS_WIDTH),
                .POS_X(PX[i*8 +: 8]), .POS_Y(PY[i*8 +: 8]),
                .CU_TYPE(16'h0000 + i[15:0]),
                .INST_DEPTH(INST_DEPTH), .MEM_TYPE(CU_MEM_TYPE)
            ) cu (
                .clk(clk), .resetn(!rst),
                .noc_in_data (r2c_data[i*DATA_WIDTH +: DATA_WIDTH]),
                .noc_in_valid(r2c_valid[i]), .noc_in_busy(r2c_busy[i]),
                .noc_out_data (c2r_data[i*DATA_WIDTH +: DATA_WIDTH]),
                .noc_out_valid(c2r_valid[i]), .noc_out_busy(c2r_busy[i]),
                .ext_kick(ext_kick[i]), .ext_dst(ext_dst[2*POS_WIDTH-1:0]), .ext_data(ext_data),
                .sig_out(cu_sig[i]), .inst_space(cu_space[i]), .busy(busy[i])
            );
        end
    endgenerate

    assign sig_out   = cu_sig[0]   ^ cu_sig[1]   ^ cu_sig[2]   ^ cu_sig[3]   ^ cu_sig[4];
    assign space_out = cu_space[0] ^ cu_space[1] ^ cu_space[2] ^ cu_space[3] ^ cu_space[4];

endmodule

`default_nettype wire

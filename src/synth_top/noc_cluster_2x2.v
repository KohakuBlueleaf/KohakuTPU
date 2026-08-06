// A complete 2x2 NoC tile: 4 routers and all 12 endpoints they can carry, every
// endpoint a zero-compute noc_cu_null. For resource measurement only.
//
// 2x2 is the smallest mesh with no dangling ports, which is what makes it a clean
// unit to extrapolate from. Each router spends two directional ports on neighbours
// and two on border PEs, plus its local port:
//
//        (1,0)     (2,0)            . = border PE, R = router
//     .----R(1,1)---R(2,1)----.     each R also carries one local PE
//   (0,1)  |         |      (3,1)
//     .----R(1,2)---R(2,2)----.
//   (0,2)  |         |      (3,2)
//        (1,3)     (2,3)
//
// 4 routers + 4 local + 8 border = 12 endpoints, which is n*m + 2(n+m) at n=m=2.
// The same relation gives 252 at 14x14, so this tile scales by inspection.
//
// Every port is connected and every endpoint drives an output, so nothing here is
// eligible for pruning -- see noc_cu_null for why that matters.

`default_nettype none

module noc_cluster_2x2 #(
    parameter DATA_WIDTH  = 288,
    parameter FIFO_DEPTH  = 32,
    parameter MEMORY_TYPE = "distributed",
    parameter POS_WIDTH   = 4,
    parameter INST_DEPTH  = 32,
    parameter CU_MEM_TYPE = "distributed",
    // Grid bounds are what the routers compare against, so they set the width of
    // the clamp comparators. Leaving them at 1..2 measures a router that only ever
    // sees a 2x2 world and is correspondingly cheaper than one deployed in a 14x14
    // machine. Override to 1..14 to measure a tile of the real thing.
    parameter GRID_LO     = 1,
    parameter GRID_HI     = 2
)(
    input  wire        clk,
    input  wire        rst,

    // stimulus into every endpoint, so no node is provably idle
    input  wire [11:0] ext_kick,
    input  wire [7:0]  ext_dst,
    input  wire [31:0] ext_data,

    // Observable state from every endpoint. inst_space is included deliberately:
    // it is a mandatory part of the CU contract (spec s6.1) and leaving it
    // unconnected prunes the occupancy counter, understating the endpoint cost.
    output wire [31:0] sig_out,
    output wire [15:0] space_out,
    output wire [11:0] busy
);
    localparam LO = GRID_LO, HI = GRID_HI;
    localparam N  = 12;

    // router <-> endpoint links, one slot per endpoint
    wire [N*DATA_WIDTH-1:0] r2c_data, c2r_data;
    wire [N-1:0]            r2c_valid, c2r_valid, r2c_busy, c2r_busy;

    // inter-router links
    wire [DATA_WIDTH-1:0] e11_21, e21_11, e12_22, e22_12;
    wire [DATA_WIDTH-1:0] s11_12, s12_11, s21_22, s22_21;
    wire e11_21v, e21_11v, e12_22v, e22_12v, s11_12v, s12_11v, s21_22v, s22_21v;
    wire e11_21b, e21_11b, e12_22b, e22_12b, s11_12b, s12_11b, s21_22b, s22_21b;

    // ---------------------------------------------------------------- routers
    NoCRouter #(.DATA_WIDTH(DATA_WIDTH), .FIFO_DEPTH(FIFO_DEPTH),
                .MEMORY_TYPE(MEMORY_TYPE), .POS_WIDTH(POS_WIDTH),
                .POS_X(1), .POS_Y(1), .GRID_LO(LO), .GRID_HI(HI)) r11 (
        .clk(clk), .rst(rst),
        .north_in_data (c2r_data[4*DATA_WIDTH +: DATA_WIDTH]), .north_in_valid (c2r_valid[4]), .north_in_busy (c2r_busy[4]),
        .north_out_data(r2c_data[4*DATA_WIDTH +: DATA_WIDTH]), .north_out_valid(r2c_valid[4]), .north_out_busy(r2c_busy[4]),
        .west_in_data  (c2r_data[5*DATA_WIDTH +: DATA_WIDTH]), .west_in_valid  (c2r_valid[5]), .west_in_busy  (c2r_busy[5]),
        .west_out_data (r2c_data[5*DATA_WIDTH +: DATA_WIDTH]), .west_out_valid (r2c_valid[5]), .west_out_busy (r2c_busy[5]),
        .east_in_data (e21_11), .east_in_valid (e21_11v), .east_in_busy (e21_11b),
        .east_out_data(e11_21), .east_out_valid(e11_21v), .east_out_busy(e11_21b),
        .south_in_data (s12_11), .south_in_valid (s12_11v), .south_in_busy (s12_11b),
        .south_out_data(s11_12), .south_out_valid(s11_12v), .south_out_busy(s11_12b),
        .local_in_data (c2r_data[0*DATA_WIDTH +: DATA_WIDTH]), .local_in_valid (c2r_valid[0]), .local_in_busy (c2r_busy[0]),
        .local_out_data(r2c_data[0*DATA_WIDTH +: DATA_WIDTH]), .local_out_valid(r2c_valid[0]), .local_out_busy(r2c_busy[0])
    );

    NoCRouter #(.DATA_WIDTH(DATA_WIDTH), .FIFO_DEPTH(FIFO_DEPTH),
                .MEMORY_TYPE(MEMORY_TYPE), .POS_WIDTH(POS_WIDTH),
                .POS_X(2), .POS_Y(1), .GRID_LO(LO), .GRID_HI(HI)) r21 (
        .clk(clk), .rst(rst),
        .north_in_data (c2r_data[6*DATA_WIDTH +: DATA_WIDTH]), .north_in_valid (c2r_valid[6]), .north_in_busy (c2r_busy[6]),
        .north_out_data(r2c_data[6*DATA_WIDTH +: DATA_WIDTH]), .north_out_valid(r2c_valid[6]), .north_out_busy(r2c_busy[6]),
        .east_in_data  (c2r_data[7*DATA_WIDTH +: DATA_WIDTH]), .east_in_valid  (c2r_valid[7]), .east_in_busy  (c2r_busy[7]),
        .east_out_data (r2c_data[7*DATA_WIDTH +: DATA_WIDTH]), .east_out_valid (r2c_valid[7]), .east_out_busy (r2c_busy[7]),
        .west_in_data (e11_21), .west_in_valid (e11_21v), .west_in_busy (e11_21b),
        .west_out_data(e21_11), .west_out_valid(e21_11v), .west_out_busy(e21_11b),
        .south_in_data (s22_21), .south_in_valid (s22_21v), .south_in_busy (s22_21b),
        .south_out_data(s21_22), .south_out_valid(s21_22v), .south_out_busy(s21_22b),
        .local_in_data (c2r_data[1*DATA_WIDTH +: DATA_WIDTH]), .local_in_valid (c2r_valid[1]), .local_in_busy (c2r_busy[1]),
        .local_out_data(r2c_data[1*DATA_WIDTH +: DATA_WIDTH]), .local_out_valid(r2c_valid[1]), .local_out_busy(r2c_busy[1])
    );

    NoCRouter #(.DATA_WIDTH(DATA_WIDTH), .FIFO_DEPTH(FIFO_DEPTH),
                .MEMORY_TYPE(MEMORY_TYPE), .POS_WIDTH(POS_WIDTH),
                .POS_X(1), .POS_Y(2), .GRID_LO(LO), .GRID_HI(HI)) r12 (
        .clk(clk), .rst(rst),
        .south_in_data (c2r_data[8*DATA_WIDTH +: DATA_WIDTH]), .south_in_valid (c2r_valid[8]), .south_in_busy (c2r_busy[8]),
        .south_out_data(r2c_data[8*DATA_WIDTH +: DATA_WIDTH]), .south_out_valid(r2c_valid[8]), .south_out_busy(r2c_busy[8]),
        .west_in_data  (c2r_data[9*DATA_WIDTH +: DATA_WIDTH]), .west_in_valid  (c2r_valid[9]), .west_in_busy  (c2r_busy[9]),
        .west_out_data (r2c_data[9*DATA_WIDTH +: DATA_WIDTH]), .west_out_valid (r2c_valid[9]), .west_out_busy (r2c_busy[9]),
        .north_in_data (s11_12), .north_in_valid (s11_12v), .north_in_busy (s11_12b),
        .north_out_data(s12_11), .north_out_valid(s12_11v), .north_out_busy(s12_11b),
        .east_in_data (e22_12), .east_in_valid (e22_12v), .east_in_busy (e22_12b),
        .east_out_data(e12_22), .east_out_valid(e12_22v), .east_out_busy(e12_22b),
        .local_in_data (c2r_data[2*DATA_WIDTH +: DATA_WIDTH]), .local_in_valid (c2r_valid[2]), .local_in_busy (c2r_busy[2]),
        .local_out_data(r2c_data[2*DATA_WIDTH +: DATA_WIDTH]), .local_out_valid(r2c_valid[2]), .local_out_busy(r2c_busy[2])
    );

    NoCRouter #(.DATA_WIDTH(DATA_WIDTH), .FIFO_DEPTH(FIFO_DEPTH),
                .MEMORY_TYPE(MEMORY_TYPE), .POS_WIDTH(POS_WIDTH),
                .POS_X(2), .POS_Y(2), .GRID_LO(LO), .GRID_HI(HI)) r22 (
        .clk(clk), .rst(rst),
        .south_in_data (c2r_data[10*DATA_WIDTH +: DATA_WIDTH]), .south_in_valid (c2r_valid[10]), .south_in_busy (c2r_busy[10]),
        .south_out_data(r2c_data[10*DATA_WIDTH +: DATA_WIDTH]), .south_out_valid(r2c_valid[10]), .south_out_busy(r2c_busy[10]),
        .east_in_data  (c2r_data[11*DATA_WIDTH +: DATA_WIDTH]), .east_in_valid  (c2r_valid[11]), .east_in_busy  (c2r_busy[11]),
        .east_out_data (r2c_data[11*DATA_WIDTH +: DATA_WIDTH]), .east_out_valid (r2c_valid[11]), .east_out_busy (r2c_busy[11]),
        .north_in_data (s21_22), .north_in_valid (s21_22v), .north_in_busy (s21_22b),
        .north_out_data(s22_21), .north_out_valid(s22_21v), .north_out_busy(s22_21b),
        .west_in_data (e12_22), .west_in_valid (e12_22v), .west_in_busy (e12_22b),
        .west_out_data(e22_12), .west_out_valid(e22_12v), .west_out_busy(e22_12b),
        .local_in_data (c2r_data[3*DATA_WIDTH +: DATA_WIDTH]), .local_in_valid (c2r_valid[3]), .local_in_busy (c2r_busy[3]),
        .local_out_data(r2c_data[3*DATA_WIDTH +: DATA_WIDTH]), .local_out_valid(r2c_valid[3]), .local_out_busy(r2c_busy[3])
    );

    // -------------------------------------------------------------- endpoints
    // Coordinate lists, written MSB-first so that index i of the part-select is
    // endpoint i: 0-3 are the four local PEs, 4-11 the eight border PEs.
    //                        11 10  9  8  7  6  5  4  3  2  1  0
    localparam [N*8-1:0] PX = {8'd3,8'd2,8'd0,8'd1,8'd3,8'd2,8'd0,8'd1,8'd2,8'd1,8'd2,8'd1};
    localparam [N*8-1:0] PY = {8'd2,8'd3,8'd2,8'd3,8'd1,8'd0,8'd1,8'd0,8'd2,8'd2,8'd1,8'd1};

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

    assign sig_out = cu_sig[0]  ^ cu_sig[1]  ^ cu_sig[2]  ^ cu_sig[3]
                   ^ cu_sig[4]  ^ cu_sig[5]  ^ cu_sig[6]  ^ cu_sig[7]
                   ^ cu_sig[8]  ^ cu_sig[9]  ^ cu_sig[10] ^ cu_sig[11];

    assign space_out = cu_space[0]  ^ cu_space[1]  ^ cu_space[2]  ^ cu_space[3]
                     ^ cu_space[4]  ^ cu_space[5]  ^ cu_space[6]  ^ cu_space[7]
                     ^ cu_space[8]  ^ cu_space[9]  ^ cu_space[10] ^ cu_space[11];

endmodule

`default_nettype wire

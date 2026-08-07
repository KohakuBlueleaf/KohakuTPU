// 5-port mesh router. See docs/noc/spec.md.
//
// FIFO_DEPTH only has to cover the backpressure round-trip, so 32 in distributed
// RAM rather than a URAM-backed 4096: depth does not prevent deadlock (XY routing
// does), and an SRL32 costs ~1 LUT/bit at any depth up to 32. Measured on
// xcvu13p: 15 URAM -> 0, for +400 LUT per router.
module NoCRouter #(
    parameter DATA_WIDTH  = 288,
    parameter FIFO_DEPTH  = 32,
    parameter MEMORY_TYPE = "distributed",
    parameter POS_WIDTH   = 4,
    parameter POS_X       = 1,
    parameter POS_Y       = 1,
    parameter GRID_LO     = 1,
    parameter GRID_HI     = 14
)(
    input clk,
    input rst,

    // North Port
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP north_in" *)
    input wire [DATA_WIDTH-1:0] north_in_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP north_in" *)
    input wire north_in_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP north_in" *)
    output wire north_in_busy,

    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP north_out" *)
    output wire [DATA_WIDTH-1:0] north_out_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP north_out" *)
    output wire north_out_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP north_out" *)
    input wire north_out_busy,

    // East Port
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP east_in" *)
    input wire [DATA_WIDTH-1:0] east_in_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP east_in" *)
    input wire east_in_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP east_in" *)
    output wire east_in_busy,

    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP east_out" *)
    output wire [DATA_WIDTH-1:0] east_out_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP east_out" *)
    output wire east_out_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP east_out" *)
    input wire east_out_busy,

    // South Port
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP south_in" *)
    input wire [DATA_WIDTH-1:0] south_in_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP south_in" *)
    input wire south_in_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP south_in" *)
    output wire south_in_busy,

    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP south_out" *)
    output wire [DATA_WIDTH-1:0] south_out_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP south_out" *)
    output wire south_out_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP south_out" *)
    input wire south_out_busy,

    // West Port
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP west_in" *)
    input wire [DATA_WIDTH-1:0] west_in_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP west_in" *)
    input wire west_in_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP west_in" *)
    output wire west_in_busy,

    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP west_out" *)
    output wire [DATA_WIDTH-1:0] west_out_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP west_out" *)
    output wire west_out_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP west_out" *)
    input wire west_out_busy,

    // Local Port
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP local_in" *)
    input wire [DATA_WIDTH-1:0] local_in_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP local_in" *)
    input wire local_in_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP local_in" *)
    output wire local_in_busy,

    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP local_out" *)
    output wire [DATA_WIDTH-1:0] local_out_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP local_out" *)
    output wire local_out_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:KohakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP local_out" *)
    input wire local_out_busy
);
    // One head bus per INPUT port, not one per (input, output) pair. An input
    // port now holds ONE flit rather than one per direction (noc_inport.v), so
    // 25 DATA_WIDTH buses collapse to 5 -- that is where the flip-flop saving
    // comes from.
    wire [DATA_WIDTH-1:0] n_head, e_head, s_head, w_head, l_head;

    // Requests, indexed by DESTINATION direction (0:N 1:E 2:S 3:W 4:L), matching
    // port_choice in InPortSwitch.
    wire [4:0] n_req, e_req, s_req, w_req, l_req;

    // Grants, indexed by SOURCE input port (0:N 1:E 2:S 3:W 4:L), matching
    // in_heads / in_reqs in OutPortSwitch.
    wire [4:0] gn, ge, gs, gw, gl;

    // Transpose. An input's grant vector is indexed by destination and an output's
    // by source, so each input collects one bit from each of the five outputs.
    // Bit order here is {local, west, south, east, north}, the same order
    // port_choice is built in -- the two must agree or every horizontal hop goes
    // the wrong way, which is exactly the bug the mesh bench caught before.
    wire [4:0] n_grant = {gl[0], gw[0], gs[0], ge[0], gn[0]};
    wire [4:0] e_grant = {gl[1], gw[1], gs[1], ge[1], gn[1]};
    wire [4:0] s_grant = {gl[2], gw[2], gs[2], ge[2], gn[2]};
    wire [4:0] w_grant = {gl[3], gw[3], gs[3], ge[3], gn[3]};
    wire [4:0] l_grant = {gl[4], gw[4], gs[4], ge[4], gn[4]};

    InPortSwitch #(
        .DATA_WIDTH(DATA_WIDTH),
        .FIFO_DEPTH(FIFO_DEPTH),
        .MEMORY_TYPE(MEMORY_TYPE),
        .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X),
        .POS_Y(POS_Y),
        .GRID_LO(GRID_LO),
        .GRID_HI(GRID_HI)
    ) north_in_switch (
        .clk(clk), .rst(rst),
        .data_in(north_in_data), .data_valid(north_in_valid), .port_busy(north_in_busy),
        .head_data(n_head), .head_req(n_req), .grant(n_grant)
    ),
    east_in_switch (
        .clk(clk), .rst(rst),
        .data_in(east_in_data), .data_valid(east_in_valid), .port_busy(east_in_busy),
        .head_data(e_head), .head_req(e_req), .grant(e_grant)
    ),
    south_in_switch (
        .clk(clk), .rst(rst),
        .data_in(south_in_data), .data_valid(south_in_valid), .port_busy(south_in_busy),
        .head_data(s_head), .head_req(s_req), .grant(s_grant)
    ),
    west_in_switch (
        .clk(clk), .rst(rst),
        .data_in(west_in_data), .data_valid(west_in_valid), .port_busy(west_in_busy),
        .head_data(w_head), .head_req(w_req), .grant(w_grant)
    ),
    local_in_switch (
        .clk(clk), .rst(rst),
        .data_in(local_in_data), .data_valid(local_in_valid), .port_busy(local_in_busy),
        .head_data(l_head), .head_req(l_req), .grant(l_grant)
    );

    OutPortSwitch #(
        .DATA_WIDTH(DATA_WIDTH)
    ) north_out_switch (
        .clk(clk), .rst(rst),
        .in_heads({l_head, w_head, s_head, e_head, n_head}),
        .in_reqs ({l_req[0], w_req[0], s_req[0], e_req[0], n_req[0]}),
        .grants(gn),
        .port_out(north_out_data), .out_valid(north_out_valid), .busy(north_out_busy)
    ),
    east_out_switch (
        .clk(clk), .rst(rst),
        .in_heads({l_head, w_head, s_head, e_head, n_head}),
        .in_reqs ({l_req[1], w_req[1], s_req[1], e_req[1], n_req[1]}),
        .grants(ge),
        .port_out(east_out_data), .out_valid(east_out_valid), .busy(east_out_busy)
    ),
    south_out_switch (
        .clk(clk), .rst(rst),
        .in_heads({l_head, w_head, s_head, e_head, n_head}),
        .in_reqs ({l_req[2], w_req[2], s_req[2], e_req[2], n_req[2]}),
        .grants(gs),
        .port_out(south_out_data), .out_valid(south_out_valid), .busy(south_out_busy)
    ),
    west_out_switch (
        .clk(clk), .rst(rst),
        .in_heads({l_head, w_head, s_head, e_head, n_head}),
        .in_reqs ({l_req[3], w_req[3], s_req[3], e_req[3], n_req[3]}),
        .grants(gw),
        .port_out(west_out_data), .out_valid(west_out_valid), .busy(west_out_busy)
    ),
    local_out_switch (
        .clk(clk), .rst(rst),
        .in_heads({l_head, w_head, s_head, e_head, n_head}),
        .in_reqs ({l_req[4], w_req[4], s_req[4], e_req[4], n_req[4]}),
        .grants(gl),
        .port_out(local_out_data), .out_valid(local_out_valid), .busy(local_out_busy)
    );
endmodule

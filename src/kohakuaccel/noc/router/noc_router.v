// 5-port mesh router. See docs/noc/spec.md.
//
// FIFO_DEPTH only has to cover the backpressure round-trip: depth does not
// prevent deadlock (XY routing does). The generated ship overrides the two
// storage defaults per mesh (ROUTER_DEPTH / ROUTER_MEM); measured on the 2x2
// grid at 3.333 ns: 32/"distributed" 3,762 LUT / 0 tiles / 426 MHz,
// 512/"block" 3,052 LUT / 20 tiles / 386 MHz.
module NoCRouter #(
    parameter DATA_WIDTH  = 288,
    parameter FIFO_DEPTH  = 32,
    parameter MEMORY_TYPE = "distributed",
    parameter POS_WIDTH   = 4,
    parameter POS_X       = 1,
    parameter POS_Y       = 1,
    parameter GRID_LO     = 1,
    parameter GRID_HI     = 14,
    // Per-axis clamp bounds, so the mesh need not be square. Both default to
    // GRID_HI, which leaves every square instantiation unchanged.
    parameter GRID_X_HI   = GRID_HI,
    parameter GRID_Y_HI   = GRID_HI
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

    // ================= turns XY routing can never ask for ==================
    // Whether each neighbour is a ROUTER. Derived from the clamp bounds rather
    // than passed in: a second parameter could disagree with the first.
    localparam N_RTR = (POS_Y > GRID_LO);
    localparam S_RTR = (POS_Y < GRID_Y_HI);
    localparam W_RTR = (POS_X > GRID_LO);
    localparam E_RTR = (POS_X < GRID_X_HI);

    // A router neighbour has already routed what it hands over, so W-in carries
    // r_pos_x >= MX, E-in r_pos_x <= MX, and N/S-in r_pos_x == MX with r_pos_y
    // on their own side of MY. An EDGE PE constrains nothing -- it injects.
    //
    // The Y-to-X turns die only AWAY from the boundary column: `at_router &&
    // pos_x < MX` is the OUTWARD HOP to an edge PE and it fires whichever port
    // the flit arrived on, so a vec core at (x,0) reaching MAG at (0,y) really
    // is north-in to west-out. Bit order {L,W,S,E,N}, as port_choice.
    localparam [4:0] N_KILL = {1'b0, (N_RTR && W_RTR), 1'b0, (N_RTR && E_RTR), N_RTR};
    localparam [4:0] S_KILL = {1'b0, (S_RTR && W_RTR), S_RTR, (S_RTR && E_RTR), 1'b0};
    localparam [4:0] W_KILL = {1'b0, W_RTR, 1'b0, 1'b0, 1'b0};
    localparam [4:0] E_KILL = {1'b0, 1'b0, 1'b0, E_RTR, 1'b0};
    // A U-turn on an EDGE-facing side, and local->local, stay reachable: the way
    // in is a PE addressing its own coordinate. Killing them too was measured at
    // ~380 LUT across the mesh, which does not buy a driver-side invariant every
    // future author would have to know.

    wire [4:0] n_req_m = n_req & ~N_KILL;
    wire [4:0] e_req_m = e_req & ~E_KILL;
    wire [4:0] s_req_m = s_req & ~S_KILL;
    wire [4:0] w_req_m = w_req & ~W_KILL;

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
        .GRID_HI(GRID_HI),
        .GRID_X_HI(GRID_X_HI),
        .GRID_Y_HI(GRID_Y_HI)
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
        .in_reqs ({l_req[0], w_req_m[0], s_req_m[0], e_req_m[0], n_req_m[0]}),
        .grants(gn),
        .port_out(north_out_data), .out_valid(north_out_valid), .busy(north_out_busy)
    ),
    east_out_switch (
        .clk(clk), .rst(rst),
        .in_heads({l_head, w_head, s_head, e_head, n_head}),
        .in_reqs ({l_req[1], w_req_m[1], s_req_m[1], e_req_m[1], n_req_m[1]}),
        .grants(ge),
        .port_out(east_out_data), .out_valid(east_out_valid), .busy(east_out_busy)
    ),
    south_out_switch (
        .clk(clk), .rst(rst),
        .in_heads({l_head, w_head, s_head, e_head, n_head}),
        .in_reqs ({l_req[2], w_req_m[2], s_req_m[2], e_req_m[2], n_req_m[2]}),
        .grants(gs),
        .port_out(south_out_data), .out_valid(south_out_valid), .busy(south_out_busy)
    ),
    west_out_switch (
        .clk(clk), .rst(rst),
        .in_heads({l_head, w_head, s_head, e_head, n_head}),
        .in_reqs ({l_req[3], w_req_m[3], s_req_m[3], e_req_m[3], n_req_m[3]}),
        .grants(gw),
        .port_out(west_out_data), .out_valid(west_out_valid), .busy(west_out_busy)
    ),
    local_out_switch (
        .clk(clk), .rst(rst),
        .in_heads({l_head, w_head, s_head, e_head, n_head}),
        .in_reqs ({l_req[4], w_req_m[4], s_req_m[4], e_req_m[4], n_req_m[4]}),
        .grants(gl),
        .port_out(local_out_data), .out_valid(local_out_valid), .busy(local_out_busy)
    );

`ifndef SYNTHESIS
    // A mask that is wrong presents as a HANG, not a wrong answer: the request
    // is never granted, the inport's hold slot never clears, and the fabric
    // wedges several modules from the cause. Name it at the router instead.
    // Said ONCE: the request is held forever, so an unguarded report is an
    // unbounded flood on top of the hang.
    reg said = 1'b0;
    always @(posedge clk) begin
        if (
            !rst
            && !said
            && (
                |(n_req & N_KILL)
                || |(e_req & E_KILL)
                || |(s_req & S_KILL)
                || |(w_req & W_KILL)
            )
        ) begin
            said <= 1'b1;
            $display("%0t ERROR NoCRouter(%0d,%0d): a turn XY routing was assumed never to ask for -- n%b/%b e%b/%b s%b/%b w%b/%b as {L,W,S,E,N}. This flit will never be granted.",
                     $time, POS_X, POS_Y, n_req, N_KILL, e_req, E_KILL,
                     s_req, S_KILL, w_req, W_KILL);
        end
    end
`endif
endmodule

// Input port: buffers arriving flits, computes the output direction for the one
// at the head, and offers it to the output ports through a single holding slot.
//
// Routing is XY dimension-order on CLAMPED coordinates -- see docs/noc/spec.md s2.
// Border PEs live outside the router grid, so a packet for (0,y) cannot literally
// finish X; it routes toward the adjacent router (GRID_LO,y) and only takes the
// outward hop on arrival. Pure XY, so the channel dependency graph stays acyclic.
//
// This port used to keep five DATA_WIDTH holding registers (one per direction)
// plus a spill register: 5*288 + 288 = 1728 flip-flops per input, 8640 per router.
// The benefit was only visible when successive flits diverged, because two flits
// bound for the SAME output were already serialised -- the second found its slot
// occupied. One slot replaces all six, at one flit outstanding per input instead
// of five, which is head-of-line blocking on a congested direction in exchange for
// two thirds of the router's flip-flops.
//
// The slot is kept rather than removed entirely. Feeding the FIFO output straight
// into the arbiter also works and saves a further ~1.5k flip-flops per router, but
// it puts FIFO read, route computation, arbitration and a 5:1 DATA_WIDTH mux into
// one combinational path: measured 347 MHz against a 300 MHz target, and 0.46 ns
// of out-of-context slack does not survive real placement. Registering here splits
// that path in two.

module InPortSwitch #(
    parameter DATA_WIDTH  = 288,
    parameter FIFO_DEPTH  = 32,
    parameter MEMORY_TYPE = "distributed",
    parameter POS_WIDTH   = 4,
    parameter POS_X       = 1,
    parameter POS_Y       = 1,
    parameter GRID_LO     = 1,   // lowest router coordinate
    parameter GRID_HI     = 14   // highest router coordinate
)(
    input clk,
    input rst,

    // In Port Signals
    input wire [DATA_WIDTH-1:0] data_in,
    input wire data_valid,
    output wire port_busy,

    // The flit being offered, and which of the five outputs it wants. head_req is
    // one-hot, so at most one output port can ever grant this input in a cycle --
    // which is what makes a single-bit "was I granted" test correct.
    output wire [DATA_WIDTH-1:0] head_data,
    output wire [4:0]            head_req,
    input  wire [4:0]            grant
);
    wire                  rd_busy;
    wire [DATA_WIDTH-1:0] rd_data;

    reg [DATA_WIDTH-1:0] hold;
    reg [4:0]            hold_req;   // one-hot; all zero means the slot is empty

    wire taken = |grant;

    // Load whenever the slot is free, including the cycle it is being emptied --
    // that is what sustains one flit per cycle rather than one every two.
    // First-word-fall-through means rd_data is already the head, so rd_en is the
    // load itself: nothing is popped speculatively, so there is no spill register
    // holding a flit that was read before it could be placed.
    wire load = !rd_busy && (hold_req == 5'b00000 || taken);

    sync_fifo #(
        .DATA_WIDTH(DATA_WIDTH),
        .FIFO_DEPTH(FIFO_DEPTH),
        .MEMORY_TYPE(MEMORY_TYPE)
    ) inport_fifo (
        .clk(clk),
        .rst(rst),
        .wr_en(data_valid),
        .wr_data(data_in),
        .wr_busy(port_busy),
        .rd_en(load),
        .rd_data(rd_data),
        .rd_busy(rd_busy)
    );

    /*
        5 port:
        0: North  --  pos_y < POS_Y
        1: East   --  pos_x > POS_X
        2: South  --  pos_y > POS_Y
        3: West   --  pos_x < POS_X
        4: Local  --  pos_x == POS_X && pos_y == POS_Y
    */
    // Routing is computed on the FIFO output, one cycle before the flit is offered,
    // so it is off the arbitration path entirely.
    wire [POS_WIDTH-1:0] pos_x = rd_data[DATA_WIDTH-1           -: POS_WIDTH];
    wire [POS_WIDTH-1:0] pos_y = rd_data[DATA_WIDTH-POS_WIDTH-1 -: POS_WIDTH];

    localparam [POS_WIDTH-1:0] LO = GRID_LO[POS_WIDTH-1:0];
    localparam [POS_WIDTH-1:0] HI = GRID_HI[POS_WIDTH-1:0];
    localparam [POS_WIDTH-1:0] MX = POS_X[POS_WIDTH-1:0];
    localparam [POS_WIDTH-1:0] MY = POS_Y[POS_WIDTH-1:0];

    // destination clamped into the router grid
    wire [POS_WIDTH-1:0] r_pos_x = (pos_x < LO) ? LO : (pos_x > HI) ? HI : pos_x;
    wire [POS_WIDTH-1:0] r_pos_y = (pos_y < LO) ? LO : (pos_y > HI) ? HI : pos_y;

    wire x_done    = (r_pos_x == MX);
    wire y_done    = (r_pos_y == MY);
    wire at_router = x_done && y_done;

    // Priority chain rather than parallel terms: for a legal destination exactly
    // one is true anyway, but the four invalid corner coordinates would otherwise
    // assert two directions at once and break the one-hot assumption.
    wire want_west  = (!x_done && (r_pos_x < MX)) || (at_router && (pos_x < MX));
    wire want_east  = !want_west &&
                      ((!x_done && (r_pos_x > MX)) || (at_router && (pos_x > MX)));
    wire want_north = !want_west && !want_east &&
                      ((x_done && !y_done && (r_pos_y < MY)) || (at_router && (pos_y < MY)));
    wire want_south = !want_west && !want_east && !want_north &&
                      ((x_done && !y_done && (r_pos_y > MY)) || (at_router && (pos_y > MY)));
    wire want_local = !want_west && !want_east && !want_north && !want_south && at_router;

    wire [4:0] port_choice = {want_local, want_west, want_south, want_east, want_north};

    always @(posedge clk, posedge rst) begin
        if (rst) begin
            hold     <= {DATA_WIDTH{1'b0}};
            hold_req <= 5'b00000;
        end else if (load) begin
            hold     <= rd_data;
            hold_req <= port_choice;
        end else if (taken) begin
            hold_req <= 5'b00000;
        end
    end

    assign head_data = hold;
    assign head_req  = hold_req;

endmodule

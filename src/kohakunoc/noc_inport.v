// Input port: buffers arriving flits, computes the output direction for the one
// at the head, and offers it to the output ports through a single holding slot.
//
// Routing is XY dimension-order on CLAMPED coordinates -- see docs/noc/spec.md s2.
// Border PEs live outside the router grid, so a packet for (0,y) cannot literally
// finish X; it routes toward the adjacent router (GRID_LO,y) and only takes the
// outward hop on arrival. Pure XY, so the channel dependency graph stays acyclic.
//
// ONE holding slot, not one per direction: two flits bound for the same output
// were already serialised, so per-direction slots only helped when successive
// flits diverged. The trade is head-of-line blocking on a congested direction
// against two thirds of the router's flip-flops.
//
// The slot is kept rather than removed. Feeding the FIFO output straight to the
// arbiter saves more flops but puts FIFO read, route computation, arbitration
// and a 5:1 DATA_WIDTH mux in one combinational path, which does not clear
// 300 MHz with enough slack to survive placement.

module InPortSwitch #(
    parameter DATA_WIDTH  = 288,
    parameter FIFO_DEPTH  = 32,
    parameter MEMORY_TYPE = "distributed",
    parameter POS_WIDTH   = 4,
    parameter POS_X       = 1,
    parameter POS_Y       = 1,
    parameter GRID_LO     = 1,   // lowest router coordinate, both axes
    parameter GRID_HI     = 14,  // highest, when the grid is square
    // PER-AXIS so the grid need not be square. Defaulting both to GRID_HI
    // leaves every existing instantiation bit-identical; a rectangular mesh
    // overrides them and a square one never mentions them.
    parameter GRID_X_HI   = GRID_HI,
    parameter GRID_Y_HI   = GRID_HI
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
    wire                  fifo_full, fifo_almost;

    // ACCEPT ON `valid && !busy`, AND THE SENDER RETRIES. Both halves are
    // required; neither works alone:
    //
    //   a sender that gives up LOSES a flit -- committing against busy at T and
    //   presenting at T+1 into a receiver that raised busy at T+1 destroys it.
    //
    //   accepting unconditionally DUPLICATES one, because every sender holds
    //   `valid` until it sees `!busy`, so a write on every cycle the FIFO has
    //   room enqueues the same flit repeatedly.
    //
    // Either way a MEM_WR_DATA goes missing or arrives twice, and the slot it
    // belongs to never completes.
    //
    // `wr_almost` is NOT the margin its name promises: sync_fifo passes
    // USE_ADV_FEATURES(0), so XPM ties prog_full low and wr_almost reduces to
    // wr_busy. What makes plain `full` safe here is the RETRY, not a margin --
    // see docs/noc/spec.md s2.1. Anything needing a real margin must count for
    // itself, as MAG does with Q_MARGIN.
    assign port_busy = fifo_almost;

    reg [DATA_WIDTH-1:0] hold;
    reg [4:0]            hold_req;   // one-hot; all zero means the slot is empty

    wire taken = |grant;

    // Load whenever the slot is free, INCLUDING the cycle it is being emptied:
    // that is what sustains one flit per cycle rather than one every two.
    // First-word-fall-through makes rd_data the head already, so rd_en is the
    // load itself -- nothing is popped speculatively and no spill register is
    // needed.
    wire load = !rd_busy && (hold_req == 5'b00000 || taken);

    sync_fifo #(
        .DATA_WIDTH(DATA_WIDTH),
        .FIFO_DEPTH(FIFO_DEPTH),
        .MEMORY_TYPE(MEMORY_TYPE)
    ) inport_fifo (
        .clk(clk),
        .rst(rst),
        .wr_en(data_valid && !port_busy),
        .wr_data(data_in),
        .wr_busy(fifo_full),
        .wr_almost(fifo_almost),
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

    localparam [POS_WIDTH-1:0] LO  = GRID_LO[POS_WIDTH-1:0];
    localparam [POS_WIDTH-1:0] XHI = GRID_X_HI[POS_WIDTH-1:0];
    localparam [POS_WIDTH-1:0] YHI = GRID_Y_HI[POS_WIDTH-1:0];
    localparam [POS_WIDTH-1:0] MX = POS_X[POS_WIDTH-1:0];
    localparam [POS_WIDTH-1:0] MY = POS_Y[POS_WIDTH-1:0];

    // destination clamped into the router grid
    wire [POS_WIDTH-1:0] r_pos_x = (pos_x < LO) ? LO : (pos_x > XHI) ? XHI : pos_x;
    wire [POS_WIDTH-1:0] r_pos_y = (pos_y < LO) ? LO : (pos_y > YHI) ? YHI : pos_y;

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

`ifndef SYNTHESIS
    // A flit offered while busy must still be offered next cycle. Testing THAT,
    // rather than "was a flit offered into a full buffer", is what separates a
    // sender that retries -- the normal steady state under backpressure -- from
    // one that gave up, whose damage lands modules away as a short write burst
    // or a corrupt L1 entry.
    reg                  offered;
    reg [DATA_WIDTH-1:0] offered_d;
    always @(posedge clk) begin
        if (rst) offered <= 1'b0;
        else begin
            offered   <= data_valid && port_busy;
            offered_d <= data_in;
            if (offered && !(data_valid && (data_in == offered_d)))
                $display("%0t ERROR noc_inport (%0d,%0d): flit LOST -- sender did not hold",
                         $time, POS_X, POS_Y);
        end
    end
`endif

endmodule

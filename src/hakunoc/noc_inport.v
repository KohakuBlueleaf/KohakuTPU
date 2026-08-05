// Input port: buffers arriving flits, computes the output direction, and holds
// one flit per direction until the corresponding output port takes it.
//
// Routing is XY dimension-order on CLAMPED coordinates -- see docs/noc/spec.md s2.
// Border PEs live outside the router grid, so a packet for (0,y) cannot literally
// finish X; it routes toward the adjacent router (GRID_LO,y) and only takes the
// outward hop on arrival. Pure XY, so the channel dependency graph stays acyclic.

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

    // NoC Router Signals, 5ports
    output reg [4:0][DATA_WIDTH-1:0] port_out,
    output reg [4:0] port_valid,
    input wire [4:0] clear
);
    reg next_data;
    wire [DATA_WIDTH-1:0] rd_data, current_data;
    wire rd_busy;
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
        .rd_en(next_data),
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
    reg state, use_cache;
    reg [DATA_WIDTH-1:0] cache;

    assign current_data = use_cache ? cache : rd_data;

    wire [POS_WIDTH-1:0] pos_x = current_data[DATA_WIDTH-1:DATA_WIDTH-POS_WIDTH];
    wire [POS_WIDTH-1:0] pos_y = current_data[DATA_WIDTH-POS_WIDTH-1:DATA_WIDTH-POS_WIDTH*2];

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
    // assert two directions at once and break the one-hot assumption that the
    // case statement below depends on.
    wire want_west  = (!x_done && (r_pos_x < MX)) || (at_router && (pos_x < MX));
    wire want_east  = !want_west &&
                      ((!x_done && (r_pos_x > MX)) || (at_router && (pos_x > MX)));
    wire want_north = !want_west && !want_east &&
                      ((x_done && !y_done && (r_pos_y < MY)) || (at_router && (pos_y < MY)));
    wire want_south = !want_west && !want_east && !want_north &&
                      ((x_done && !y_done && (r_pos_y > MY)) || (at_router && (pos_y > MY)));
    wire want_local = !want_west && !want_east && !want_north && !want_south && at_router;

    wire [4:0] port_choice = {want_local, want_west, want_south, want_east, want_north};

    // & ~clear, never ^ clear: XOR only means "clear these" when clear is a subset
    // of port_valid. clear arrives a cycle after the outport selected it, by which
    // time the slot may already be free -- and XOR would then SET it, emitting a
    // stale flit.
    wire [4:0] real_port_valid = port_valid & ~clear;
    wire [4:0] avail_port      = port_choice & ~real_port_valid;

    always @(posedge clk, posedge rst) begin
        if (rst) begin
            state <= 1'b0;
            next_data <= 1'b0;
            port_valid <= 0;
            use_cache <= 1'b0;
            port_out <= 0;
            cache <= 0;
        end else begin
            if (~rd_busy | use_cache) begin //FIFO have data
                if (avail_port != 5'b00000) begin //target port is free
                    if (state == 1'b1 | use_cache) begin //first data
                        case (avail_port)
                            5'b00001: port_out[0] <= current_data; //north
                            5'b00010: port_out[1] <= current_data; //east
                            5'b00100: port_out[2] <= current_data; //south
                            5'b01000: port_out[3] <= current_data; //west
                            5'b10000: port_out[4] <= current_data; //local
                        endcase
                        port_valid <= real_port_valid | avail_port;
                        if (use_cache) begin
                            use_cache <= 1'b0;
                            next_data <= 1'b0;
                            state <= 1'b0;
                        end else begin
                            next_data <= 1'b1;
                            state <= 1'b1;
                        end
                    end else begin
                        use_cache <= 1'b0;
                        port_valid <= real_port_valid;
                        next_data <= 1'b1;
                        state <= 1'b1;
                    end
                end else begin //target port busy, hold this flit
                    if (next_data) begin
                        cache <= rd_data;
                        use_cache <= 1'b1;
                    end else begin
                        use_cache <= use_cache;
                        cache <= cache;
                    end
                    state <= 1'b0;
                    port_valid <= real_port_valid;
                    next_data <= 1'b0;
                end
            end else begin // FIFO is empty and no cache, do nothing
                use_cache <= 1'b0;
                cache <= cache;
                state <= 1'b0;
                port_valid <= real_port_valid;
                next_data <= 1'b0;
            end
        end
    end
endmodule

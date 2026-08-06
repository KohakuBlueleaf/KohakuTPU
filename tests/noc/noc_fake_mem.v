// A memory node for system tests: answers MEM_RD_REQ, absorbs MEM_WR_REQ.
//
// Stands in for MAS. It implements the wire protocol of spec s5.1/s5.2 and
// nothing else -- no cache, no reordering, no quantiser. That is deliberate:
// a system test should fail because the system is wrong, not because the stub
// is elaborate enough to have its own bugs.
//
//   MEM_RD_REQ  ->  len+1 x MEM_RD_RESP   (txn_id echoed)
//   MEM_WR_REQ  ->  consume len+1 data flits, then one MEM_WR_ACK
//
// The backdoor port lets a bench preload operands and read results back
// without going through the mesh, so the mesh is not in the loop when the
// bench is establishing what the right answer is.

`default_nettype none

module noc_fake_mem #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer POS_X      = 2,
    parameter integer POS_Y      = 1,
    parameter integer WORDS      = 1024     // 256-bit words
)(
    input  wire                   clk,
    input  wire                   resetn,

    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output reg  [FLIT_WIDTH-1:0]  noc_out_data,
    output reg                    noc_out_valid,
    input  wire                   noc_out_busy,

    // backdoor, for the bench only
    input  wire                   bd_we,
    input  wire [15:0]            bd_addr,      // word index
    input  wire [255:0]           bd_wdata,
    output wire [255:0]           bd_rdata,

    output reg  [15:0]            rd_count,
    output reg  [15:0]            wr_count
);
    localparam [3:0] T_MEM_RD_REQ  = 4'h0,
                     T_MEM_WR_REQ  = 4'h1,
                     T_MEM_RD_RESP = 4'h2,
                     T_MEM_WR_ACK  = 4'h3;

    localparam integer HP = FLIT_WIDTH - 4*POS_WIDTH;   // payload starts below here

    reg [255:0] mem [0:WORDS-1];

    assign bd_rdata = mem[bd_addr[$clog2(WORDS)-1:0]];

    always @(posedge clk)
        if (bd_we) mem[bd_addr[$clog2(WORDS)-1:0]] <= bd_wdata;

    // ---- inbound decode ----
    wire [3:0]           in_type = noc_in_data[HP-1 -: 4];
    wire [POS_WIDTH-1:0] in_sx   = noc_in_data[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH];
    wire [POS_WIDTH-1:0] in_sy   = noc_in_data[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH];
    wire [7:0]           in_txn  = noc_in_data[HP-5 -: 8];
    wire [33:0]          in_addr = noc_in_data[255 -: 34];
    wire [7:0]           in_len  = noc_in_data[215 -: 8];

    localparam [1:0] S_IDLE = 2'd0, S_READ = 2'd1, S_WRITE = 2'd2, S_ACK = 2'd3;
    reg [1:0]  state;
    reg [7:0]  left;
    reg [15:0] word;
    reg [POS_WIDTH-1:0] rx, ry;
    reg [7:0]  rtxn;

    // Accept whenever we are idle, or while consuming a write burst. Anything
    // else must be held off rather than dropped -- the link has no retry.
    assign noc_in_busy = !(state == S_IDLE || state == S_WRITE);

    always @(posedge clk) begin
        if (!resetn) begin
            state <= S_IDLE; left <= 8'd0; word <= 16'd0;
            rx <= 0; ry <= 0; rtxn <= 8'd0;
            noc_out_valid <= 1'b0; noc_out_data <= {FLIT_WIDTH{1'b0}};
            rd_count <= 16'd0; wr_count <= 16'd0;
        end else begin
            noc_out_valid <= 1'b0;

            case (state)
            S_IDLE: begin
                if (noc_in_valid) begin
                    rx   <= in_sx;  ry <= in_sy;  rtxn <= in_txn;
                    word <= in_addr[33:5];              // 256-bit words
                    left <= in_len;
                    if (in_type == T_MEM_RD_REQ) begin
                        state    <= S_READ;
                        rd_count <= rd_count + 16'd1;
                    end else if (in_type == T_MEM_WR_REQ) begin
                        state    <= S_WRITE;
                        wr_count <= wr_count + 16'd1;
                    end
                end
            end

            S_READ: begin
                // one response flit per cycle the link will take
                if (!noc_out_busy) begin
                    noc_out_data <= { rx, ry, POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                      T_MEM_RD_RESP, rtxn, (left == 8'd0), 3'b000,
                                      mem[word[$clog2(WORDS)-1:0]] };
                    noc_out_valid <= 1'b1;
                    word <= word + 16'd1;
                    if (left == 8'd0) state <= S_IDLE;
                    else              left  <= left - 8'd1;
                end
            end

            S_WRITE: begin
                if (noc_in_valid) begin
                    mem[word[$clog2(WORDS)-1:0]] <= noc_in_data[255:0];
                    word <= word + 16'd1;
                    if (left == 8'd0) state <= S_ACK;
                    else              left  <= left - 8'd1;
                end
            end

            S_ACK: begin
                if (!noc_out_busy) begin
                    noc_out_data <= { rx, ry, POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                      T_MEM_WR_ACK, rtxn, 1'b1, 3'b000, 256'd0 };
                    noc_out_valid <= 1'b1;
                    state <= S_IDLE;
                end
            end
            endcase
        end
    end

endmodule

`default_nettype wire

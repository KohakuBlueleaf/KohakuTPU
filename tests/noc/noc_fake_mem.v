// A memory node for system tests: answers MEM_RD_REQ, absorbs MEM_WR_REQ.
//
// Stands in for MAG. It implements the wire protocol of spec s5.1/s5.2 and
// nothing else -- no cache, no reordering, no AXI. That is deliberate: a
// system test should fail because the system is wrong, not because the stub is
// elaborate enough to have its own bugs.
//
//   MEM_RD_REQ            ->  len+1 x MEM_RD_RESP   (txn_id echoed)
//   MEM_RD_REQ + QUANT    ->  8 source words quantised into 4 x MEM_RD_RESP
//   MEM_WR_REQ            ->  consume len+1 data flits, then one MEM_WR_ACK
//
// THE QUANTISER IS NOT OPTIONAL HERE. Memory holds FP16 and the CU always
// asks for MXFP7, so a stub that ignores flags[4] hands back raw FP16 words
// for the cluster to read as operands. That produces numbers, not an error --
// which is the failure mode a stub is supposed to remove, not create.
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
                     T_MEM_WR_ACK  = 4'h3,
                     T_MEM_WR_DATA = 4'h4;

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
    // 34, NOT 40, and MEASURED: widening this to the NOC_MEM_ADDR field breaks
    // `system` and `system32`, which pass at 34. Their requester is not the CU.
    wire [33:0]          in_addr  = noc_in_data[255 -: 34];
    wire [7:0]           in_len   = noc_in_data[215 -: 8];
    wire [7:0]           in_flags = noc_in_data[207 -: 8];
    wire                 in_quant = in_flags[4];
    wire                 in_blay  = in_flags[5];

    localparam [2:0] S_IDLE  = 3'd0, S_READ  = 3'd1, S_WRITE = 3'd2, S_ACK = 3'd3,
                     S_QFILL = 3'd4, S_QWAIT = 3'd5, S_QEMIT = 3'd6;
    reg [2:0]  state;
    reg [7:0]  left;
    reg [15:0] word;
    reg [POS_WIDTH-1:0] rx, ry;
    reg [7:0]  rtxn;
    reg [1:0]  qemit;

    // ---- quantiser, same part MAG uses ----
    reg          q_start, q_blay;
    reg  [255:0] q_beat;
    reg          q_bv;
    wire         q_done;
    wire [255:0] q_w0, q_w1, q_w2, q_w3;

    mx_quant u_quant (
        .clk(clk), .rst(!resetn),
        .start(q_start), .b_layout(q_blay),
        .beat(q_beat), .beat_valid(q_bv), .need_beat(),
        .done(q_done), .word0(q_w0), .word1(q_w1), .word2(q_w2), .word3(q_w3)
    );

    // Accept whenever we are idle, or while consuming a write burst. Anything
    // else must be held off rather than dropped: the link retries on the
    // SENDER's side, and holding `busy` is how a receiver uses that.
    assign noc_in_busy = !(state == S_IDLE || state == S_WRITE);

    // THE LINK RETRIES, so this side must HOLD. A flit stays asserted until a
    // cycle in which the receiver is not busy, and the receiver accepts iff
    // `valid && !busy` -- see noc_outport.v and docs/noc/spec.md s2.1. Driving
    // `valid` from the PREVIOUS cycle's `busy` destroys any flit that meets a
    // receiver which raised busy in between: nothing re-presents it, and
    // `word`/`left`/`qemit` have already moved on. That is the same defect the
    // real output port was fixed for; this stub was left behind.
    //
    // Free when the register is empty OR is being emptied this cycle, which is
    // what sustains one flit per cycle rather than one every two.
    wire out_free = !noc_out_valid || !noc_out_busy;

    always @(posedge clk) begin
        if (!resetn) begin
            state <= S_IDLE; left <= 8'd0; word <= 16'd0;
            rx <= 0; ry <= 0; rtxn <= 8'd0;
            noc_out_valid <= 1'b0; noc_out_data <= {FLIT_WIDTH{1'b0}};
            rd_count <= 16'd0; wr_count <= 16'd0;
            q_start <= 1'b0; q_bv <= 1'b0; q_blay <= 1'b0;
            q_beat <= 256'd0; qemit <= 2'd0;
        end else begin
            // Cleared only once the receiver has actually taken it; otherwise
            // the flit stays asserted and is offered again next cycle. The
            // reloads below run after this in the same block, so an
            // accept-and-reload in one cycle still ends with valid high.
            if (noc_out_valid && !noc_out_busy) noc_out_valid <= 1'b0;
            q_start <= 1'b0;
            q_bv    <= 1'b0;

            case (state)
            S_IDLE: begin
                if (noc_in_valid) begin
                    rx   <= in_sx;  ry <= in_sy;  rtxn <= in_txn;
                    word <= in_addr[33:5];              // 256-bit words
                    left <= in_len;
                    if (in_type == T_MEM_RD_REQ && in_quant) begin
                        q_blay   <= in_blay;
                        q_start  <= 1'b1;
                        left     <= 8'd7;               // 8 source words
                        state    <= S_QFILL;
                        rd_count <= rd_count + 16'd1;
                    end else if (in_type == T_MEM_RD_REQ) begin
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
                if (out_free) begin
                    noc_out_data <= { rx, ry, POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                      T_MEM_RD_RESP, rtxn, (left == 8'd0), 3'b000,
                                      mem[word[$clog2(WORDS)-1:0]] };
                    noc_out_valid <= 1'b1;
                    word <= word + 16'd1;
                    if (left == 8'd0) state <= S_IDLE;
                    else              left  <= left - 8'd1;
                end
            end

            // ---- quantised read: 8 FP16 source words in, 4 MXFP7 flits out.
            // `start` is a pulse from S_IDLE, so the first beat can only be
            // presented here -- feeding one in the same cycle as start would
            // be swallowed by the quantiser's else-if chain.
            S_QFILL: begin
                q_beat <= mem[word[$clog2(WORDS)-1:0]];
                q_bv   <= 1'b1;
                word   <= word + 16'd1;
                if (left == 8'd0) state <= S_QWAIT;
                else              left  <= left - 8'd1;
            end
            S_QWAIT: if (q_done) begin
                qemit <= 2'd0;
                state <= S_QEMIT;
            end
            S_QEMIT: if (out_free) begin
                noc_out_data <= { rx, ry, POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                  T_MEM_RD_RESP, rtxn, (qemit == 2'd3), 3'b000,
                                  (qemit == 2'd0) ? q_w0 :
                                  (qemit == 2'd1) ? q_w1 :
                                  (qemit == 2'd2) ? q_w2 : q_w3 };
                noc_out_valid <= 1'b1;
                if (qemit == 2'd3) state <= S_IDLE;
                else               qemit <= qemit + 2'd1;
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
                if (out_free) begin
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

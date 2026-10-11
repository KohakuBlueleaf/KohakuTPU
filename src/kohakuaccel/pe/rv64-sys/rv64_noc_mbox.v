// rv64_noc_mbox -- the system core's way onto the mesh: dispatch out,
// completions in. SysCore drops the compute-unit shell by decision, and that
// dropped its only path onto the fabric with it.
//
// SOFTWARE WRITES A DISPATCH, NOT A FLIT: DST and four payload words set at
// leisure, GO commits atomically -- no tearing window. The four words are the
// WHOLE 256-bit CU_INST payload, so the primitive is complete even where the
// compiler uses a subset.

`default_nettype none

// Sends any flit type (DST[24] selects DST[23:20]); queues inbound CU_SIGNALs
// in the completion queue and every other inbound flit whole in the RX queue.

module rv64_noc_mbox #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer CQ_DEPTH   = 16,
    parameter integer RQ_DEPTH   = 8
)(
    input  wire                   clk,
    input  wire                   resetn,

    input  wire [POS_WIDTH-1:0]   my_x,
    input  wire [POS_WIDTH-1:0]   my_y,

    // ---- the control-region window ----
    input  wire                   cfg_en,
    input  wire [2:0]             cfg_addr,
    input  wire [63:0]            cfg_data,
    input  wire [2:0]             rd_addr,
    output reg  [63:0]            rd_data,

    // ---- the RX window: 0 HDR {valid[63], header[31:0]}, 1..4 P0..P3,
    //      5 STAT {used}; rx_pop drops the head ----
    input  wire [2:0]             rx_rd_addr,
    output reg  [63:0]            rx_rd_data,
    input  wire                   rx_pop,
    output wire                   rq_nonempty,

    // ---- flits, as a client of the node's hub ----
    output reg  [FLIT_WIDTH-1:0]  tx_data,
    output reg                    tx_valid,
    input  wire                   tx_busy,
    input  wire [FLIT_WIDTH-1:0]  rx_data,
    input  wire                   rx_valid,
    output wire                   rx_busy,

    output wire                   cq_nonempty,

    // ---- completions as they are accepted; a claimed one skips the queue ----
    output wire                   sig_v,
    output wire [POS_WIDTH-1:0]   sig_x,
    output wire [POS_WIDTH-1:0]   sig_y,
    output wire [7:0]             sig_code,
    input  wire                   sig_claim,
    output wire                   offered
);
    localparam [3:0] T_CU_INST   = 4'h5;
    localparam [3:0] T_CU_SIGNAL = 4'h6;

    localparam integer PAY_W = FLIT_WIDTH - 4*POS_WIDTH - 16;

    // Eight 8-byte slots before the mover at 0x80; ARG0..ARG3 are the payload
    // low-to-high (op at payload[255:252] = ARG3[63:60]). A WRITE to HEAD pops
    // it -- explicit, since the region answers a read a cycle late.
    localparam [2:0] R_DST  = 3'd0, R_ARG0 = 3'd1, R_ARG1 = 3'd2, R_ARG2 = 3'd3;
    localparam [2:0] R_ARG3 = 3'd4, R_GO   = 3'd5, R_STAT = 3'd6, R_HEAD = 3'd7;

    reg [POS_WIDTH-1:0] dst_x, dst_y;
    reg [3:0]           tx_type;     // DST[23:20], used when DST[24] is set
    reg                 tx_typed;
    reg [63:0]          arg0, arg1, arg2, arg3;
    reg [7:0]           txn;

    // ---- outbound: the whole 256-bit payload, low word first ---------------
    wire [PAY_W-1:0] payload = {arg3, arg2, arg1, arg0};

    always @(posedge clk) begin
        // RESET THE CONTROL, NOT THE DATA: dst and the args are written before
        // read, and reset values would cost a control set for nothing.
        if (!resetn) begin
            tx_valid <= 1'b0;
            txn      <= 8'd0;
        end
        else begin
            // HOLD UNTIL TAKEN. Withdrawing an offered flit destroys it.
            if (tx_valid && !tx_busy) begin
                tx_valid <= 1'b0;
            end

            if (cfg_en) begin
                case (cfg_addr)
                    R_DST: begin
                        dst_x    <= cfg_data[POS_WIDTH-1:0];
                        dst_y    <= cfg_data[8 +: POS_WIDTH];
                        tx_type  <= cfg_data[23:20];
                        tx_typed <= cfg_data[24];
                    end
                    R_ARG0: arg0 <= cfg_data;
                    R_ARG1: arg1 <= cfg_data;
                    R_ARG2: arg2 <= cfg_data;
                    R_ARG3: arg3 <= cfg_data;
                    R_GO: if (!tx_valid) begin
                        tx_data  <= {dst_x, dst_y, my_x, my_y,
                                     tx_typed ? tx_type : T_CU_INST,
                                     txn, 1'b1, 3'b000, payload};
                        tx_valid <= 1'b1;
                        txn      <= txn + 8'd1;
                    end
                    default: ;
                endcase
            end
        end
    end

`ifdef MBOX_TRACE
    // Each dispatch: the cycle GO offered it and the cycle the hub took it.
    reg [31:0] tr_cyc = 32'd0, tr_go = 32'd0;
    always @(posedge clk) begin
        tr_cyc <= tr_cyc + 32'd1;
        if (resetn && cfg_en && (cfg_addr == R_GO) && !tx_valid) tr_go <= tr_cyc;
        if (resetn && tx_valid && !tx_busy) begin
            $display("MBOX %0d go %0d taken %0d dst %0d,%0d op %0d", txn - 8'd1,
                     tr_go, tr_cyc, dst_x, dst_y, arg3[63:60]);
        end
    end
`endif

    // ---- inbound completions ----------------------------------------------
    localparam integer CQ_AW = $clog2(CQ_DEPTH);

    reg [63:0]      cq [0:CQ_DEPTH-1];
    reg [CQ_AW:0]   cq_wr, cq_rd;
    wire [CQ_AW:0]  cq_used  = cq_wr - cq_rd;
    wire            cq_full  = (cq_used == CQ_DEPTH[CQ_AW:0]);
    assign cq_nonempty = (cq_wr != cq_rd);

    wire [3:0] rx_type = rx_data[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    wire       rx_sig  = (rx_type == T_CU_SIGNAL);

    // ---- the RX queue: every non-signal flit, whole --------------------------
    localparam integer RQ_AW = $clog2(RQ_DEPTH);
    reg [FLIT_WIDTH-1:0] rq [0:RQ_DEPTH-1];
    reg [RQ_AW:0]        rq_wr, rq_rd;
    wire [RQ_AW:0]       rq_used = rq_wr - rq_rd;
    wire                 rq_full = (rq_used == RQ_DEPTH[RQ_AW:0]);
    assign rq_nonempty = (rq_wr != rq_rd);

    // Busy while either queue is full. A flit is taken only on valid && !busy,
    // so one held behind the other queue's backpressure is queued once.
    assign rx_busy = cq_full || rq_full;
    wire   rx_take = rx_valid && !rx_busy;

    always @(posedge clk) begin
        if (!resetn) begin
            rq_wr <= {(RQ_AW+1){1'b0}};
            rq_rd <= {(RQ_AW+1){1'b0}};
        end else begin
            if (rx_take && !rx_sig) begin
                rq[rq_wr[RQ_AW-1:0]] <= rx_data;
                rq_wr <= rq_wr + 1'b1;
            end
            if (rx_pop && rq_nonempty) begin
                rq_rd <= rq_rd + 1'b1;
            end
        end
    end

    wire [FLIT_WIDTH-1:0] rq_head = rq[rq_rd[RQ_AW-1:0]];
    always @(*) begin
        case (rx_rd_addr)
            3'd0:    rx_rd_data = {rq_nonempty, 31'd0, rq_head[FLIT_WIDTH-1 -: 32]};
            3'd1:    rx_rd_data = rq_head[0   +: 64];
            3'd2:    rx_rd_data = rq_head[64  +: 64];
            3'd3:    rx_rd_data = rq_head[128 +: 64];
            3'd4:    rx_rd_data = rq_head[192 +: 64];
            3'd5:    rx_rd_data = {{(64-RQ_AW-1){1'b0}}, rq_used};
            default: rx_rd_data = 64'd0;
        endcase
    end

    wire [POS_WIDTH-1:0] rx_sx = rx_data[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH];
    wire [POS_WIDTH-1:0] rx_sy = rx_data[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH];
    // A signal's code is the payload's top byte, its argument the 32 bits below.
    wire [7:0]  rx_code = rx_data[FLIT_WIDTH-4*POS_WIDTH-17 -: 8];
    wire [31:0] rx_arg  = rx_data[FLIT_WIDTH-4*POS_WIDTH-25 -: 32];

    assign sig_v    = rx_take && rx_sig;
    assign sig_x    = rx_sx;
    assign sig_y    = rx_sy;
    assign sig_code = rx_code;
    assign offered  = tx_valid;

    always @(posedge clk) begin
        if (!resetn) begin
            cq_wr  <= {(CQ_AW+1){1'b0}};
            cq_rd  <= {(CQ_AW+1){1'b0}};
        end
        else begin
            if (sig_v && !sig_claim) begin
                cq[cq_wr[CQ_AW-1:0]] <= {
                    8'd0,               // [63:56]
                    rx_sy,              // [55:52] source y
                    rx_sx,              // [51:48] source x
                    rx_code,            // [47:40] completion code
                    rx_arg,             // [39:8]  argument
                    {(64-8-2*POS_WIDTH-8-32){1'b0}}
                };
                cq_wr <= cq_wr + 1'b1;
            end
            if (cfg_en && (cfg_addr == R_HEAD) && cq_nonempty) begin
                cq_rd <= cq_rd + 1'b1;
            end
        end
    end

    always @(*) begin
        case (rd_addr)
            R_DST:  rd_data = {{(56-POS_WIDTH){1'b0}}, dst_y,
                               {(8-POS_WIDTH){1'b0}}, dst_x};
            R_ARG0: rd_data = arg0;
            R_ARG1: rd_data = arg1;
            R_ARG2: rd_data = arg2;
            R_ARG3: rd_data = arg3;
            // [31] is allocated and reads 0: a full queue holds the hub.
            R_STAT: rd_data = {32'd0, 1'b0, 15'd0,
                               tx_valid, 7'd0, {(8-CQ_AW-1){1'b0}}, cq_used};
            R_HEAD: rd_data = cq_nonempty ? cq[cq_rd[CQ_AW-1:0]] : 64'd0;
            default: rd_data = 64'd0;
        endcase
    end

endmodule

`default_nettype wire

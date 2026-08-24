// Pseudo compute unit -- a test double that conforms to the mandatory CU
// interface (docs/noc/spec.md s6) without computing anything real.
//
// It exists to close the loop the system actually needs:
//
//   receives CU_INST into an instruction FIFO
//   "executes" one every EXEC_CYCLES, validating each flit as it goes
//   emits CU_SIGNAL / INST_COMPLETE per instruction  (returns a dispatch credit)
//   emits CU_SIGNAL / BATCH_COMPLETE on the flit marked `last`, carrying the
//     program id from txn_id
//
// That last one is the important one. In production a CU writes its results
// straight to DRAM; the orchestrator never sees them and only learns whether a
// program finished. So the status path is a completion path, not a data path,
// and this models exactly that.
//
// Replies go to the `src` in the received flit, so the CU needs no knowledge of
// where the orchestrator is.

`default_nettype none

module noc_pseudo_cu #(
    parameter FLIT_WIDTH = 288,
    parameter POS_WIDTH  = 4,
    parameter POS_X      = 2,
    parameter POS_Y      = 2,
    parameter FIFO_DEPTH = 32,
    parameter EXEC_CYCLES = 4       // pretend an instruction takes this long
) (
    input  wire                  clk,
    input  wire                  resetn,

    input  wire [FLIT_WIDTH-1:0] noc_in_data,
    input  wire                  noc_in_valid,
    output wire                  noc_in_busy,
    output reg  [FLIT_WIDTH-1:0] noc_out_data,
    output reg                   noc_out_valid,
    input  wire                  noc_out_busy,

    // observation for the testbench
    output reg  [31:0]           inst_count,
    output reg  [31:0]           bad_count,
    output reg  [63:0]           body_xor
);

    localparam [3:0] T_CU_INST   = 4'h5;
    localparam [3:0] T_CU_SIGNAL = 4'h6;
    localparam [7:0] SIG_INST_COMPLETE  = 8'h00;
    localparam [7:0] SIG_BATCH_COMPLETE = 8'h01;

    // ------------------------------------------------------- instruction FIFO
    wire fifo_full, fifo_empty;
    wire [FLIT_WIDTH-1:0] fifo_head;
    reg  fifo_pop;

    sync_fifo #(.DATA_WIDTH(FLIT_WIDTH), .FIFO_DEPTH(FIFO_DEPTH),
                .MEMORY_TYPE("distributed")) u_inst (
        .clk(clk), .rst(!resetn),
        .wr_en(noc_in_valid && !fifo_full), .wr_data(noc_in_data), .wr_busy(fifo_full),
        .rd_en(fifo_pop), .rd_data(fifo_head), .rd_busy(fifo_empty)
    );
    assign noc_in_busy = fifo_full;

    // reject anything that is not a CU_INST addressed to us
    wire [POS_WIDTH-1:0] in_dx = noc_in_data[FLIT_WIDTH-1              -: POS_WIDTH];
    wire [POS_WIDTH-1:0] in_dy = noc_in_data[FLIT_WIDTH-POS_WIDTH-1    -: POS_WIDTH];
    wire [3:0]           in_ty = noc_in_data[FLIT_WIDTH-4*POS_WIDTH-1  -: 4];
    always @(posedge clk) begin
        if (!resetn) begin
            bad_count <= 32'd0;
        end
        else if (
            noc_in_valid
            && !fifo_full
            && (
                (in_dx !== POS_X[POS_WIDTH-1:0])
                || (in_dy !== POS_Y[POS_WIDTH-1:0])
                || (in_ty !== T_CU_INST)
            )
        ) begin
            bad_count <= bad_count + 32'd1;
        end
    end

    // ------------------------------------------------------------- execution
    wire [POS_WIDTH-1:0] h_sx  = fifo_head[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH];
    wire [POS_WIDTH-1:0] h_sy  = fifo_head[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH];
    wire [7:0]           h_txn = fifo_head[FLIT_WIDTH-4*POS_WIDTH-5 -: 8];
    wire                 h_last= fifo_head[FLIT_WIDTH-4*POS_WIDTH-13];
    wire [63:0]          h_body= fifo_head[63:0];

    localparam [1:0] E_IDLE = 2'd0, E_WORK = 2'd1, E_SIG = 2'd2;
    reg [1:0]  estate;
    reg [15:0] etimer;
    reg [7:0]  sig_code, sig_txn;
    reg [31:0] sig_arg;
    reg [POS_WIDTH-1:0] sig_dx, sig_dy;
    reg        sig_pending;

    // sig_pending has exactly one driver; set and clear are separate conditions
    // rather than separate always blocks.
    wire exec_done = (estate == E_WORK) && (etimer <= 16'd1);
    wire sig_go    = sig_pending && !noc_out_busy;

    always @(posedge clk) begin
        fifo_pop <= 1'b0;
        if (!resetn) begin
            estate <= E_IDLE; etimer <= 16'd0;
            inst_count <= 32'd0; body_xor <= 64'd0;
            sig_pending <= 1'b0;
            sig_dx <= 0; sig_dy <= 0; sig_txn <= 0; sig_code <= 0; sig_arg <= 0;
        end else begin
            if (exec_done) begin
                sig_pending <= 1'b1;
            end
            else if (sig_go) begin
                sig_pending <= 1'b0;
            end

            case (estate)
                E_IDLE: if (!fifo_empty && !sig_pending) begin
                    etimer <= EXEC_CYCLES[15:0];
                    estate <= E_WORK;
                end
                E_WORK: if (etimer > 16'd1) begin
                    etimer <= etimer - 16'd1;
                end
                        else begin
                            // "execute": account for it, then report
                            inst_count <= inst_count + 32'd1;
                            body_xor   <= body_xor ^ h_body;
                            sig_dx     <= h_sx;
                            sig_dy     <= h_sy;
                            sig_txn    <= h_txn;
                            sig_code   <= h_last ? SIG_BATCH_COMPLETE : SIG_INST_COMPLETE;
                            sig_arg    <= h_last ? {24'd0, h_txn}   // program id
                                                 : inst_count;
                            fifo_pop   <= 1'b1;
                            estate     <= E_SIG;
                        end
                E_SIG: if (sig_go) begin
                    estate <= E_IDLE;
                end
                default: estate <= E_IDLE;
            endcase
        end
    end

    // ------------------------------------------------------------ signal out
    // Same busy/valid discipline as OutPortSwitch: only commit when busy was low.
    always @(posedge clk) begin
        if (!resetn) begin
            noc_out_valid <= 1'b0;
            noc_out_data  <= {FLIT_WIDTH{1'b0}};
        end else begin
            noc_out_valid <= sig_go;
            if (sig_go) begin
                noc_out_data <= { sig_dx, sig_dy,                   // back to sender
                                  POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                  T_CU_SIGNAL, sig_txn, 1'b1, 3'b000,
                                  sig_code, sig_arg,
                                  {(FLIT_WIDTH-4*POS_WIDTH-16-40){1'b0}} };
            end
        end
    end

endmodule

`default_nettype wire

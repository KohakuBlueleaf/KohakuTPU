// noc_cu_base -- the part every compute unit needs, regardless of what it computes.
//
// docs/noc/spec.md s6 mandates that every CU expose the same two things: an
// ordered instruction FIFO whose free space is visible, and a fixed four-word
// control register block. This module implements both, plus the NoC plumbing, and
// hands the user's compute logic a small handshake:
//
//     inst_flit / inst_valid / inst_ready     one instruction to execute
//     exec_done / exec_result / exec_fault    report it retired
//     send_* / recv_*                         everything that is not an instruction
//
// A CU author writes only the datapath: framing, routing, completion signalling
// and CU_CTRL discovery are handled here, so a unit conforms by construction.
// The framework replies to whoever sent the instruction (`src` of the CU_INST
// flit), so a CU never needs to be told where its orchestrator is.

`default_nettype none

module noc_cu_base #(
    parameter FLIT_WIDTH  = 288,
    parameter POS_WIDTH   = 4,
    parameter POS_X       = 2,
    parameter POS_Y       = 2,
    parameter CU_TYPE     = 16'h0000,   // published in CU_CAPS
    parameter CU_VERSION  = 8'h01,
    parameter N_BUFFERS   = 4,
    parameter INST_DEPTH  = 32,         // 512 in production, see spec s6.1
    parameter RECV_DEPTH  = 16,
    parameter MEM_TYPE    = "distributed",
    // The receive queue is 288 bits wide and two thirds of this module's LUTs,
    // so it gets its own knob: `u_sig` is narrow enough that block RAM loses,
    // and one parameter governing both would force the wrong answer on one.
    parameter RECV_MEM    = "distributed"
) (
    input  wire                   clk,
    input  wire                   resetn,

    // ---- NoC local port ----
    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output reg  [FLIT_WIDTH-1:0]  noc_out_data,
    output reg                    noc_out_valid,
    input  wire                   noc_out_busy,

    // ---- instruction issue to the datapath ----
    output wire [FLIT_WIDTH-1:0]  inst_flit,
    output wire                   inst_valid,
    input  wire                   inst_ready,

    // ---- retirement from the datapath ----
    input  wire                   exec_done,
    input  wire [31:0]            exec_result,
    input  wire                   exec_fault,

    // Two counters only the datapath can know, read as CU_CTRL index 3.
    // Tie to zero where there is nothing to report.
    input  wire [63:0]            dbg_ctr,

    // ---- datapath-originated packets (CU_DATA, MEM_*, ...) ----
    input  wire [FLIT_WIDTH-1:0]  send_flit,
    input  wire                   send_valid,
    output wire                   send_ready,

    // ---- non-instruction packets for the datapath ----
    output wire [FLIT_WIDTH-1:0]  recv_flit,
    output wire                   recv_valid,
    input  wire                   recv_ready,

    // ---- visibility ----
    output wire [15:0]            inst_space,
    output wire                   busy
);
    localparam [3:0] T_CU_INST   = 4'h5;
    localparam [3:0] T_CU_SIGNAL = 4'h6;
    localparam [3:0] T_CU_CTRL   = 4'h7;
    localparam [7:0] SIG_INST_COMPLETE  = 8'h00;
    localparam [7:0] SIG_BATCH_COMPLETE = 8'h01;
    localparam [7:0] SIG_FAULT          = 8'h04;

    // header field extraction, matching src/kohakunoc/noc_pkt.vh
    `define HDR_DX(f) f[FLIT_WIDTH-1              -: POS_WIDTH]
    `define HDR_DY(f) f[FLIT_WIDTH-POS_WIDTH-1    -: POS_WIDTH]
    `define HDR_SX(f) f[FLIT_WIDTH-2*POS_WIDTH-1  -: POS_WIDTH]
    `define HDR_SY(f) f[FLIT_WIDTH-3*POS_WIDTH-1  -: POS_WIDTH]
    `define HDR_TY(f) f[FLIT_WIDTH-4*POS_WIDTH-1  -: 4]
    `define HDR_ID(f) f[FLIT_WIDTH-4*POS_WIDTH-5  -: 8]
    `define HDR_LS(f) f[FLIT_WIDTH-4*POS_WIDTH-13]

    // ------------------------------------------------------------------ RX
    wire [3:0] in_type = `HDR_TY(noc_in_data);
    wire       in_inst = (in_type == T_CU_INST);
    wire       in_ctrl = (in_type == T_CU_CTRL);

    wire inst_full, inst_empty, recv_full, recv_empty;
    wire inst_almost, recv_almost;
    wire [FLIT_WIDTH-1:0] inst_head;
    reg  inst_pop;

    // Stall if EITHER queue is full: busy must be meaningful even when
    // noc_in_valid is low, and the type field is only trustworthy alongside a
    // valid flit. Plain `full` despite the name -- sync_fifo passes
    // USE_ADV_FEATURES(0) -- which is safe only because the link RETRIES.
    // See docs/noc/spec.md s2.1.
    assign noc_in_busy = inst_almost | recv_almost;

    sync_fifo #(.DATA_WIDTH(FLIT_WIDTH), .FIFO_DEPTH(INST_DEPTH),
                .MEMORY_TYPE(MEM_TYPE)) u_inst (
        .clk(clk), .rst(!resetn),
        .wr_en(noc_in_valid && !noc_in_busy && in_inst),
        .wr_data(noc_in_data), .wr_busy(inst_full), .wr_almost(inst_almost),
        .rd_en(inst_pop), .rd_data(inst_head), .rd_busy(inst_empty)
    );

    // CU_CTRL is answered here, so it never reaches the datapath.
    // `recv_almost` reduces to plain `full` (USE_ADV_FEATURES is 0), and XPM
    // derives that from the pointers whichever memory backs the data, so
    // RECV_MEM cannot weaken the backpressure. What it does move is `recv_flit`
    // onto a BRAM output, in front of whatever reads it combinationally.
    sync_fifo #(.DATA_WIDTH(FLIT_WIDTH), .FIFO_DEPTH(RECV_DEPTH),
                .MEMORY_TYPE(RECV_MEM)) u_recv (
        .clk(clk), .rst(!resetn),
        .wr_en(noc_in_valid && !noc_in_busy && !in_inst && !in_ctrl),
        .wr_data(noc_in_data), .wr_busy(recv_full), .wr_almost(recv_almost),
        .rd_en(recv_valid && recv_ready), .rd_data(recv_flit), .rd_busy(recv_empty)
    );
    assign recv_valid = !recv_empty;

    // instruction-FIFO occupancy, so a sender can hold credits against it
    reg [15:0] inst_used;
    always @(posedge clk) begin
        if (!resetn) inst_used <= 16'd0;
        else begin
            case ({(noc_in_valid && !noc_in_busy && in_inst), inst_pop})
                2'b10: inst_used <= inst_used + 16'd1;
                2'b01: inst_used <= inst_used - 16'd1;
                default: ;
            endcase
        end
    end
    assign inst_space = INST_DEPTH[15:0] - inst_used;

    // ----------------------------------------------------- instruction issue
    // The datapath sees one instruction at a time; the framework remembers who to
    // answer and whether this was the last of a batch.
    reg  in_flight;
    reg  [POS_WIDTH-1:0] rep_x, rep_y;
    reg  [7:0]           rep_id;
    reg                  rep_last;

    assign inst_flit = inst_head;

    // ------------------------------------------------------------ signal TX
    // Completions are QUEUED, not held in one register: the datapath can retire
    // faster than a busy outbound link drains, and one register would let each
    // completion overwrite the last, losing credits silently.
    localparam SIG_DEPTH = 16;
    localparam SIG_W     = 2*POS_WIDTH + 48;   // rep_x, rep_y, id[8], code[8], arg[32]

    wire             sig_full, sig_empty;
    wire [SIG_W-1:0] sig_head;
    wire             sig_pend = !sig_empty;
    reg              ctrl_pend;

    wire [7:0]  ret_code = exec_fault ? SIG_FAULT
                         : rep_last   ? SIG_BATCH_COMPLETE
                                      : SIG_INST_COMPLETE;
    // a batch report carries the program id; anything else carries whatever the
    // datapath wants to say
    wire [31:0] ret_arg  = (!exec_fault && rep_last) ? {24'd0, rep_id} : exec_result;

    // Issue also stops when the signal queue is full: the report returns the
    // dispatch credit, so an instruction executed but unreportable stalls the
    // orchestrator permanently.
    assign inst_valid = !inst_empty && !in_flight && !sig_full;

    // TX arbitration. Declared here, not next to the output register, because
    // the state machines above consume these and Verilog needs the declaration
    // first -- iverilog letting that slide is how a 288-bit net became 1 bit
    // elsewhere in this project.
    //
    // Signals win: they return dispatch credits, so starving them stalls the
    // orchestrator. CU_CTRL next (a controller may be blocked on discovery),
    // then the datapath.
    //
    // `tx_free` covers the register being emptied THIS cycle, not merely an idle
    // link: deciding from `!noc_out_busy` alone pops the signal FIFO against a
    // link that may be busy by the time the flit is presented, and a lost
    // CU_SIGNAL never returns its credit.
    wire tx_free   = !noc_out_valid || !noc_out_busy;
    wire sig_sent  = sig_pend  && tx_free;
    wire ctrl_sent = ctrl_pend && tx_free && !sig_pend;
    assign send_ready = tx_free && !sig_pend && !ctrl_pend;

    sync_fifo #(.DATA_WIDTH(SIG_W), .FIFO_DEPTH(SIG_DEPTH),
                .MEMORY_TYPE("distributed")) u_sig (
        .clk(clk), .rst(!resetn),
        .wr_en(exec_done && in_flight),
        .wr_data({rep_x, rep_y, rep_id, ret_code, ret_arg}),
        .wr_busy(sig_full),
        .rd_en(sig_sent), .rd_data(sig_head), .rd_busy(sig_empty)
    );

    always @(posedge clk) begin
        inst_pop <= 1'b0;
        if (!resetn) begin
            in_flight <= 1'b0;
            rep_x <= 0; rep_y <= 0; rep_id <= 0; rep_last <= 0;
        end else begin
            if (inst_valid && inst_ready) begin
                rep_x     <= `HDR_SX(inst_head);
                rep_y     <= `HDR_SY(inst_head);
                rep_id    <= `HDR_ID(inst_head);
                rep_last  <= `HDR_LS(inst_head);
                in_flight <= 1'b1;
                inst_pop  <= 1'b1;
            end
            if (exec_done && in_flight) in_flight <= 1'b0;
        end
    end

    // -------------------------------------------------------- CU_CTRL replies
    // The mandatory four words. Answering here rather than in the datapath is what
    // lets a controller enumerate an unknown CU.
    wire [FLIT_WIDTH-1:0] ctrl_req = noc_in_data;
    wire                  ctrl_hit = noc_in_valid && !noc_in_busy && in_ctrl;
    wire [7:0]            ctrl_idx = ctrl_req[247 -: 8];

    reg [POS_WIDTH-1:0] ctrl_x, ctrl_y;
    reg [7:0]  ctrl_ridx, ctrl_txn;
    reg [63:0] ctrl_val;

    wire [63:0] caps_w = {CU_TYPE[15:0], CU_VERSION[7:0], N_BUFFERS[3:0],
                          INST_DEPTH[15:0], 20'd0};
    wire [63:0] stat_w = {busy, 1'b0 /*error*/, 14'd0, inst_space, 32'd0};

    // COUNTED HERE so every CU type reports cycles identically. Wall clock
    // cannot substitute: one JTAG access is ~32 ms against us of compute.
    reg [31:0] ctr_busy, ctr_inst;
    always @(posedge clk) begin
        if (!resetn) begin
            ctr_busy <= 32'd0; ctr_inst <= 32'd0;
        end else begin
            if (busy)      ctr_busy <= ctr_busy + 32'd1;
            if (exec_done) ctr_inst <= ctr_inst + 32'd1;
        end
    end

    always @(posedge clk) begin
        if (!resetn) begin
            ctrl_pend <= 1'b0; ctrl_val <= 64'd0;
            ctrl_x <= 0; ctrl_y <= 0; ctrl_ridx <= 0; ctrl_txn <= 0;
        end else begin
            if (ctrl_hit && !ctrl_pend) begin
                ctrl_pend <= 1'b1;
                ctrl_x    <= `HDR_SX(ctrl_req);
                ctrl_y    <= `HDR_SY(ctrl_req);
                ctrl_txn  <= `HDR_ID(ctrl_req);
                ctrl_ridx <= ctrl_idx;
                case (ctrl_idx)
                    8'd0:    ctrl_val <= caps_w;
                    8'd1:    ctrl_val <= stat_w;
                    8'd2:    ctrl_val <= {ctr_inst, ctr_busy};
                    8'd3:    ctrl_val <= dbg_ctr;
                    default: ctrl_val <= 64'd0;
                endcase
            end else if (ctrl_sent) ctrl_pend <= 1'b0;
        end
    end

    // ------------------------------------------------------------- TX output
    localparam PAD_SIG = FLIT_WIDTH - 4*POS_WIDTH - 16 - 40;

    // the queued signal's reply context, which is not necessarily the instruction
    // currently in flight
    wire [POS_WIDTH-1:0] s_x    = sig_head[SIG_W-1           -: POS_WIDTH];
    wire [POS_WIDTH-1:0] s_y    = sig_head[SIG_W-POS_WIDTH-1 -: POS_WIDTH];
    wire [7:0]           s_id   = sig_head[47 -: 8];
    wire [7:0]           s_code = sig_head[39 -: 8];
    wire [31:0]          s_arg  = sig_head[31 -: 32];

    always @(posedge clk) begin
        if (!resetn) begin
            noc_out_valid <= 1'b0;
            noc_out_data  <= {FLIT_WIDTH{1'b0}};
        end else begin
            // Hold the flit until the receiver takes it, rather than letting it
            // expire after one cycle.
            noc_out_valid <= sig_sent | ctrl_sent | (send_valid && send_ready) |
                             (noc_out_valid && noc_out_busy);
            if (sig_sent)
                noc_out_data <= { s_x, s_y, POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                  T_CU_SIGNAL, s_id, 1'b1, 3'b000,
                                  s_code, s_arg, {PAD_SIG{1'b0}} };
            else if (ctrl_sent)
                noc_out_data <= { ctrl_x, ctrl_y, POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                  T_CU_CTRL, ctrl_txn, 1'b1, 3'b000,
                                  8'd2 /*read response*/, ctrl_ridx, ctrl_val,
                                  {(FLIT_WIDTH-4*POS_WIDTH-16-16-64){1'b0}} };
            else if (send_valid && send_ready)
                noc_out_data <= send_flit;
        end
    end

    // not idle until the completions have actually left, not merely been generated
    assign busy = in_flight | !inst_empty | sig_pend;

endmodule

`default_nettype wire

// rv_noc_req -- the NoC requestor: everything about the framework memory
// protocol that RV32 software must never see.
//
// It owns transaction tags, descriptor legality, response matching, write
// ordering and backpressure. `lw` and `sw` are the only interface software
// gets; MEM_RD_REQ, MEM_WR_REQ, MEM_WR_DATA and CU_DATA are constructed here.
//
// ONE OUTSTANDING WRITE AT A TIME, AND THAT IS A CONTRACT, not a simplification
// to be relaxed later. docs/spec/memory-protocol.md s4.1 already forbids two
// open writes from one source -- slots at the agent are matched by source
// coordinate alone, so two would bind data to the wrong descriptor. The PE
// leans on it further: because this engine emits one burst at a time and the
// mesh preserves order between one source and one destination, PROGRAM ORDER IS
// ARRIVAL ORDER, which is exactly what makes "payload stores, then the doorbell
// last" a working protocol with no barriers and no acknowledgements.
//
// A FILL IS AN ENTRY READ, NOT A PLAIN READ. entry_words = 1 with STREAM set
// asks the agent's read engine for one 32-byte entry; a plain read would go
// through the port's shared write/read FSM instead and exclude a write for its
// whole duration (memory-protocol.md s3.1). The response is one flit, `last`
// set, `rsvd[1:0]` zero.
//
// WRITE ACKNOWLEDGEMENTS ARE COUNTED, then dropped. Nothing in the framework
// consumes them and a unit that holds one wedges the mesh (compute-unit-port.md
// s5) -- but flush-all needs to know when a writeback is actually in memory,
// and the ack is the only thing that says so.

`default_nettype none

module rv_noc_req #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer POS_X      = 2,
    parameter integer POS_Y      = 2,
    parameter integer MEM_X      = 0,       // the memory agent's port
    parameter integer MEM_Y      = 1,
    // The 40-bit physical base this PE's 2 GB software DRAM window maps onto.
    // OR-ed in, never added: the low 31 bits are zero by construction, so the
    // translation costs no logic at all.
    parameter [39:0]  DRAM_BASE  = 40'h00_0000_0000,
    parameter integer WR_MAX     = 1,       // un-acknowledged writes allowed
    // buf_id allocation, published in docs/arch/pe/programming.md as
    // flit-format.md s4.7.1 requires. 3 is reserved to the framework.
    parameter [7:0]   BUF_SPAD   = 8'd0,    // raw 32-byte granules
    parameter [7:0]   BUF_IMEM   = 8'd1,
    parameter [7:0]   BUF_SPAD_W = 8'd4,    // one 32-bit word, byte enabled
    parameter [7:0]   BUF_IMEM_W = 8'd5
)(
    input  wire                   clk,
    input  wire                   resetn,

    // ---- line fill, from the internal L1 ----
    input  wire                   fill_valid,
    output wire                   fill_ready,
    input  wire [30:0]            fill_addr,
    output reg                    resp_valid,
    output reg  [255:0]           resp_data,

    // ---- dirty writeback, from the internal L1 ----
    input  wire                   wb_valid,
    output wire                   wb_ready,
    input  wire [30:0]            wb_addr,
    input  wire [255:0]           wb_data,

    // ---- dispatch, from the MEM stage: one CU_INST at a peer ----
    // `txn` is a program id locally and `fin` -- the final {y,x} in the target
    // mesh -- when rsvd[2] marks the flit remote; mag_ilink.v:332-334 uses the
    // one field for both, so software owns which it is.
    input  wire                   disp_valid,
    output wire                   disp_ready,
    input  wire [POS_WIDTH-1:0]   disp_dx,
    input  wire [POS_WIDTH-1:0]   disp_dy,
    input  wire [7:0]             disp_txn,
    input  wire                   disp_last,
    input  wire [2:0]             disp_rsvd,
    input  wire [7:0]             disp_op,
    input  wire [31:0]            disp_pc,
    input  wire [31:0]            disp_arg,

    // ---- uncached peer push, from the MEM stage ----
    input  wire                   push_valid,
    output wire                   push_ready,
    input  wire [POS_WIDTH-1:0]   push_dx,
    input  wire [POS_WIDTH-1:0]   push_dy,
    input  wire                   push_win,
    input  wire [13:0]            push_gran,
    input  wire [2:0]             push_sel,
    input  wire [3:0]             push_be,
    input  wire [31:0]            push_data,

    // ---- noc_cu_base datapath handshake ----
    output reg  [FLIT_WIDTH-1:0]  send_flit,
    output reg                    send_valid,
    input  wire                   send_ready,

    // Responses are classified by rv_pe and handed here already sorted; this
    // module never sees a flit it does not own.
    input  wire                   rx_rd_resp,
    input  wire [7:0]             rx_txn,
    input  wire [255:0]           rx_data,
    input  wire                   rx_wr_ack,

    // ---- CU_SIGNAL, the completion edge of graph execution ----
    input  wire                   rx_sig,
    input  wire [7:0]             rx_sig_id,
    input  wire [7:0]             rx_sig_code,
    input  wire [31:0]            rx_sig_arg,

    input  wire                   sig_pop,
    output wire [7:0]             sig_cnt,
    output wire                   sig_ovf,
    output wire [7:0]             sig_code,
    output wire [7:0]             sig_id,
    output wire [31:0]            sig_arg,

    output wire [15:0]            wr_out,
    output wire                   idle
);
    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1;
    localparam [3:0] T_MEM_WR_DATA = 4'h4, T_CU_INST = 4'h5, T_CU_DATA = 4'h8;

    localparam integer PAY = FLIT_WIDTH - 4*POS_WIDTH - 16;   // 256 at defaults

    localparam [7:0] F_STREAM = 8'h40;

    localparam [1:0] S_IDLE = 2'd0, S_WDATA = 2'd1, S_PDATA = 2'd2;
    reg [1:0]  st;
    reg [3:0]  rd_tag;          // rotates so a stale response is detectable
    reg        rd_pend;

    reg [255:0] hold_data;
    reg [FLIT_WIDTH-1:0] hold_desc;

    // ---- the peer push, held in a register ---------------------------------
    // push_ready IS A FLOP. Driven from `take_push` it was the front of the
    // binding path -- the completion FIFO's `empty` reached the MEM stage stall
    // through send_ready, and the stall reached the branch predictor's counter:
    // 12 levels across five modules. One entry costs no throughput, since a
    // push holds the engine for two cycles, its descriptor and its data.
    reg                  pq_valid;
    reg [POS_WIDTH-1:0]  pq_dx, pq_dy;
    reg                  pq_win;
    reg [13:0]           pq_gran;
    reg [2:0]            pq_sel;
    reg [3:0]            pq_be;
    reg [31:0]           pq_data;

    // The dispatch register, for the reason `pq_*` is one: driven straight from
    // the MEM stage this would put the completion FIFO's `empty` on the store's
    // stall path.
    reg                  dq_valid;
    reg [POS_WIDTH-1:0]  dq_dx, dq_dy;
    reg [7:0]            dq_txn;
    reg                  dq_last;
    reg [2:0]            dq_rsvd;
    reg [7:0]            dq_op;
    reg [31:0]           dq_pc, dq_arg;

    reg [15:0] n_out;
    assign wr_out = n_out;
    // A push or a dispatch still in the register has not been sent, so the unit
    // is not idle and the completion it would gate must not go.
    assign idle = (
        (st == S_IDLE)
        && !send_valid
        && !rd_pend
        && !pq_valid
        && !dq_valid
    );

    wire tx_free = !send_valid || send_ready;

    // A read is only issued when nothing is outstanding: one blocking miss is
    // the whole miss model, so a second tag would have nothing to name.
    wire take_fill = (
        (st == S_IDLE)
        && tx_free
        && fill_valid
        && !rd_pend
    );
    // AND ONE UN-ACKNOWLEDGED WRITE. The agent allocates the LOWEST FREE slot
    // and issues the LOWEST READY one (mag_mem_port.v), which is not fair: a
    // requester that outruns AXI is handed a high slot that is then never the
    // lowest ready again. Measured on a 64-line flush -- slot 15 took a dirty
    // line, MAG alternated slots 0 and 1 for the other sixty-three, and that
    // line never reached DRAM, silently. Bounding what is outstanding keeps the
    // agent's in-use set where the two rules do compose.
    wire wr_room   = (n_out < WR_MAX[15:0]);
    wire take_wb = (
        (st == S_IDLE)
        && tx_free
        && wb_valid
        && !take_fill
        && wr_room
    );
    wire take_push = (
        (st == S_IDLE)
        && tx_free
        && pq_valid
        && !take_fill
        && !take_wb
    );
    // LAST, so nothing about the three existing emitters moves: a fill is a
    // blocking miss the core is already stopped on, and a dispatch is not.
    wire take_disp = (
        (st == S_IDLE)
        && tx_free
        && dq_valid
        && !take_fill
        && !take_wb
        && !take_push
    );

    assign fill_ready = take_fill;
    assign wb_ready   = take_wb;
    assign push_ready = !pq_valid;
    assign disp_ready = !dq_valid;

    wire [39:0] fill_phys = DRAM_BASE | {9'd0, fill_addr};
    wire [39:0] wb_phys   = DRAM_BASE | {9'd0, wb_addr};

    // `rsv` IS AN ARGUMENT, not a constant: bit 258 is "this flit leaves the
    // mesh" and 257:256 the destination mesh, so hard-coded 3'b000 made every
    // flit this PE emits unconditionally local. Local emitters pass 3'd0.
    function [FLIT_WIDTH-1:0] hdr;
        input [POS_WIDTH-1:0] dx;
        input [POS_WIDTH-1:0] dy;
        input [3:0]           ty;
        input [7:0]           txn;
        input                 lst;
        input [2:0]           rsv;
        input [PAY-1:0]       pay;
        hdr = {dx, dy, POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
               ty, txn, lst, rsv, pay};
    endfunction

    // MEM_RD_REQ / MEM_WR_REQ descriptor. The address field is FORTY bits, not
    // the 34 the flit table shows: mag_mem_port slices [255 -: 40] whatever
    // ADDR_W is, and the six bits above 34 carry the mesh and aperture. Every
    // other field the agent reads is named; the rest is zero, which the spec
    // requires rather than tolerates.
    function [PAY-1:0] descr;
        input [39:0] a;
        input [7:0]  len;
        input [7:0]  flags;
        input [7:0]  count;
        input [7:0]  ewords;
        descr = {a, len, flags, count, 24'd0, 2'd0, ewords, 158'd0};
    endfunction

    // CU_DATA descriptor. len = 0: one data flit follows, and flags[0] is clear
    // because a completion addressed at a compute unit is consumed by nobody.
    function [PAY-1:0] cudesc;
        input [7:0]  buf_id;
        input [15:0] offset;
        cudesc = {buf_id, offset, 8'd0, 8'd0, 4'd0, 4'd0, 208'd0};
    endfunction

    // CU_INST payload, where noc_cu_base's client reads it (rv_pe.v:325-327).
    function [PAY-1:0] idesc;
        input [7:0]  op;
        input [31:0] pc;
        input [31:0] arg;
        idesc = {op, pc, arg, {(PAY-72){1'b0}}};
    endfunction

    wire [7:0] push_buf = pq_win ? BUF_IMEM_W : BUF_SPAD_W;

    always @(posedge clk) begin
        if (!resetn) begin
            st         <= S_IDLE;
            send_valid <= 1'b0;
            rd_pend    <= 1'b0;
            rd_tag     <= 4'd0;
            n_out      <= 16'd0;
            resp_valid <= 1'b0;
            pq_valid   <= 1'b0;
            dq_valid   <= 1'b0;
        end else begin
            resp_valid <= 1'b0;

            if (send_valid && send_ready) begin
                send_valid <= 1'b0;
            end

            case (st)
                S_IDLE: begin
                    if (take_fill) begin
                        send_flit  <= hdr(MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                                          T_MEM_RD_REQ, {4'd0, rd_tag}, 1'b0, 3'd0,
                                          descr(fill_phys, 8'd0, F_STREAM, 8'd1, 8'd1));
                        send_valid <= 1'b1;
                        rd_pend    <= 1'b1;
                    end else if (take_wb) begin
                        send_flit  <= hdr(MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                                          T_MEM_WR_REQ, 8'h20, 1'b0, 3'd0,
                                          descr(wb_phys, 8'd0, 8'd0, 8'd0, 8'd0));
                        send_valid <= 1'b1;
                        // Counted HERE, not when the data flit goes: rv_l1 releases
                        // the writeback on this cycle, so a count that lags leaves a
                        // window in which flush-all sees zero outstanding and
                        // declares itself finished one line short.
                        n_out      <= n_out + 16'd1;
                        hold_data  <= wb_data;
                        hold_desc  <= hdr(MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                                          T_MEM_WR_DATA, 8'h20, 1'b1, 3'd0,
                                          {PAY{1'b0}});
                        st <= S_WDATA;
                    end else if (take_push) begin
                        send_flit  <= hdr(pq_dx, pq_dy, T_CU_DATA, 8'd0, 1'b0, 3'd0,
                                          cudesc(push_buf, {2'd0, pq_gran}));
                        send_valid <= 1'b1;
                        hold_data  <= {217'd0, pq_sel, pq_be, pq_data};
                        hold_desc  <= hdr(pq_dx, pq_dy, T_CU_DATA, 8'd0, 1'b1, 3'd0,
                                          {PAY{1'b0}});
                        st <= S_PDATA;
                    end else if (take_disp) begin
                        // ONE FLIT AND NO DATA BEAT: a CU_INST carries its whole
                        // {op, pc, arg} in the descriptor, so there is no S_*DATA.
                        send_flit  <= hdr(dq_dx, dq_dy, T_CU_INST, dq_txn, dq_last,
                                          dq_rsvd, idesc(dq_op, dq_pc, dq_arg));
                        send_valid <= 1'b1;
                    end
                end

                // The data flit follows its descriptor with nothing between: the
                // burst must leave this endpoint as one uninterrupted sequence.
                S_WDATA: if (tx_free) begin
                    send_flit  <= {hold_desc[FLIT_WIDTH-1:PAY], hold_data};
                    send_valid <= 1'b1;
                    st <= S_IDLE;
                end

                S_PDATA: if (tx_free) begin
                    send_flit  <= {hold_desc[FLIT_WIDTH-1:PAY], hold_data};
                    send_valid <= 1'b1;
                    st <= S_IDLE;
                end

                default: st <= S_IDLE;
            endcase

            // Fill and drain cannot collide: push_ready is !pq_valid and
            // take_push needs pq_valid, so a cycle is one or the other.
            if (push_valid && !pq_valid) begin
                pq_valid <= 1'b1;
                pq_dx    <= push_dx;
                pq_dy    <= push_dy;
                pq_win   <= push_win;
                pq_gran  <= push_gran;
                pq_sel   <= push_sel;
                pq_be    <= push_be;
                pq_data  <= push_data;
            end else if (take_push) begin
                pq_valid <= 1'b0;
            end

            if (disp_valid && !dq_valid) begin
                dq_valid <= 1'b1;
                dq_dx    <= disp_dx;
                dq_dy    <= disp_dy;
                dq_txn   <= disp_txn;
                dq_last  <= disp_last;
                dq_rsvd  <= disp_rsvd;
                dq_op    <= disp_op;
                dq_pc    <= disp_pc;
                dq_arg   <= disp_arg;
            end else if (take_disp) begin
                dq_valid <= 1'b0;
            end

            if (rx_rd_resp && rd_pend && (rx_txn[3:0] == rd_tag)) begin
                resp_data  <= rx_data;
                resp_valid <= 1'b1;
                rd_pend    <= 1'b0;
                rd_tag     <= rd_tag + 4'd1;
            end

            if (rx_wr_ack && (n_out != 16'd0)) begin
                n_out <= n_out - 16'd1;
            end
        end
    end

    // ---- the completion queue ----------------------------------------------
    // A CONTROL-REGISTER FIFO, NOT A SCRATCHPAD MAILBOX: the scratchpad has ONE
    // port, shared with the core's own loads and stores and named by rv_mem as
    // the base core's critical path. And a lost completion must be DETECTABLE --
    // a count plus a sticky overflow says so, a memory location does not.
    localparam integer SIG_D = 8;
    localparam integer SIG_W = 48;

    reg [SIG_W-1:0] sq [0:SIG_D-1];
    reg [3:0]       sq_wr, sq_rd;
    reg             sq_ovf;

    wire [3:0] sq_used  = sq_wr - sq_rd;
    wire       sq_full  = (sq_used == SIG_D[3:0]);
    wire       sq_empty = (sq_used == 4'd0);

    assign sig_cnt  = {4'd0, sq_used};
    assign sig_ovf  = sq_ovf;
    assign sig_code = sq[sq_rd[2:0]][47 -: 8];
    assign sig_id   = sq[sq_rd[2:0]][39 -: 8];
    assign sig_arg  = sq[sq_rd[2:0]][31 -: 32];

    always @(posedge clk) begin
        if (!resetn) begin
            sq_wr  <= 4'd0;
            sq_rd  <= 4'd0;
            sq_ovf <= 1'b0;
        end else begin
            if (rx_sig) begin
                if (sq_full) begin
                    sq_ovf <= 1'b1;
                end
                else begin
                    sq[sq_wr[2:0]] <= {rx_sig_code, rx_sig_id, rx_sig_arg};
                    sq_wr <= sq_wr + 4'd1;
                end
            end
            if (sig_pop && !sq_empty) begin
                sq_rd <= sq_rd + 4'd1;
            end
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (resetn && rx_sig && sq_full) begin
            $display("%0t ERROR rv_noc_req(%0d,%0d): CU_SIGNAL id %0h code %0h dropped -- completion queue full, sig_ovf latched",
                     $time, POS_X, POS_Y, rx_sig_id, rx_sig_code);
        end
        if (resetn && rx_rd_resp && !(rd_pend && (rx_txn[3:0] == rd_tag))) begin
            $display("%0t ERROR rv_noc_req(%0d,%0d): MEM_RD_RESP txn %0h with no matching outstanding fill -- dropped",
                     $time, POS_X, POS_Y, rx_txn);
        end
        if (resetn && rx_wr_ack && (n_out == 16'd0)) begin
            $display("%0t ERROR rv_noc_req(%0d,%0d): MEM_WR_ACK with no write outstanding",
                     $time, POS_X, POS_Y);
        end
    end
`endif

endmodule

`default_nettype wire

// One cluster: manager + 4-TCU cascade + accumulator.
//
//   mgr  ->  tcu -> tcu -> tcu -> tcu  ->  acu
//   L1       direct DSP cascade (PCOUT/PCIN + W)   resident output tile
//
// The compute half of the 2-port cluster: the manager takes one NoC port for
// operands and its own memory requests, the accumulator takes one for results
// and peer transfer.
//
// The accumulator's control is muxed between the manager (during a sweep) and
// the drain sequencer here (during an explicit drain). The sequencer WAITS on
// emit_valid rather than counting cycles -- the ACU's read-to-emit depth is a
// design variable, and a blind drain would return the wrong sub-tile.

`default_nettype none

module mx_cluster_node #(
    parameter integer TILES  = 256,     // resident output sub-tiles
    parameter integer GA     = 32,
    parameter integer GB     = 64,
    parameter integer ACC_MW = 14,
    parameter integer MODEL  = 0,
    parameter         L1_PRIM = "distributed",
    // SEPARATE from L1_PRIM: the accumulator is already READ_LAT=2, so it
    // takes "ultra" with no pipeline change, where L1 A/B needs one.
    parameter         TILE_PRIM = "block"
)(
    input  wire         clk,
    input  wire         rst,

    // ---- L1 load -------------------------------------------------------
    input  wire         l1_we,
    input  wire         l1_sel,
    input  wire [15:0]  l1_addr,
    input  wire [927:0] l1_data,

    // ---- sweep ---------------------------------------------------------
    input  wire         gemm_start,
    input  wire [7:0]   gemm_gm,
    input  wire [7:0]   gemm_gn,
    input  wire [7:0]   gemm_nk,
    input  wire [7:0]   gemm_anchor,
    input  wire         gemm_acc,   // add into the resident tile, do not reload
    input  wire [7:0]   gemm_aoff,  // this sweep's base entry in L1 A
    input  wire [7:0]   gemm_boff,  // ... and in L1 B
    // Which 256-entry half each side reads. A fill's bank rides in `l1_addr`,
    // which is already wide enough; only the sweep's 8-bit offsets need one.
    input  wire         gemm_abank,
    input  wire         gemm_bbank,
    input  wire         gemm_emit,  // hand each sub-tile out as it finishes
    output wire         gemm_busy,
    // The SWEEP alone -- the manager still issuing -- without the ~19-cycle
    // cascade and the accumulator's settling tail behind it. The next sweep
    // only needs this one; a DRAIN needs `gemm_busy`, because it takes the
    // accumulator's control mux and would cut the cascade off.
    output wire         sweep_busy,

    // ---- drain ---------------------------------------------------------
    input  wire         drain_start,
    input  wire [15:0]  drain_n,        // number of sub-tiles to emit
    input  wire         drain_fused,    // they came from the sweep; just wait
    // OP_SEND rather than OP_EMIT: the sub-tile leaves in the accumulator's own
    // float instead of being converted to FP16, which is the only form worth
    // sending to another cluster -- a K-split that rounded in between is what
    // going through memory already does.
    input  wire         drain_send,
    output wire         drain_busy,
    output wire [255:0] drain_data,     // 16 x FP16, one sub-tile
    output wire [15:0]  drain_idx,
    output wire         drain_valid,
    input  wire         drain_take,     // the write port accepted drain_data

    // ---- peer transfer -------------------------------------------------
    // mx_acu_fp's OP_ADD_PEER/OP_SEND/OP_FWD, so a matmul can span clusters:
    // one cluster's partial output tile is added into another's resident tile
    // instead of going out to memory and being read back.
    //
    // `peer_cmd` offers one sub-tile in the accumulator's own format and
    // `peer_ready` says the control mux is free to take it. `peer_open` is held
    // for the whole stream -- the wait for the sweep is paid at its head only.
    input  wire                     peer_open,
    input  wire                     peer_cmd,
    input  wire [15:0]              peer_idx,
    input  wire [16*(ACC_MW+8)-1:0] peer_data,
    output wire                     peer_ready,
    // The send half, for mx_cluster_cu to packetise.
    output wire [16*(ACC_MW+8)-1:0] peer_out,
    output wire                     peer_out_valid
);
    localparam integer TAW = (TILES <= 1) ? 1 : $clog2(TILES);
    localparam [2:0] OP_NOP = 3'd0, OP_ADD_PEER = 3'd3, OP_SEND = 3'd4,
                     OP_EMIT = 3'd5;
    localparam integer PW = 16*(ACC_MW+8);

    // ---- manager --------------------------------------------------------
    wire [895:0] a_bus, b_bus;
    wire         core_valid, core_first, part_valid;
    wire [2:0]      m_op;
    wire [TAW-1:0]  m_addr;
    wire            m_cmd;
    wire [31:0]     m_sa, m_sb;
    wire [7:0]      m_anchor;

    // GEMM is finished when the sweep has stopped issuing AND the accumulator
    // has settled. The manager knows only the first half -- its counters say
    // nothing about the ~19-deep cascade behind them -- and DRAIN takes the
    // control mux the cycle it starts, discarding anything still in flight.
    wire mgr_busy, acu_busy;
    assign gemm_busy  = mgr_busy || acu_busy;
    assign sweep_busy = mgr_busy;

    // The manager above reads these and the drain block below drives them: the
    // sweep produces emitted sub-tiles, so the collector's backpressure runs
    // backwards through it.
    wire emit_stall;
    wire emit_issue;

    mx_cluster_mgr #(.GA(GA), .GB(GB), .TAW(TAW), .L1_PRIM(L1_PRIM)) u_mgr (
        .clk(clk), .rst(rst),
        .l1_we(l1_we), .l1_sel(l1_sel), .l1_addr(l1_addr), .l1_data(l1_data),
        .gemm_start(gemm_start), .gemm_gm(gemm_gm), .gemm_gn(gemm_gn),
        .gemm_nk(gemm_nk), .gemm_anchor(gemm_anchor), .gemm_acc(gemm_acc),
        .gemm_aoff(gemm_aoff), .gemm_boff(gemm_boff),
        .gemm_abank(gemm_abank), .gemm_bbank(gemm_bbank),
        .gemm_emit(gemm_emit), .emit_stall(emit_stall),
        .emit_issue(emit_issue),
        .gemm_busy(mgr_busy),
        .a_out(a_bus), .b_out(b_bus),
        .core_valid(core_valid), .core_first(core_first),
        .part_valid(part_valid),
        .acu_op(m_op), .acu_addr(m_addr), .acu_cmd(m_cmd),
        .acu_sa(m_sa), .acu_sb(m_sb), .acu_anchor(m_anchor)
    );

    // ---- the cascade ----------------------------------------------------
    wire [383:0] part_bus;
    wire         part_first;

    mx_cluster_core #(.MODEL(MODEL)) u_core (
        .clk(clk), .rst(rst), .en(1'b1),
        .a_in(a_bus), .b_in(b_bus),
        .in_valid(core_valid), .in_first(core_first),
        .part_out(part_bus), .part_valid(part_valid), .part_first(part_first)
    );

    // ---- drain sequencer, PIPELINED --------------------------------------
    // EMITs issue back to back. Consecutive EMITs address DIFFERENT sub-tiles,
    // so REUSE_MIN -- which constrains repeats of ONE address -- does not apply
    // between them and the accumulator accepts a command every cycle. The FIFO
    // holds results that arrive while the write port is busy.
    //
    // DEEP, because a fused emit arrives in a BURST: the last K block of a sweep
    // completes one sub-tile per cycle, while memory retires a WBURST in ~11
    // cycles and serves every cluster. Averaged over a pass the write path keeps
    // up (512 sub-tiles per 4,096 cycles of compute), so the buffer only has to
    // carry the burst until the gap after it. Too small and the sweep stalls,
    // which is the serialisation the fusion removes. (Floor of 16 regardless:
    // xpm_fifo_sync's minimum write depth. Asking for less does not get a
    // smaller FIFO, it gets one that does not behave.)
    localparam integer DQ_DEPTH = 128;
    // Sub-tiles that may be outstanding: issued into the accumulator but not
    // yet taken by the write port. Bounded rather than trusted, because a
    // result arriving with nowhere to go is silently lost.
    localparam integer DQ_LIMIT = DQ_DEPTH - 16;

    // `d_iss` counts SUB-TILES and the other three count GRANULES, because a
    // send produces two of them per command and an emit one. `drain_idx` is
    // therefore a granule index in both modes, which is what lets the write
    // port stay unaware of the difference.
    reg [15:0] d_iss, d_n;      // sub-tiles issued, and how many to issue
    reg [15:0] d_got;           // granules returned by the accumulator
    reg [15:0] d_pop;           // granules the write port has taken
    reg        d_run;
    reg        d_send;          // this batch is OP_SEND, not OP_EMIT
    reg        d_gap;           // a send issues every other cycle
    // Whether the batch in flight came from the sweep. A fused batch must NOT
    // take the accumulator's control mux -- the sweep still owns it.
    reg        d_fused;
    reg        d_wait;          // a fused barrier is waiting for its sub-tiles
    wire [15:0] dn_w = (drain_n == 16'd0) ? 16'd1 : drain_n;

    wire [255:0]  emit_out;
    wire          emit_valid;
    wire [PW-1:0] acu_peer;
    wire          acu_peer_v;
    assign peer_out       = acu_peer;
    assign peer_out_valid = acu_peer_v;

    // A SEND RESULT IS TWO GRANULES. `peer_out` is PW bits and the queue -- and
    // every stage of the write-back behind it -- is 256, so the low half goes in
    // on the cycle it arrives and the high half on the next. Widening the whole
    // path to PW instead would have cost the drain buffer 96 bits x 16 slots to
    // carry a mode it is not in most of the time.
    //
    // This is also why a send drain issues every OTHER cycle: two granules per
    // command, one flit per cycle on the link, so the rates already match.
    reg            pq_pend;
    reg [PW-257:0] pq_hi;

    reg [2:0]      d_op;
    reg [TAW-1:0]  d_addr;
    reg            d_cmd;

    // ISSUED MINUS RETIRED: results inside the accumulator plus results in the
    // queue. Bounding this against the queue depth is what makes overflow
    // impossible -- bounding only the in-flight half lets the pipeline and the
    // queue each fill to DQ_DEPTH, silently losing a sub-tile.
    //
    // MAINTAINED, not subtracted. It equals `d_iss - d_pop` at every edge, but
    // as a 16-bit subtract it lands on the arc
    // `d_pop -> acu_busy_drain -> the ACU's DSP control` and misses 300 MHz.
    reg  [15:0] d_out;
    wire        dq_full, dq_empty;
    wire [271:0] dq_head;

    wire         dq_wr  = emit_valid || acu_peer_v || pq_pend;
    wire [255:0] dq_dat = acu_peer_v ? acu_peer[255:0]
                        : pq_pend    ? {{(512-PW){1'b0}}, pq_hi}
                                     : emit_out;

    sync_fifo #(.DATA_WIDTH(272), .FIFO_DEPTH(DQ_DEPTH),
                .MEMORY_TYPE("distributed")) u_dq (
        .clk(clk), .rst(rst),
        .wr_en(dq_wr), .wr_data({d_got, dq_dat}),
        .wr_busy(dq_full), .wr_almost(),
        .rd_en(drain_valid && drain_take), .rd_data(dq_head), .rd_busy(dq_empty)
    );

    always @(posedge clk) begin
        if (rst) begin
            pq_pend <= 1'b0; pq_hi <= {(PW-256){1'b0}};
        end else begin
            pq_pend <= acu_peer_v;
            if (acu_peer_v) pq_hi <= acu_peer[PW-1:256];
        end
    end

    assign drain_valid = !dq_empty;
    assign drain_idx   = dq_head[271:256];
    assign drain_data  = dq_head[255:0];
    // Not finished until the last result has LEFT: `d_run` covers issuing and
    // the queue covers everything still in flight behind it.
    assign drain_busy  = d_run || (d_out != 16'd0) || d_wait;
    // Hold the sweep when the collector cannot take any more. The pressure has
    // to be applied at ISSUE: once a command is in the accumulator its result
    // will come out ~19 cycles later whatever happens downstream.
    assign emit_stall  = (d_out >= DQ_LIMIT[15:0]);

    // ---- peer receive ----------------------------------------------------
    // Issued through the DRAIN SEQUENCER'S OWN command registers rather than as
    // a third input to the accumulator's control mux: that mux is one level deep
    // by design (mx_acu_fp timing rule 2) and a third source would put a second
    // level in front of the tile address. The sequencer is idle whenever a peer
    // sub-tile may issue, so its registers are free.
    //
    // `gemm_busy` is tested ONCE, at the head of a stream. It covers the
    // accumulator's REUSE_MIN tail, so re-testing it per sub-tile would idle
    // ~12 cycles each; inside a stream consecutive sub-tiles address DIFFERENT
    // tiles, so REUSE_MIN does not apply and the accumulator takes one a cycle.
    reg p_open;
    reg [PW-1:0] d_peer;

    assign peer_ready = p_open && !d_run && !d_wait && (d_out == 16'd0);
    wire   peer_iss   = peer_cmd && peer_ready;

    always @(posedge clk) begin
        if (rst) p_open <= 1'b0;
        else     p_open <= peer_open && (p_open || !gemm_busy);
    end

    always @(posedge clk) begin
        if (rst) begin
            d_iss <= 16'd0; d_got <= 16'd0; d_pop <= 16'd0; d_out <= 16'd0;
            d_n <= 16'd0; d_run <= 1'b0; d_fused <= 1'b0; d_wait <= 1'b0;
            d_send <= 1'b0; d_gap <= 1'b0;
            d_op <= OP_NOP; d_addr <= {TAW{1'b0}}; d_cmd <= 1'b0;
            d_peer <= {PW{1'b0}};
        end else begin
            d_cmd <= 1'b0;

            if (dq_wr)                     d_got <= d_got + 16'd1;
            if (drain_valid && drain_take) d_pop <= d_pop + 16'd1;
            // A fused sweep is its own issuer: every ADD_EMIT it puts into the
            // accumulator is a sub-tile that will come back.
            if (emit_issue && d_fused)     d_iss <= d_iss + 16'd1;

            // Same two events, mirrored. The branches below override this
            // wherever they reset d_iss and d_pop, so the two stay equal.
            d_out <= d_out + ((emit_issue && d_fused)     ? 16'd1 : 16'd0)
                           - ((drain_valid && drain_take) ? 16'd1 : 16'd0);

            // An emitting sweep opens a batch, and the counters restart HERE,
            // not at the DRAIN: results arrive long before the DRAIN is decoded
            // and `drain_idx` is `d_got`, so restarting late misplaces every
            // sub-tile. `d_fused` is set by an emitting sweep and never cleared
            // by a non-emitting one -- the next pass's first sweep may run while
            // this batch is still draining.
            if (gemm_start && gemm_emit) begin
                d_iss <= 16'd0; d_got <= 16'd0; d_pop <= 16'd0;
                d_out <= 16'd0;
                d_fused <= 1'b1;
                d_send <= 1'b0;     // a fused batch is OP_ADD_EMIT, never SEND
                d_gap <= 1'b0;
                d_run <= 1'b0;
            end else if (drain_start && !d_run && drain_fused) begin
                // A barrier, not an issuer. Waiting on `drain_busy` alone is a
                // race: the sweep may have started only cycles earlier, so
                // nothing is outstanding and the barrier passes before a single
                // sub-tile exists.
                d_n    <= dn_w;
                d_wait <= (d_pop < dn_w);
            end else if (drain_start && !d_run) begin
                d_n   <= dn_w;
                d_iss <= 16'd0;
                d_got <= 16'd0;
                d_pop <= 16'd0;
                d_out <= 16'd0;
                d_run <= 1'b1;
                d_fused <= 1'b0;    // an explicit drain DOES take the mux
                d_send <= drain_send;
                d_gap  <= 1'b0;
            end else if (d_run) begin
                // One EMIT per cycle, held back only by what can still be
                // absorbed downstream. DQ_LIMIT is DQ_DEPTH-16, NOT 1: it was 1
                // only because the write path once retired a single word per
                // visit to S_IDLE and deadlocked. It now collects into slots and
                // retires bursts, so the queue's own depth is the only bound
                // needed.
                if ((d_iss < d_n) && (d_out < DQ_LIMIT[15:0]) && !d_gap) begin
                    d_op   <= d_send ? OP_SEND : OP_EMIT;
                    d_addr <= d_iss[TAW-1:0];
                    d_cmd  <= 1'b1;
                    d_iss  <= d_iss + 16'd1;
                    d_gap  <= d_send;
                    // This branch issues, so d_out gains a granule per result
                    // here -- two for a send. `d_fused` is low throughout an
                    // explicit drain, so the unconditional term above
                    // contributed nothing but the pop.
                    d_out  <= d_out + (d_send ? 16'd2 : 16'd1)
                            - ((drain_valid && drain_take) ? 16'd1 : 16'd0);
                    if (d_iss + 16'd1 == d_n) d_run <= 1'b0;
                end else if (d_gap) d_gap <= 1'b0;
            end

            // Last, so a barrier armed and satisfied in the same cycle ends
            // correctly rather than hanging for a transition that has passed.
            if (d_wait && (d_pop >= d_n)) d_wait <= 1'b0;

            // After the chain above, which `peer_ready` has already excluded:
            // it requires the sequencer idle, so the two never write these
            // registers on the same cycle. `d_peer` is registered here rather
            // than read straight off the caller, so `peer_in` is valid on the
            // cycle `d_cmd` presents the command and the caller owes no hold.
            if (peer_iss) begin
                d_op   <= OP_ADD_PEER;
                d_addr <= peer_idx[TAW-1:0];
                d_cmd  <= 1'b1;
                d_peer <= peer_data;
            end
        end
    end

`ifndef SYNTHESIS
    // Losing one sub-tile here reads as an accumulator fault several modules
    // away. Say so instead.
    always @(posedge clk) begin
        if (!rst && dq_wr && dq_full)
            $display("%0t ERROR mx_cluster_node: drain queue overflow, sub-tile lost",
                     $time);
        // The gap between sends is what keeps these apart. If a send ever
        // issued back to back, the second result would overwrite a high half
        // that had not been queued yet and one sub-tile would come out spliced.
        if (!rst && acu_peer_v && pq_pend)
            $display("%0t ERROR mx_cluster_node: peer results collided, high half lost",
                     $time);
    end
`endif

    // ---- accumulator ----------------------------------------------------
    // The control mux belongs to the drain sequencer only for an EXPLICIT
    // drain, and `d_cmd` is part of the condition: it is registered, so the last
    // EMIT reaches the accumulator the cycle AFTER `d_run` falls. Dropping the
    // mux at `d_run` loses that command.
    //
    // A FUSED batch never takes the mux at all: the sweep produced it and still
    // owns the port. `d_fused` must therefore be STICKY until its own batch is
    // retired -- clearing it at the next sweep's start hands the mux to the
    // drain sequencer mid-sweep as soon as a pass's first GEMM overlaps the
    // previous pass's write-back. Faster and wrong.
    // `d_cmd` is also how a peer sub-tile takes the mux, for the one cycle it
    // is presented.
    wire acu_busy_drain = d_run || d_cmd || ((d_out != 16'd0) && !d_fused);

    mx_acu_fp #(.DEPTH(TILES), .ACC_MW(ACC_MW), .TILE_PRIM(TILE_PRIM)) u_acu (
        .clk(clk), .rst(rst), .en(1'b1),
        .part_in(part_bus),
        // THE SCALES ARE NOT MUXED. A drain issues OP_EMIT, which reads the
        // resident tile and never consumes `val_r`, so muxing them buys nothing
        // and costs the cluster its clock -- it puts `d_out` into the DSP's own
        // data input:
        //
        //   d_out -> acu_busy_drain -> sa/sb -> mm -> val_r's B port
        //
        // Only the three signals that actually SELECT behaviour are muxed.
        .sa(m_sa),
        .sb(m_sb),
        .anchor(m_anchor),
        .op(acu_busy_drain ? d_op : m_op),
        .tile_addr(acu_busy_drain ? d_addr : m_addr),
        .cmd_valid(acu_busy_drain ? d_cmd : m_cmd),
        .peer_in(d_peer), .peer_out(acu_peer), .peer_valid(acu_peer_v),
        .emit_out(emit_out), .emit_valid(emit_valid),
        .busy(acu_busy)
    );

endmodule

`default_nettype wire

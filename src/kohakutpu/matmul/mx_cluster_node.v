// One cluster: manager + 4-TCU cascade + accumulator.
//
//   mgr  ->  tcu -> tcu -> tcu -> tcu  ->  acu
//   L1       direct DSP cascade (PCOUT/PCIN + W)   resident output tile
//
// This is the compute half of the 2-port cluster. The NoC attachment lives
// above it: the manager takes one port for operands and its own memory
// requests, the accumulator takes one for results and peer transfer.
//
// The accumulator's control is muxed between two sources. During a sweep it
// comes from the manager (one command per part_valid, via the ordering FIFO).
// During a drain it comes from the drain sequencer here, which walks tile
// addresses and waits on emit_valid for each. It waits rather than counts:
// the ACU's read-to-emit depth is a design variable, and a drain issued blind
// against a stale constant would silently return the wrong sub-tile.

`default_nettype none

module mx_cluster_node #(
    parameter integer TILES  = 256,     // resident output sub-tiles
    parameter integer GA     = 32,
    parameter integer GB     = 64,
    parameter integer ACC_MW = 14,
    parameter integer MODEL  = 0,
    parameter         L1_PRIM = "distributed"
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
    output wire         drain_busy,
    output wire [255:0] drain_data,     // 16 x FP16, one sub-tile
    output wire [15:0]  drain_idx,
    output wire         drain_valid,
    input  wire         drain_take      // the write port accepted drain_data
);
    localparam integer TAW = (TILES <= 1) ? 1 : $clog2(TILES);
    localparam [2:0] OP_NOP = 3'd0, OP_EMIT = 3'd5;

    // ---- manager --------------------------------------------------------
    wire [895:0] a_bus, b_bus;
    wire         core_valid, core_first, part_valid;
    wire [2:0]      m_op;
    wire [TAW-1:0]  m_addr;
    wire            m_cmd;
    wire [31:0]     m_sa, m_sb;
    wire [7:0]      m_anchor;

    // GEMM is finished when the sweep has stopped issuing AND the accumulator
    // has settled. The manager only knows the first half: its own counters and
    // command FIFO say nothing about the ~19-deep cascade behind them. DRAIN
    // takes the accumulator's control mux the cycle it starts, so anything
    // still in flight when it does is discarded.
    wire mgr_busy, acu_busy;
    assign gemm_busy  = mgr_busy || acu_busy;
    assign sweep_busy = mgr_busy;

    // Declared here because the manager above reads them and the drain block
    // below drives them: the sweep is the producer of emitted sub-tiles now,
    // so the collector's backpressure runs backwards through it.
    wire emit_stall;
    wire emit_issue;

    mx_cluster_mgr #(.GA(GA), .GB(GB), .TAW(TAW), .L1_PRIM(L1_PRIM)) u_mgr (
        .clk(clk), .rst(rst),
        .l1_we(l1_we), .l1_sel(l1_sel), .l1_addr(l1_addr), .l1_data(l1_data),
        .gemm_start(gemm_start), .gemm_gm(gemm_gm), .gemm_gn(gemm_gn),
        .gemm_nk(gemm_nk), .gemm_anchor(gemm_anchor), .gemm_acc(gemm_acc),
        .gemm_aoff(gemm_aoff), .gemm_boff(gemm_boff),
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
    // It used to issue one EMIT and then wait for its result before issuing
    // the next, so every sub-tile paid the accumulator's whole read-to-emit
    // depth: ~9 cycles each against a floor of 2 (the write port sends a
    // descriptor and a data flit per sub-tile, so two cycles is what the
    // output can absorb). At 512 resident sub-tiles that tail became 23% of
    // the run.
    //
    // The wait was never necessary. Consecutive EMITs address DIFFERENT
    // sub-tiles, so `REUSE_MIN` -- which constrains repeats of ONE address --
    // does not apply between them, and the accumulator accepts a command every
    // cycle. What the old code actually needed was somewhere to put results
    // that arrive while the write port is busy. A small FIFO is that somewhere,
    // and it turns the depth from a per-sub-tile cost into a one-off.
    // 16, not 8: xpm_fifo_sync's minimum write depth is 16, and asking for
    // less does not get a smaller FIFO -- it gets one that does not behave.
    // The depth only has to cover the accumulator's read-to-emit distance plus
    // the write port's two-cycle turnaround, so 16 is already slack.
    // DEEP, because a fused emit arrives in a BURST. The last K block of a
    // sweep completes one sub-tile per cycle, while memory retires a burst of
    // WBURST beats in ~11 cycles and serves every cluster. Averaged over a
    // whole pass the write path keeps up easily -- 512 sub-tiles per 4,096
    // cycles of compute -- so all the buffer has to do is carry the burst
    // until the gap after it. Too small and the sweep stalls instead, which is
    // exactly the serialisation the fusion removes.
    localparam integer DQ_DEPTH = 128;
    // Sub-tiles that may be outstanding: issued into the accumulator but not
    // yet taken by the write port. Bounded rather than trusted, because a
    // result arriving with nowhere to go is silently lost.
    localparam integer DQ_LIMIT = DQ_DEPTH - 16;

    reg [15:0] d_iss, d_n;      // sub-tiles issued, and how many to issue
    reg [15:0] d_got;           // results returned by the accumulator
    reg [15:0] d_pop;           // results the write port has taken
    reg        d_run;
    // Whether the batch in flight came from the sweep. A fused batch must NOT
    // take the accumulator's control mux -- the sweep still owns it.
    reg        d_fused;
    reg        d_wait;          // a fused barrier is waiting for its sub-tiles
    wire [15:0] dn_w = (drain_n == 16'd0) ? 16'd1 : drain_n;

    wire [255:0] emit_out;
    wire         emit_valid;

    reg [2:0]      d_op;
    reg [TAW-1:0]  d_addr;
    reg            d_cmd;

    // ISSUED MINUS RETIRED: results still inside the accumulator plus results
    // sitting in the queue. Bounding this against the queue depth is what makes
    // overflow impossible -- bounding only the in-flight half would let the
    // pipeline and the queue each fill to DQ_DEPTH, and a result arriving with
    // nowhere to go loses a sub-tile silently. It is exact and owes nothing to
    // the pipeline depth, which is a design variable here as everywhere else.
    //
    // MAINTAINED, not subtracted. It equals `d_iss - d_pop` at every edge, but
    // computing it as a 16-bit subtract put it on the arc
    // `d_pop -> acu_busy_drain -> the ACU's DSP control` and measured 275.8 MHz
    // against a 300 MHz target. Nothing else on that arc is avoidable.
    reg  [15:0] d_out;
    wire        dq_full, dq_empty;
    wire [271:0] dq_head;

    sync_fifo #(.DATA_WIDTH(272), .FIFO_DEPTH(DQ_DEPTH),
                .MEMORY_TYPE("distributed")) u_dq (
        .clk(clk), .rst(rst),
        .wr_en(emit_valid), .wr_data({d_got, emit_out}),
        .wr_busy(dq_full), .wr_almost(),
        .rd_en(drain_valid && drain_take), .rd_data(dq_head), .rd_busy(dq_empty)
    );

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

    always @(posedge clk) begin
        if (rst) begin
            d_iss <= 16'd0; d_got <= 16'd0; d_pop <= 16'd0; d_out <= 16'd0;
            d_n <= 16'd0; d_run <= 1'b0; d_fused <= 1'b0; d_wait <= 1'b0;
            d_op <= OP_NOP; d_addr <= {TAW{1'b0}}; d_cmd <= 1'b0;
        end else begin
            d_cmd <= 1'b0;

            if (emit_valid)                d_got <= d_got + 16'd1;
            if (drain_valid && drain_take) d_pop <= d_pop + 16'd1;
            // A fused sweep is its own issuer: every ADD_EMIT it puts into the
            // accumulator is a sub-tile that will come back.
            if (emit_issue && d_fused)     d_iss <= d_iss + 16'd1;

            // Same two events, mirrored. The branches below override this
            // wherever they reset d_iss and d_pop, so the two stay equal.
            d_out <= d_out + ((emit_issue && d_fused)     ? 16'd1 : 16'd0)
                           - ((drain_valid && drain_take) ? 16'd1 : 16'd0);

            // An emitting sweep opens a batch. The counters restart here
            // rather than at the DRAIN, because the results start arriving
            // long before the DRAIN is decoded -- `drain_idx` is `d_got`, so
            // restarting late would misplace every sub-tile of the tile.
            // SET by an emitting sweep, never cleared by a non-emitting one:
            // the next pass's first sweep may run while this batch is still
            // draining, and it must not look like the batch ended.
            if (gemm_start && gemm_emit) begin
                d_iss <= 16'd0; d_got <= 16'd0; d_pop <= 16'd0;
                d_out <= 16'd0;
                d_fused <= 1'b1;
                d_run <= 1'b0;
            end else if (drain_start && !d_run && drain_fused) begin
                // A barrier, not an issuer. Waiting for `drain_busy` alone
                // would be a race: the sweep may have started only cycles
                // earlier, so nothing is outstanding yet and the barrier would
                // pass before a single sub-tile had been produced.
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
            end else if (d_run) begin
                // One EMIT per cycle, held back only by what can still be
                // absorbed downstream.
                //
                // DQ_LIMIT is DQ_DEPTH-16, NOT 1. It was 1 for a while, because
                // raising it deadlocked the write path: the CU emitted a
                // MEM_WR_REQ/MEM_WR_DATA pair per sub-tile while MAG retired
                // one single-word write per visit to S_IDLE, so MAG's input
                // queue filled and wedged on a data flit whose slot was still
                // on the AXI bus -- `C write amp 0.01x`, `in_bp 70.4%`.
                // The write path now collects into slots and retires bursts,
                // which removed it: 8 CU measures `C write amp 1.00x` and
                // `in_bp 2.1%`. The bound that remains is the queue's, and it
                // is what stops a result arriving with nowhere to go.
                if ((d_iss < d_n) && (d_out < DQ_LIMIT[15:0])) begin
                    d_op   <= OP_EMIT;
                    d_addr <= d_iss[TAW-1:0];
                    d_cmd  <= 1'b1;
                    d_iss  <= d_iss + 16'd1;
                    // This branch issues, so d_out gains one here. `d_fused`
                    // is low throughout an explicit drain, so the unconditional
                    // term above contributed nothing but the pop.
                    d_out  <= d_out + 16'd1
                            - ((drain_valid && drain_take) ? 16'd1 : 16'd0);
                    if (d_iss + 16'd1 == d_n) d_run <= 1'b0;
                end
            end

            // Last, so a barrier armed and satisfied in the same cycle ends
            // correctly rather than hanging for a transition that has passed.
            if (d_wait && (d_pop >= d_n)) d_wait <= 1'b0;
        end
    end

`ifndef SYNTHESIS
    // The bound above is the only thing keeping a result from arriving with
    // nowhere to go, and losing one sub-tile reads as an accumulator fault
    // several modules away. Say so instead.
    always @(posedge clk)
        if (!rst && emit_valid && dq_full)
            $display("%0t ERROR mx_cluster_node: drain queue overflow, sub-tile lost",
                     $time);
`endif

    // ---- accumulator ----------------------------------------------------
    // The control mux belongs to the drain sequencer only for an EXPLICIT
    // drain, and `d_cmd` is part of the condition: it is registered, so the
    // last EMIT reaches the accumulator the cycle AFTER `d_run` falls.
    // Dropping the mux at `d_run` loses that command -- measured, 252 of 7260
    // checks in mx_cluster_node_tb.
    //
    // A FUSED batch never takes the mux at all: the sweep produced it and
    // still owns the port. `d_fused` therefore has to be STICKY until its own
    // batch is retired -- clearing it at the next sweep's start handed the mux
    // to the drain sequencer mid-sweep the moment a pass's first GEMM was
    // allowed to overlap the previous pass's write-back. That was also
    // measured: 23,536 cycles and `p99 vs fp64` 4.15e+01. Faster and wrong.
    wire acu_busy_drain = d_run || d_cmd || ((d_out != 16'd0) && !d_fused);

    mx_acu_fp #(.DEPTH(TILES), .ACC_MW(ACC_MW)) u_acu (
        .clk(clk), .rst(rst), .en(1'b1),
        .part_in(part_bus),
        // THE SCALES ARE NOT MUXED. They used to be forced to zero during a
        // drain, and that was defensive rather than functional: a drain issues
        // OP_EMIT, which reads the resident tile and never consumes `val_r`.
        // The mux bought nothing and cost the cluster its clock -- it put
        // `d_out` into the DSP's own data input:
        //
        //   d_out -> acu_busy_drain -> sa/sb -> mm -> val_r's B port
        //
        // measured as the critical path at 285.9 MHz against a 300 MHz target.
        // Only the three signals that actually SELECT behaviour are muxed.
        .sa(m_sa),
        .sb(m_sb),
        .anchor(m_anchor),
        .op(acu_busy_drain ? d_op : m_op),
        .tile_addr(acu_busy_drain ? d_addr : m_addr),
        .cmd_valid(acu_busy_drain ? d_cmd : m_cmd),
        .peer_in({(16*(ACC_MW+8)){1'b0}}), .peer_out(), .peer_valid(),
        .emit_out(emit_out), .emit_valid(emit_valid),
        .busy(acu_busy)
    );

endmodule

`default_nettype wire

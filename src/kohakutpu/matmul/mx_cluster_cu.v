// A cluster as a TWO-PORT NoC compute unit.
//
//   NoC <-> manager <-> tcu -> tcu -> tcu -> tcu -> acu <-> NoC
//            L1            direct DSP cascade        resident tile
//
// Port 0 (manager)  instructions in, operand fetch out, completion signals.
// Port 1 (acu)      result write-back.
//
// Two ports, not five. The chain eats eight 256-bit operand words per cycle and
// a port delivers one, so no port count closes an 8x deficit -- REUSE does: a
// Gm x Gn sub-tile block needs 4(Gm+Gn)/(Gm*Gn) words per cycle, 0.375 at
// 16x32. See docs/compute/tensor-isa.md s1.
//
// Instruction, in the CU_INST payload:
//
//   [255:252] op      1 = FILL, 2 = GEMM, 3 = DRAIN
//   [251:218] addr    FILL: operand base   DRAIN: destination base
//   [217:202] n       FILL: entries        DRAIN: sub-tiles
//   [201]     sel     FILL: 0 = A, 1 = B
//   [200]     acc     GEMM: accumulate into the resident tile instead of
//                     reloading it, so one output tile can span several
//                     instructions
//   [199:192] gm      GEMM: row groups
//   [191:184] gn      GEMM: column groups
//   [183:176] nk      GEMM: K blocks
//   [175:168] anchor  GEMM/DRAIN: common output exponent
//   [167:144] peers   FILL: other clusters receiving this fetch, {y,x} each
//   [143:142] npeer   FILL: how many of them are present
//   [141]     preq    FILL: the operand is ALREADY MXFP7 in memory, so memory
//                     must not quantise it again
//   [140:133] eoff    FILL: first L1 entry to write
//   [132:125] aoff    GEMM: base L1 entry on the A side
//   [124:117] boff    GEMM: base L1 entry on the B side
//   [116]     emit    GEMM: hand each sub-tile out as its last K block
//                     completes it, and write it to `addr`
//   [115]     fuse    DRAIN: the results already came out of the sweep; wait
//                     for them to leave rather than issuing reads for them
//
// One L1 entry is four consecutive 256-bit operand words -- the four K-slices
// of one row group (A) or column group (B) -- so FILL asks for entries and
// assembles each set of four into the 928-bit entry the sweep consumes.

`default_nettype none

module mx_cluster_cu #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer MGR_X      = 0,      // manager's NoC coordinates
    parameter integer MGR_Y      = 0,
    parameter integer ACU_X      = 0,      // accumulator's NoC coordinates
    parameter integer ACU_Y      = 1,
    parameter integer MEM_X      = 1,
    parameter integer MEM_Y      = 1,
    parameter integer TILES      = 256,
    parameter integer GA         = 32,
    parameter integer GB         = 32,
    parameter integer ACC_MW     = 14,
    parameter integer INST_DEPTH = 32,
    // Operand response buffer, in FLITS. Sized to hold PREFETCH entry bursts
    // of 4 flits each: this is what bounds how far the fill requester may run
    // ahead, and therefore how much memory latency it can hide. Distributed
    // RAM, one per CU.
    parameter integer RECV_DEPTH = 64,
    parameter integer MODEL      = 0,
    parameter         L1_PRIM    = "distributed"
)(
    input  wire                   clk,
    input  wire                   resetn,

    // ---- port 0: manager ----
    input  wire [FLIT_WIDTH-1:0]  m_in_data,
    input  wire                   m_in_valid,
    output wire                   m_in_busy,
    output wire [FLIT_WIDTH-1:0]  m_out_data,
    output wire                   m_out_valid,
    input  wire                   m_out_busy,

    // ---- port 1: accumulator ----
    input  wire [FLIT_WIDTH-1:0]  a_in_data,
    input  wire                   a_in_valid,
    output wire                   a_in_busy,
    output wire [FLIT_WIDTH-1:0]  a_out_data,
    output wire                   a_out_valid,
    input  wire                   a_out_busy,

    output wire [15:0]            fills_done,
    output wire [15:0]            gemms_done,
    output wire [15:0]            drains_done
);
    localparam [3:0] T_MEM_RD_REQ  = 4'h0,
                     T_MEM_WR_REQ  = 4'h1,
                     T_MEM_RD_RESP = 4'h2,
                     // A write is a descriptor followed by its data, and the
                     // mesh interleaves streams -- another node's flit can land
                     // between the two. A data flit therefore has to be
                     // identifiable BY TYPE rather than by position, or memory
                     // pairs it with whichever write happens to be open and
                     // silently stores the wrong bytes.
                     T_MEM_WR_DATA = 4'h4;

    localparam [3:0] OP_FILL = 4'd1, OP_GEMM = 4'd2, OP_DRAIN = 4'd3;

    // ================================================ framework, port 0
    wire [FLIT_WIDTH-1:0] inst_flit, recv_flit;
    wire                  inst_valid, recv_valid, send_ready;
    reg                   inst_ready, recv_ready;
    reg                   exec_done;
    reg  [31:0]           exec_result;
    reg  [FLIT_WIDTH-1:0] send_flit;
    reg                   send_valid;

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(MGR_X), .POS_Y(MGR_Y),
        .CU_TYPE(16'h4D47), .CU_VERSION(8'h01), .N_BUFFERS(2),
        .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH)
    ) u_base (
        .clk(clk), .resetn(resetn),
        .noc_in_data(m_in_data), .noc_in_valid(m_in_valid), .noc_in_busy(m_in_busy),
        .noc_out_data(m_out_data), .noc_out_valid(m_out_valid), .noc_out_busy(m_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(exec_result), .exec_fault(1'b0),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .recv_flit(recv_flit), .recv_valid(recv_valid), .recv_ready(recv_ready),
        .inst_space(), .busy()
    );

    // `n` is SIXTEEN bits: a 512-sub-tile resident tile means a DRAIN of 512,
    // and an 8-bit field wraps at 256, silently re-draining the start of the
    // tile. Fields below it moved down rather than being overlapped with gm/gn.
    wire [3:0]  i_op   = inst_flit[255 -: 4];
    wire [33:0] i_addr = inst_flit[251 -: 34];
    wire [15:0] i_n    = inst_flit[217 -: 16];
    wire        i_sel  = inst_flit[201];
    wire [7:0]  i_gm   = inst_flit[199 -: 8];
    wire [7:0]  i_gn   = inst_flit[191 -: 8];
    wire [7:0]  i_nk   = inst_flit[183 -: 8];
    wire [7:0]  i_anch = inst_flit[175 -: 8];
    // ACCUMULATE. Without it every GEMM opens with OP_LOAD, overwriting the
    // resident tile, so an output tile could only come from ONE instruction and
    // a K longer than L1 could not be expressed. With it K splits into chunks
    // chaining into the same tile, keeping the dataflow output-stationary.
    wire        i_acc  = inst_flit[200];
    // SHARED FETCH. Every cluster sweeps the same rows of A, so at any moment
    // they issue byte-identical FILL A requests; served separately that is one
    // DRAM read and one quantiser pass PER CONSUMER for a bit-identical result.
    //
    // `i_peer` lists the other clusters sharing THIS fill as {y,x} node indices,
    // `i_npeer` how many are present. Four destinations total is not arbitrary:
    // with eight clusters tiled 4x2 over the output, A is shared by the 2 in a
    // row and B by the 4 in a column, so the emitter stays a fixed mux.
    wire [23:0] i_peer  = inst_flit[167 -: 24];
    wire [1:0]  i_npeer = inst_flit[143 -: 2];
    // PRE-QUANTISED OPERAND: already converted on the way into memory, so the
    // fetch is 4 words instead of 8 FP16 beats and no quantiser pass. A property
    // of the OPERAND carried by the instruction, which keeps memory free of an
    // address map -- see docs/isa/memory.md s3.
    wire        i_preq  = inst_flit[141];
    // L1 IS ADDRESSABLE, not one buffer per side. `eoff` says where a FILL
    // lands, `aoff`/`boff` where a sweep reads, so the driver can fill one half
    // while the other is swept and leave an unchanged operand in place.
    wire [7:0]  i_eoff  = inst_flit[140 -: 8];
    wire [7:0]  i_aoff  = inst_flit[132 -: 8];
    wire [7:0]  i_boff  = inst_flit[124 -: 8];
    // FUSED DRAIN. `emit` streams results out during the sweep; `fuse` turns
    // DRAIN into the barrier that waits for them, instead of a second pass that
    // needs one accumulator command per sub-tile the sweep has none spare of.
    wire        i_emit  = inst_flit[116];
    wire        i_fuse  = inst_flit[115];

    wire [3:0] rtype = recv_flit[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    // A response carries its own address. `rtag` is the L1 entry this flit
    // belongs to -- the value this CU put in the request's txn field, echoed
    // back by MAG -- and `rword` is which of the entry's four words it is.
    // Nothing about placement depends on the order flits arrive in.
    wire [7:0] rtag  = recv_flit[FLIT_WIDTH-4*POS_WIDTH-5  -: 8];
    wire [1:0] rword = recv_flit[FLIT_WIDTH-4*POS_WIDTH-15 -: 2];

    // ================================================ the cluster
    // Write-back port state, declared here because the DRAIN sequencer below
    // waits on it: a drain is not done until its last write has left the CU.
    reg          w_valid;
    reg [1:0]    w_st;
    // Declared early because DRAIN's completion test and `drain_take` both
    // read them, and the write port is defined further down.
    wire         w_full;
    wire         w_idle;

    reg          l1_we, l1_sel;
    reg  [15:0]  l1_addr;
    reg  [927:0] l1_data;
    reg          gemm_start;
    reg  [7:0]   gm_r, gn_r, nk_r, anch_r, aoff_r, boff_r;
    reg          acc_r, emit_r, fuse_r;
    wire         sweep_busy;
    // mx_acu_fp's REUSE_MIN. A tiling this size or larger cannot revisit a
    // tile address inside the reuse window, which is what lets one sweep start
    // while the previous one's cascade is still draining.
    localparam [15:0] ACU_REUSE_MIN = 16'd5;

    // DECODED ONCE, NOT RECOMPUTED EVERY CYCLE. Derived combinationally from
    // gm_r/gn_r it is an 8x8 fabric multiply feeding the state machine's own
    // clock enable:
    //
    //   gn_r -> gm_r*gn_r -> >= REUSE_MIN -> st/CE     13 levels
    //
    // Nothing needs the product, only whether it reaches 5. Both operands are at
    // least 1, so `Gm*Gn < 5` is exactly (1,<=4), (<=4,1) and (2,2) -- the same
    // expansion as mx_cluster_mgr's pacing. See docs/compute/accumulator.md s4.
    wire [7:0] i_gm_w = (i_gm == 8'd0) ? 8'd1 : i_gm;
    wire [7:0] i_gn_w = (i_gn == 8'd0) ? 8'd1 : i_gn;
    wire       i_wide = !(((i_gm_w == 8'd1) && (i_gn_w <= 8'd4))
                       || ((i_gn_w == 8'd1) && (i_gm_w <= 8'd4))
                       || ((i_gm_w == 8'd2) && (i_gn_w == 8'd2)));
    reg          wide_r;
    wire         gemm_wide = wide_r;

`ifndef SYNTHESIS
    // The expansion above does not follow ACU_REUSE_MIN. Say so at elaboration
    // rather than let a sweep overlap one it must not.
    initial if (ACU_REUSE_MIN != 16'd5)
        $display("ERROR mx_cluster_cu: i_wide is expanded for ACU_REUSE_MIN=5, got %0d",
                 ACU_REUSE_MIN);
`endif
    wire         gemm_busy;
    reg          drain_start;
    reg  [15:0]  drain_n;
    wire         drain_busy, drain_valid;
    wire [255:0] drain_data;
    wire [15:0]  drain_idx;
    // `drain_valid` is a LEVEL from the drain queue, not a pulse: the write port
    // says when it has taken a sub-tile rather than being assumed ready the
    // instant one appears, which lets the sequencer run ahead of it.
    wire         drain_take = drain_valid && !w_full;

    mx_cluster_node #(.TILES(TILES), .GA(GA), .GB(GB),
                      .ACC_MW(ACC_MW), .MODEL(MODEL), .L1_PRIM(L1_PRIM)) u_node (
        .clk(clk), .rst(!resetn),
        .l1_we(l1_we), .l1_sel(l1_sel), .l1_addr(l1_addr), .l1_data(l1_data),
        .gemm_start(gemm_start), .gemm_gm(gm_r), .gemm_gn(gn_r),
        .gemm_nk(nk_r), .gemm_anchor(anch_r), .gemm_acc(acc_r),
        .gemm_aoff(aoff_r), .gemm_boff(boff_r), .gemm_emit(emit_r),
        .gemm_busy(gemm_busy), .sweep_busy(sweep_busy),
        .drain_start(drain_start), .drain_n(drain_n), .drain_fused(fuse_r),
        .drain_busy(drain_busy),
        .drain_data(drain_data), .drain_idx(drain_idx), .drain_valid(drain_valid),
        .drain_take(drain_take)
    );

    // ================================================ fill / sequencer
    localparam [3:0] S_IDLE = 4'd0, S_FILL = 4'd1,
                     S_GEMM = 4'd3, S_GWAIT = 4'd4,
                     S_DRAIN = 4'd5, S_DWAIT = 4'd6, S_DONE = 4'd7;

    // No PREFETCH parameter: a descriptor removes the requester rather than
    // pipelining it. One flit names the whole run and MAG streams it, so there
    // is nothing left to run ahead. The receive FIFO bounds how far MAG may run
    // ahead of the receiver, as backpressure rather than as a guessed constant.

    reg [3:0]  st;
    reg [33:0] base_r;
    // req_ent counts entries REQUESTED, rcv_ent entries COMPLETED; neither is
    // an L1 address, the flit carries that. `n_r` is 16 bits because DRAIN
    // counts sub-tiles and the resident tile holds 512; FILL's own count fits 8
    // (gn*nk = 128 at the largest tile), so the entry cursors stay narrow.
    reg [15:0] n_r;
    reg [7:0]  req_ent, rcv_ent;
    // Which entry `l1_data` is accumulating. One register is enough only
    // because a single MAG finishes an entry's four words before starting the
    // next -- a property of the SERVER, asserted below rather than assumed.
    reg [7:0]  asm_ent;
    reg [15:0] nfill, ngemm, ndrain;

    // ---- shared fetch: who asks, and who just listens --------------------
    reg [23:0] peer_r;
    reg [1:0]  npeer_r;
    reg        preq_r;
    reg [7:0]  eoff_r;
    localparam [7:0] MY_NODE = {MGR_Y[3:0], MGR_X[3:0]};

    // The LOWEST node index in the sharing set issues the descriptor; the rest
    // issue nothing and receive. Every cluster in the set is handed the same
    // set by the driver, so all of them reach the same answer independently --
    // no negotiation, no election, and no extra bit in the instruction saying
    // who leads.
    wire p0_ok = (npeer_r >= 2'd1) ? (MY_NODE < peer_r[7:0])   : 1'b1;
    wire p1_ok = (npeer_r >= 2'd2) ? (MY_NODE < peer_r[15:8])  : 1'b1;
    wire p2_ok = (npeer_r >= 2'd3) ? (MY_NODE < peer_r[23:16]) : 1'b1;
    wire lead  = p0_ok && p1_ok && p2_ok;

    assign fills_done  = nfill;
    assign gemms_done  = ngemm;
    assign drains_done = ndrain;

    // Software never sees MXFP7: an operand is FP16 in memory and memory
    // quantises it on the way out, or it was quantised on the way IN and memory
    // streams it. `preq` says which, per FILL, so both operands of one GEMM may
    // differ. Either way an entry returns as 4 operand flits, and the entry size
    // lives ONLY in MAG, the module that walks the addresses.
    //   flags[4] QUANT, flags[5] BLAYOUT, flags[6] STREAM

    // ONE FLIT FOR THE WHOLE RUN. The txn field carries the index of the FIRST
    // entry -- `eoff` -- and MAG adds each entry's position in the run, so every
    // response names its exact L1 slot and the receiver needs no cursor of its
    // own. flags[6] STREAM marks this a descriptor rather than a single fetch,
    // and `cnt` is how many consecutive entries it covers.
    function [FLIT_WIDTH-1:0] rd_req;
        input [33:0] adr;
        input        blay;
        input        quant;     // 0 = the operand is already MXFP7
        input [7:0]  ent;
        input [7:0]  cnt;
        input [23:0] peers;     // extra destinations, {y,x} each
        input [1:0]  nd;        // how many of them are present
        begin
            rd_req = { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                       MGR_X[POS_WIDTH-1:0], MGR_Y[POS_WIDTH-1:0],
                       T_MEM_RD_REQ, ent, 1'b1, 3'b000,
                       adr, 6'd0, 8'd3, {1'b0, 1'b1, blay, quant, 4'b0000},
                       cnt, peers, nd, 166'd0 };
        end
    endfunction

    integer bi, bk, bj;
    always @(posedge clk) begin
        if (!resetn) begin
            st <= S_IDLE; asm_ent <= 8'd0; req_ent <= 8'd0; rcv_ent <= 8'd0;
            inst_ready <= 1'b0; recv_ready <= 1'b0;
            exec_done <= 1'b0; exec_result <= 32'd0;
            send_valid <= 1'b0; send_flit <= {FLIT_WIDTH{1'b0}};
            l1_we <= 1'b0; l1_sel <= 1'b0; l1_addr <= 16'd0; l1_data <= 928'd0;
            gemm_start <= 1'b0; drain_start <= 1'b0; drain_n <= 16'd0;
            gm_r <= 8'd1; gn_r <= 8'd1; nk_r <= 8'd1; anch_r <= 8'd0;
            // matches gm_r = gn_r = 1, i.e. one sub-tile: NOT wide
            wide_r <= 1'b0;
            aoff_r <= 8'd0; boff_r <= 8'd0; eoff_r <= 8'd0;
            acc_r <= 1'b0; emit_r <= 1'b0; fuse_r <= 1'b0;
            base_r <= 34'd0; n_r <= 16'd1;
            peer_r <= 24'd0; npeer_r <= 2'd0; preq_r <= 1'b0;
            nfill <= 16'd0; ngemm <= 16'd0; ndrain <= 16'd0;
        end else begin
            inst_ready  <= 1'b0;
            exec_done   <= 1'b0;
            l1_we       <= 1'b0;
            gemm_start  <= 1'b0;
            drain_start <= 1'b0;
            // Accept a response only when there is somewhere to put it. Held
            // high it pops the receive FIFO and DISCARDS anything arriving
            // outside a FILL, which matters as soon as a fetch is shared: one
            // CU's responses can arrive before another has decoded its own FILL.
            recv_ready  <= (st == S_FILL);

            if (send_valid && send_ready) send_valid <= 1'b0;

            case (st)
            S_IDLE: if (inst_valid && !inst_ready) begin
                base_r <= i_addr;
                n_r    <= (i_n == 16'd0) ? 16'd1 : i_n;
                l1_sel <= i_sel;
                gm_r   <= i_gm; gn_r <= i_gn; nk_r <= i_nk; anch_r <= i_anch;
                wide_r <= i_wide;
                acc_r  <= i_acc;
                aoff_r <= i_aoff; boff_r <= i_boff; eoff_r <= i_eoff;
                emit_r <= i_emit; fuse_r <= i_fuse;
                peer_r <= i_peer; npeer_r <= i_npeer; preq_r <= i_preq;
                inst_ready <= 1'b1;
                req_ent <= 8'd0; rcv_ent <= 8'd0;
                case (i_op)
                    OP_FILL:  st <= S_FILL;
                    OP_GEMM:  st <= S_GEMM;
                    OP_DRAIN: st <= S_DRAIN;
                    default:  st <= S_DONE;
                endcase
            end

            // ---- FILL: one descriptor, then pure reception ---------------
            // The CU states the run once and then only places the words that
            // come back. No requester on the critical loop, so the round trip is
            // paid once per FILL rather than once per entry and everything after
            // the first entry arrives at MAG's service rate -- a throughput, not
            // a latency.
            S_FILL: begin
                // The descriptor goes out ONCE, only from the leader of the
                // sharing set. A follower sends nothing: its operands are
                // already on the way. `req_ent` marks the decision either way.
                if (!send_valid && (req_ent == 8'd0)) begin
                    if (lead) begin
                        send_flit  <= rd_req(base_r, l1_sel, !preq_r, eoff_r,
                                             n_r[7:0], peer_r, npeer_r);
                        send_valid <= 1'b1;
                    end
                    req_ent <= 8'd1;
                end
                if ({8'd0, rcv_ent} == n_r) begin
                    nfill <= nfill + 16'd1;
                    st <= S_DONE;
                end

                // receiver: assemble one entry from the 4 words it is told it
                // is receiving. `rword` and `rtag` come off the flit, so this
                // block holds no cursor and cannot desynchronise from MAG.
                if (recv_valid && recv_ready && rtype == T_MEM_RD_RESP) begin
                // word `rword` carries K-slice rword: elements 8*rw .. 8*rw+7.
                // Unrolled over rword rather than indexed by it -- a variable
                // part-select write builds a barrel mux across all 928 bits.
                for (bk = 0; bk < 8; bk = bk + 1) begin
                    if (!l1_sel) begin
                        for (bi = 0; bi < 4; bi = bi + 1) begin
                            if (rword == 2'd0) l1_data[(bi*32 + 0 + bk)*7 +: 7] <= recv_flit[255 - (bi*8+bk)*7 -: 7];
                            if (rword == 2'd1) l1_data[(bi*32 + 8 + bk)*7 +: 7] <= recv_flit[255 - (bi*8+bk)*7 -: 7];
                            if (rword == 2'd2) l1_data[(bi*32 +16 + bk)*7 +: 7] <= recv_flit[255 - (bi*8+bk)*7 -: 7];
                            if (rword == 2'd3) l1_data[(bi*32 +24 + bk)*7 +: 7] <= recv_flit[255 - (bi*8+bk)*7 -: 7];
                        end
                    end else begin
                        for (bj = 0; bj < 4; bj = bj + 1) begin
                            if (rword == 2'd0) l1_data[(( 0+bk)*4 + bj)*7 +: 7] <= recv_flit[255 - (bk*4+bj)*7 -: 7];
                            if (rword == 2'd1) l1_data[(( 8+bk)*4 + bj)*7 +: 7] <= recv_flit[255 - (bk*4+bj)*7 -: 7];
                            if (rword == 2'd2) l1_data[((16+bk)*4 + bj)*7 +: 7] <= recv_flit[255 - (bk*4+bj)*7 -: 7];
                            if (rword == 2'd3) l1_data[((24+bk)*4 + bj)*7 +: 7] <= recv_flit[255 - (bk*4+bj)*7 -: 7];
                        end
                    end
                end
                // scales ride in every word; take them from the first
                if (rword == 2'd0) begin
                    asm_ent <= rtag;
                    for (bi = 0; bi < 4; bi = bi + 1)
                        l1_data[896 + bi*8 +: 8] <= recv_flit[31 - bi*8 -: 8];
                end

                if (rword == 2'd3) begin
                    // last word of this entry: commit it to the slot the flit
                    // named. `rcv_ent` is now a COUNT of entries completed --
                    // it bounds the requester and ends the FILL; it is no
                    // longer the L1 address.
                    l1_we   <= 1'b1;
                    l1_addr <= {8'd0, rtag};
                    rcv_ent <= rcv_ent + 8'd1;
                end
                end
            end

            // ---- GEMM -------------------------------------------------
            // RETIRED ON ISSUE, not on completion. The sweep runs in the manager
            // and needs nothing from this sequencer once started, so holding the
            // instruction here only stops the CU from filling the other half of
            // L1 -- which is why L1 has to be addressable, or the fill would
            // land on what the sweep is reading.
            //
            // BACK TO BACK SWEEPS wait for the MANAGER, not for the cascade
            // behind it: sweep i+1's first command addresses tile 0, and sweep i
            // last touched tile 0 `gm*gn` cycles before it stopped issuing, so
            // REUSE_MIN is cleared by a wide margin. Guarded on
            // `gm*gn >= REUSE_MIN` -- below that an address DOES recur inside
            // the window and the conservative `gemm_busy` wait is correct.
            //
            // An EMITTING sweep also waits for the previous tile's results to
            // have left, because it sets `w_base`: starting early would redirect
            // writes still sitting in the buffer.
            S_GEMM: if (!(gemm_wide ? sweep_busy : gemm_busy) &&
                        (!emit_r || !drain_busy)) begin
                gemm_start <= 1'b1;
                st <= S_GWAIT;
            end
            S_GWAIT: if (!gemm_start) begin
                ngemm <= ngemm + 16'd1;
                st <= S_DONE;
            end

            // ---- DRAIN -------------------------------------------------
            // `drain_busy` seizes the accumulator's control mux the cycle it
            // rises, so a sweep still in flight would have its results
            // discarded. A FUSED drain issues nothing -- the sub-tiles came out
            // of the sweep -- and does NOT wait for `gemm_busy`, so one tile's
            // results can drain while the next tile's sweep runs.
            S_DRAIN: if (fuse_r || !gemm_busy) begin
                drain_n     <= n_r;
                drain_start <= 1'b1;
                st <= S_DWAIT;
            end
            // A DRAIN is finished when the last sub-tile's write has LEFT the
            // CU, not when the accumulator stops producing: `drain_busy` falls
            // as soon as the final value reaches the write-back port, which
            // still has a REQ/DATA pair to send. Retiring there runs the round's
            // DONE ahead of the memory traffic it stands for -- benign only
            // while no later round reads what an earlier one wrote, which a
            // K-split reduction (task #12) does.
            S_DWAIT: if (!drain_start && !drain_busy && w_idle) begin
                ndrain <= ndrain + 16'd1;
                st <= S_DONE;
            end

            S_DONE: begin
                exec_done   <= 1'b1;
                exec_result <= {16'd0, nfill + ngemm + ndrain};
                st <= S_IDLE;
            end
            default: st <= S_IDLE;
            endcase
        end
    end

    // ================================================ port 1: result write-back
    // The accumulator's own port. Each drained sub-tile is one 256-bit word:
    // a two-flit MEM_WR_REQ, descriptor then data.
    // A DRAIN'S SUB-TILES ARE ONE BURST: they go to `addr + t*32`, consecutive
    // 256-bit words. This is what bounds the drain -- MAG retires one
    // single-beat write per visit to S_IDLE (~4 cycles, of which axi_ram is 3)
    // while two clusters can produce a pair per cycle between them, so
    // pipelining the drain alone only wedges MAG's input queue. Amortising one
    // transaction over WBURST beats is what makes the rate achievable.
    //
    // WBURST = 1 reduces EXACTLY to one beat per descriptor (len = 0), which is
    // what makes this safe to turn down.
    localparam integer WBURST = 8;
    localparam integer WBW    = (WBURST <= 1) ? 1 : $clog2(WBURST);

    // TWO BANKS. A burst is collected into one while the other is sent, because
    // the two take almost exactly as long -- 8 sub-tiles in, 9 flits out -- so
    // in sequence every burst costs the sum instead of the larger. The sweep
    // pays that: 512 sub-tiles arrive in 512 consecutive cycles at the end of a
    // pass and `emit_stall` holds the array once DQ_LIMIT are outstanding.
    reg [FLIT_WIDTH-1:0] w_flit;
    reg [255:0]          w_buf [0:2*WBURST-1];
    reg                  w_cb, w_sb;   // bank being collected into / sent from
    reg [WBW:0]          w_fill;   // sub-tiles in the collect bank, 0..WBURST
    reg [WBW:0]          w_send;   // which one is going out
    reg [WBW:0]          w_len;    // beats in the burst being sent
    reg [15:0]           w_cfirst; // sub-tile index each bank starts at
    reg [15:0]           w_sfirst;
    reg [33:0]           w_base;

    localparam [1:0] W_IDLE = 2'd0, W_REQ = 2'd1, W_DATA = 2'd2;

    // The burst must be CLOSED when the drain runs dry, not only when it is
    // full, or a tile whose count is not a multiple of WBURST leaves its tail
    // sitting in the buffer forever.
    wire w_flush = (w_fill != 0) &&
                   ((w_fill == WBURST[WBW:0]) || !drain_busy);
    // Hand the collected bank to the sender. On this cycle the collector is
    // necessarily idle -- either the bank is full, or `!drain_busy` says there
    // is nothing left in the queue to take -- so the two never contend for
    // `w_fill`.
    wire w_hand  = (w_st == W_IDLE) && w_flush;
    assign w_full = (w_fill == WBURST[WBW:0]);
    // The output register is free when it is empty OR is being emptied THIS
    // cycle. Reloading only once it reads empty costs a cycle per flit, and a
    // drain is nothing but flits -- half the write port's time goes to waiting
    // for its own register, and the sweep pays for it through `emit_stall`.
    wire w_free = !w_valid || !a_out_busy;
    // The write port is idle only when nothing is buffered AND nothing is on
    // the wire. A DRAIN that retires while a burst is still buffered reports
    // completion ahead of the memory traffic it stands for.
    assign w_idle = (w_st == W_IDLE) && (w_fill == 0) && !w_valid;

    integer wbi;
    always @(posedge clk) begin
        if (!resetn) begin
            w_valid <= 1'b0; w_st <= W_IDLE; w_flit <= {FLIT_WIDTH{1'b0}};
            w_fill <= 0; w_send <= 0; w_len <= 0;
            w_cb <= 1'b0; w_sb <= 1'b0;
            w_cfirst <= 16'd0; w_sfirst <= 16'd0; w_base <= 34'd0;
            for (wbi = 0; wbi < 2*WBURST; wbi = wbi + 1) w_buf[wbi] <= 256'd0;
        end else begin
            if (w_valid && !a_out_busy) w_valid <= 1'b0;
            // The destination comes from whichever instruction produces the
            // sub-tiles. A fused sweep starts emitting long before its DRAIN
            // is decoded, so it carries the address itself.
            if ((st == S_DRAIN) || ((st == S_GEMM) && emit_r)) w_base <= base_r;

            // Collection is not a state any more -- it runs every cycle there
            // is room, against whichever bank the sender does not hold.
            if (drain_valid && !w_full) begin
                w_buf[{w_cb, w_fill[WBW-1:0]}] <= drain_data;
                if (w_fill == 0) w_cfirst <= drain_idx;
                w_fill <= w_fill + 1'b1;
            end else if (w_hand) begin
                w_cb   <= ~w_cb;
                w_fill <= 0;
            end

            case (w_st)
            W_IDLE: if (w_hand) begin
                w_sb     <= w_cb;
                w_len    <= w_fill;
                w_sfirst <= w_cfirst;
                w_send   <= 0;
                w_st     <= W_REQ;
            end
            // One descriptor for the whole run. `len` is beats-minus-one, the
            // field docs/isa/memory.md s2 records as decoded and ignored --
            // it is honoured now, on both sides.
            W_REQ: if (w_free) begin
                w_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                            ACU_X[POS_WIDTH-1:0], ACU_Y[POS_WIDTH-1:0],
                            T_MEM_WR_REQ, 8'h02, 1'b0, 3'b000,
                            w_base + {18'd0, w_sfirst} * 34'd32,
                            6'd0, {{(8-WBW-1){1'b0}}, (w_len - 1'b1)},
                            8'd0, 200'd0 };
                w_valid <= 1'b1;
                w_st    <= W_DATA;
            end
            W_DATA: if (w_free) begin
                w_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                            ACU_X[POS_WIDTH-1:0], ACU_Y[POS_WIDTH-1:0],
                            T_MEM_WR_DATA, 8'h02,
                            (w_send + 1'b1 == w_len), 3'b000,
                            w_buf[{w_sb, w_send[WBW-1:0]}] };
                w_valid <= 1'b1;
                if (w_send + 1'b1 == w_len) w_st <= W_IDLE;
                else                        w_send <= w_send + 1'b1;
            end
            default: w_st <= W_IDLE;
            endcase
        end
    end

    assign a_out_data  = w_flit;
    assign a_out_valid = w_valid;
    assign a_in_busy   = 1'b0;       // acknowledgements are discarded

`ifndef SYNTHESIS
    // An x in a memory address is invisible downstream and fatal: memory returns
    // x, the quantiser packs it, the accumulator sums it, and the drained tile
    // is a plausible-looking zero -- so the symptom points at the datapath.
    // It comes from address arithmetic (an out-of-range part-select, an unsized
    // constant widened past 32 bits), both of which elaborate cleanly. Checked
    // at the producer so the message names the module that built it.
    always @(posedge clk) begin
        if (resetn && send_valid && (^send_flit[255 -: 34] === 1'bx))
            $display("%0t ERROR mx_cluster_cu: read request address is x", $time);
        if (resetn && w_valid && (w_st == 2'd2) && (^w_flit[255 -: 34] === 1'bx))
            $display("%0t ERROR mx_cluster_cu: write request address is x", $time);
        // ONE assembly register, sufficient only because a single MAG delivers
        // an entry's four words consecutively. A second server -- multicast, a
        // second MAG, a reordering fetch engine -- would interleave two entries
        // into one and produce a plausible wrong tile.
        if (resetn && (st == S_FILL) && recv_valid && recv_ready &&
            (rtype == T_MEM_RD_RESP) && (rword != 2'd0) && (rtag !== asm_ent))
            $display("%0t ERROR mx_cluster_cu: response for entry %0d word %0d arrived while assembling entry %0d",
                     $time, rtag, rword, asm_ent);
    end
`endif

endmodule

`default_nettype wire

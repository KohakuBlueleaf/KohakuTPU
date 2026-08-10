// A cluster as a ONE-PORT NoC compute unit.
//
//   NoC <-> manager <-> tcu -> tcu -> tcu -> tcu -> acu
//            L1            direct DSP cascade        resident tile
//
// ONE port at (CU_X, CU_Y): instructions and fill responses in, fetch
// descriptors, drain writes and completion signals out. The accumulator had a
// node of its own and it bought no bandwidth -- the link is full duplex, and
// the two endpoints loaded opposite directions of it. What it cost was a router
// local: eight clusters at two locals each force a 4x4 mesh where one local
// each fits 2x4, eight NoCRouter instances fewer at 3,281 LUT apiece.
//
// Inbound is demuxed BY TYPE, the way mag.v shares its memory ports. Outbound,
// the fetch descriptor and the drain share one queue.
//
// The chain eats eight 256-bit operand words per cycle and a port delivers one,
// so no port count closes an 8x deficit -- REUSE does: a Gm x Gn sub-tile block
// needs 4(Gm+Gn)/(Gm*Gn) words per cycle, 0.375 at 16x32. See
// docs/compute/tensor-isa.md s1.
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
//   [114]     abank   GEMM: which 256-entry half of L1 A this sweep reads
//   [113]     bbank   GEMM: ... and of L1 B
//   [112]     fbank   FILL: which half this fill writes
//   [111]     dnode   DRAIN: send to a NoC node instead of to memory
//   [110:107] dst_x   DRAIN: the node
//   [106:103] dst_y
//   [102:95]  dbuf    DRAIN: the destination's buffer id
//   [94:87]   dflags  DRAIN: copied into the descriptor's flags byte
//   [86:83]   dack_y  DRAIN: where the receiver sends its completion,
//   [82:79]   dack_x         0 meaning back to this cluster
//
// One L1 entry is four consecutive 256-bit operand words -- the four K-slices
// of one row group (A) or column group (B) -- so FILL asks for entries and
// assembles each set of four into the 928-bit entry the sweep consumes.

`default_nettype none

module mx_cluster_cu #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer CU_X       = 0,      // the cluster's one NoC coordinate
    parameter integer CU_Y       = 0,
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
    parameter         L1_PRIM    = "distributed",
    // The receive queue is 288 x RECV_DEPTH and the largest single item in
    // noc_cu_base. Block RAM by default; a knob so the trade can be measured.
    parameter         RECV_MEM   = "block"
)(
    input  wire                   clk,
    input  wire                   resetn,

    // ---- the cluster's NoC local ----
    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,

    output wire [15:0]            fills_done,
    output wire [15:0]            gemms_done,
    output wire [15:0]            drains_done
);
    localparam [3:0] T_MEM_RD_REQ  = 4'h0,
                     T_MEM_WR_REQ  = 4'h1,
                     T_MEM_RD_RESP = 4'h2,
                     T_MEM_WR_ACK  = 4'h3,
                     // A write is a descriptor followed by its data, and the
                     // mesh interleaves streams -- another node's flit can land
                     // between the two. A data flit therefore has to be
                     // identifiable BY TYPE rather than by position, or memory
                     // pairs it with whichever write happens to be open and
                     // silently stores the wrong bytes.
                     T_MEM_WR_DATA = 4'h4,
                     // 0x8, NOT the 0x4 docs/noc/spec.md names: that is
                     // T_MEM_WR_DATA, and a CU_DATA flit reaching MAG would
                     // enter the write queue as data.
                     T_CU_DATA     = 4'h8,
                     T_CU_SIGNAL   = 4'h6;

    // A received burst reports itself, when its descriptor's flags ask. The
    // sender is otherwise waiting on nothing: noc_cu_base signals on
    // instruction retirement and a burst is not an instruction, so without
    // this a unit that sends and waits would block for ever.
    localparam [7:0]  SIG_DATA_RECEIVED = 8'h03;
    localparam integer PAD_SIG = FLIT_WIDTH - 4*POS_WIDTH - 16 - 40;

    localparam [3:0] OP_FILL = 4'd1, OP_GEMM = 4'd2, OP_DRAIN = 4'd3;

    // Which buffer of this CU a CU_DATA stream is addressed to. 0 and 1 match
    // `l1_sel`; 2 is the accumulator's resident tile, reached through
    // OP_ADD_PEER. See docs/isa/cluster.md s9.
    localparam [7:0] BUF_L1A = 8'd0, BUF_L1B = 8'd1, BUF_PEER = 8'd2;
    // One peer sub-tile in the accumulator's internal float, 22 bits x 16 lanes.
    localparam integer PW = 16*(ACC_MW+8);

    // ================================================ framework
    wire [FLIT_WIDTH-1:0] inst_flit, recv_flit;
    wire                  inst_valid, recv_valid, send_ready;
    reg                   inst_ready;
    // COMBINATIONAL, and dispatched by flit type below: a memory response only
    // makes sense inside a FILL, but CU_DATA is unsolicited by definition and
    // arrives whenever its sender chose to send it. Registered on the state
    // alone, it accepted a CU_DATA flit during a FILL and discarded it.
    wire                  recv_ready;
    reg                   exec_done;
    reg  [31:0]           exec_result;
    reg  [FLIT_WIDTH-1:0] send_flit;
    reg                   send_valid;
    reg  [FLIT_WIDTH-1:0] sg_flit;
    reg                   sg_valid;
    // Declared here because noc_cu_base reads it below; set by the CU_DATA
    // receiver much further down.
    reg                   fault_r;

    // ---- inbound demux by TYPE, as mag.v does on its shared memory ports ---
    // A MEM_WR_ACK is ACCEPTED AND DROPPED here. Nothing consumes it, so queued
    // it fills the receive FIFO, raises `noc_in_busy` for good, and wedges the
    // instructions behind it. It was `a_in_busy = 1'b0` on a port of its own.
    wire [3:0] in_ty  = noc_in_data[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    wire       in_ack = (in_ty == T_MEM_WR_ACK);
    wire       base_in_busy;

    wire [FLIT_WIDTH-1:0] tx_flit;
    wire                  tx_valid, tx_ready;
    wire                  sg_take = sg_valid && tx_ready;

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(CU_X), .POS_Y(CU_Y),
        // Mesh-wide build number -- see vec_cu.v. 0x03 is the CU_DATA
        // interconnect: unit-to-unit transfer on both halves of the mesh.
        .CU_TYPE(16'h4D47), .CU_VERSION(8'h03), .N_BUFFERS(2),
        .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH),
        .RECV_MEM(RECV_MEM)
    ) u_base (
        .clk(clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid && !in_ack),
        .noc_in_busy(base_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(noc_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(exec_result), .exec_fault(fault_r),
        .dbg_ctr({ctr_comp, ctr_fill}),
        .send_flit(tx_flit), .send_valid(tx_valid), .send_ready(tx_ready),
        .recv_flit(recv_flit), .recv_valid(recv_valid), .recv_ready(recv_ready),
        .inst_space(), .busy()
    );

    assign noc_in_busy = in_ack ? 1'b0 : base_in_busy;

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
    // L1 IS TWO BANKS of 256 entries. `aoff`/`boff`/`eoff` stay 8 bits and a
    // bank bit picks the half, rather than widening them and moving every field
    // below -- and 0 is the lower half, so an instruction written before these
    // existed addresses exactly what it did. Fill one bank while the other is
    // swept: the double buffering docs/isa/cluster.md s4.6 does by address
    // arithmetic today, with twice the L1 behind it.
    wire        i_abank = inst_flit[114];
    wire        i_bbank = inst_flit[113];
    wire        i_fbank = inst_flit[112];
    // WHERE A DRAIN'S RESULTS GO. `dnode` = 0 is the memory port, which is what
    // an all-zero tail means, so a DRAIN written before these bits existed
    // still writes to memory. `addr` keeps its meaning either way -- a byte
    // address -- and a node destination sends addr[20:5] as the descriptor's
    // granule offset, which is the same arithmetic the memory path does when
    // it puts sub-tile t at addr + t*32.
    wire        i_dnode = inst_flit[111];
    wire [3:0]  i_dstx  = inst_flit[110 -: 4];
    wire [3:0]  i_dsty  = inst_flit[106 -: 4];
    wire [7:0]  i_dbuf  = inst_flit[102 -: 8];
    wire [7:0]  i_dflag = inst_flit[94 -: 8];
    // Its own field rather than borrowed bits of `dflags`, which is copied into
    // the descriptor's flags byte verbatim and has to stay that.
    wire [3:0]  i_dacky = inst_flit[86 -: 4];
    wire [3:0]  i_dackx = inst_flit[82 -: 4];

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
    // The FILL's own side. `l1_sel` is no longer this: a CU_DATA stream names
    // its side per stream, so the write port's side is registered with the
    // entry that commits it rather than latched from the instruction.
    reg          sel_r;
    reg          gemm_start;
    reg  [7:0]   gm_r, gn_r, nk_r, anch_r, aoff_r, boff_r;
    reg          abank_r, bbank_r, fbank_r;
    reg          dnode_r;
    reg  [3:0]   dstx_r, dsty_r;
    reg  [7:0]   dbuf_r, dflag_r;
    reg  [3:0]   dackx_r, dacky_r;
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

    // ---- peer transfer, driven by the CU_DATA receiver below -------------
    wire          peer_ready;
    reg           pk_pend;
    reg  [15:0]   pk_idx;
    reg  [255:0]  pk_lo;
    reg  [PW-257:0] pk_hi;
    wire          peer_open;

    mx_cluster_node #(.TILES(TILES), .GA(GA), .GB(GB),
                      .ACC_MW(ACC_MW), .MODEL(MODEL), .L1_PRIM(L1_PRIM)) u_node (
        .clk(clk), .rst(!resetn),
        .l1_we(l1_we), .l1_sel(l1_sel), .l1_addr(l1_addr), .l1_data(l1_data),
        .gemm_start(gemm_start), .gemm_gm(gm_r), .gemm_gn(gn_r),
        .gemm_nk(nk_r), .gemm_anchor(anch_r), .gemm_acc(acc_r),
        .gemm_aoff(aoff_r), .gemm_boff(boff_r),
        .gemm_abank(abank_r), .gemm_bbank(bbank_r), .gemm_emit(emit_r),
        .gemm_busy(gemm_busy), .sweep_busy(sweep_busy),
        .drain_start(drain_start), .drain_n(drain_n), .drain_fused(fuse_r),
        // buf 2 IS the accumulator-float mode; nothing else selects it. A drain
        // to L1 emits FP16 sub-tiles and L1 takes int7+E5M3, so cluster to
        // cluster is buf 2 or it is a type error -- one field, and no second
        // bit that can disagree with it.
        .drain_send(dnode_r && (dbuf_r == BUF_PEER)),
        .drain_busy(drain_busy),
        .drain_data(drain_data), .drain_idx(drain_idx), .drain_valid(drain_valid),
        .drain_take(drain_take),
        .peer_open(peer_open), .peer_cmd(pk_pend), .peer_idx(pk_idx),
        .peer_data({pk_hi, pk_lo}), .peer_ready(peer_ready),
        // The SEND direction is the node-addressed drain of docs/isa/cluster.md
        // s10, which is not built yet.
        .peer_out(), .peer_out_valid()
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
    reg [8:0]  asm_ent;
    reg [15:0] nfill, ngemm, ndrain;

    // ---- shared fetch: who asks, and who just listens --------------------
    reg [23:0] peer_r;
    reg [1:0]  npeer_r;
    reg        preq_r;
    reg [7:0]  eoff_r;
    localparam [7:0] MY_NODE = {CU_Y[3:0], CU_X[3:0]};

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
                       CU_X[POS_WIDTH-1:0], CU_Y[POS_WIDTH-1:0],
                       T_MEM_RD_REQ, ent, 1'b1, 3'b000,
                       adr, 6'd0, 8'd3, {1'b0, 1'b1, blay, quant, 4'b0000},
                       cnt, peers, nd, 166'd0 };
        end
    endfunction

    // Memory against compute, so a gap in the total has a cause rather than a
    // size. S_FILL is waiting on operands; gemm/sweep is the array running.
    reg [31:0] ctr_fill, ctr_comp;
    always @(posedge clk) begin
        if (!resetn) begin
            ctr_fill <= 32'd0; ctr_comp <= 32'd0;
        end else begin
            if (st == S_FILL)            ctr_fill <= ctr_fill + 32'd1;
            if (gemm_busy || sweep_busy) ctr_comp <= ctr_comp + 32'd1;
        end
    end

    // ================================================ CU_DATA reception
    // A stream is one descriptor flit then `len+1` pure data flits, the last of
    // them carrying the header's `last` bit. `off` is the index of a flit's
    // 32-byte granule within the named buffer and advances by one per data
    // flit, so a stream is a run and nothing needs a cursor per buffer.
    //
    //   [255:248] buf   [247:232] off   [231:224] len   [223:216] flags
    //
    // ONE STREAM AT A TIME, per docs/isa/cluster.md s9: the mesh interleaves,
    // so two senders into one CU would splice their data flits together. The
    // check below fires if it happens.
    wire [7:0]  cd_dbuf = recv_flit[255 -: 8];
    wire [15:0] cd_doff = recv_flit[247 -: 16];
    wire [7:0]  cd_dlen = recv_flit[231 -: 8];

    wire [7:0]  cd_dflg  = recv_flit[223 -: 8];
    // WHERE THE ACK GOES, and zero means the descriptor's source. A CU->CU
    // transfer's completion is otherwise unobservable: the ack reaches the
    // SENDING unit, and no CU consumes one, so nothing can sequence a reader
    // behind a writer. (0,0) is a safe sentinel because gen_mesh.py requires
    // the mesh corners empty, so no endpoint can live there.
    wire [3:0]  cd_dacky = recv_flit[215 -: 4];
    wire [3:0]  cd_dackx = recv_flit[211 -: 4];

    reg         cd_run;
    reg  [7:0]  cd_buf;
    reg  [15:0] cd_off;
    reg  [8:0]  cd_left;
    // The descriptor's sender and its flags, for the completion signal.
    reg  [POS_WIDTH-1:0] cd_sx, cd_sy;
    // SEPARATE REGISTERS, not the sender reused. They hold the same value
    // whenever the ack is not redirected, which is exactly why sharing one pair
    // survives every test except the one case the field exists for.
    reg  [POS_WIDTH-1:0] cd_ax, cd_ay;
    reg  [7:0]  cd_txn, cd_flg;
    // REJECTED, NOT WRAPPED. `off` is 16 bits against an L1 of GA entries, so
    // an over-range burst would silently overwrite the bottom of the buffer.
    // The stream is still COUNTED OUT -- otherwise the next data flit is read
    // as a descriptor and the damage spreads -- and only the writing is
    // suppressed.
    reg         cd_bad;

    wire rx_resp = (rtype == T_MEM_RD_RESP);
    wire rx_data = (rtype == T_CU_DATA);

    // The last granule the burst touches, against what the named buffer holds.
    // Sized explicitly: an unsized `GA*4` is a 32-bit signed integer and the
    // comparison's signedness would depend on it.
    localparam [16:0] LIM_L1A = GA*4, LIM_L1B = GB*4, LIM_PEER = TILES*2;
    wire [16:0] cd_end = {1'b0, cd_doff} + {9'd0, cd_dlen};
    wire        cd_fits =
        (cd_dbuf == BUF_L1A)  ? (cd_end < LIM_L1A)
      : (cd_dbuf == BUF_L1B)  ? (cd_end < LIM_L1B)
      : (cd_dbuf == BUF_PEER) ? (cd_end < LIM_PEER) && !cd_doff[0]
                              : 1'b0;

    // Nothing is taken while a peer sub-tile is pending: `pk_lo`/`pk_hi` ARE the
    // register the node reads, so the next flit of any stream would overwrite a
    // sub-tile the accumulator has not been given yet. Nor while a completion
    // signal is unsent, so a second burst cannot overwrite the one it reports.
    // A flit of a type this CU does not know is accepted and dropped, the way
    // the port does with MEM_WR_ACK -- held, it would fill the receive FIFO and
    // wedge the CU.
    wire cd_take   = !pk_pend && !sg_valid;
    assign recv_ready = rx_resp ? (st == S_FILL)
                      : rx_data ? cd_take
                                : 1'b1;
    wire cd_pop = recv_valid && recv_ready && rx_data;

    // Held for the whole stream: the node waits for the sweep at its head only.
    //
    // REGISTERED, because it gates the sequencer: combinationally it put an
    // 8-bit compare behind `drain_busy` on the path d_out -> the state
    // machine's clock enable, which became the worst path in the CU at 324 MHz
    // against 345 before. A cycle of latency is safe in both directions -- late
    // to rise only delays a peer command, which `p_open` already holds behind
    // `gemm_busy`, and late to fall only delays a sweep.
    reg peer_open_r;
    assign peer_open = peer_open_r;
    always @(posedge clk) begin
        if (!resetn) peer_open_r <= 1'b0;
        else         peer_open_r <= (cd_run && (cd_buf == BUF_PEER)) || pk_pend;
    end

    always @(posedge clk) begin
        if (!resetn) begin
            cd_run <= 1'b0; cd_buf <= 8'd0; cd_off <= 16'd0; cd_left <= 9'd0;
            cd_sx <= 0; cd_sy <= 0; cd_ax <= 0; cd_ay <= 0;
            cd_txn <= 8'd0; cd_flg <= 8'd0;
            cd_bad <= 1'b0; fault_r <= 1'b0;
            pk_pend <= 1'b0; pk_idx <= 16'd0;
            pk_lo <= 256'd0; pk_hi <= {(PW-256){1'b0}};
            sg_valid <= 1'b0; sg_flit <= {FLIT_WIDTH{1'b0}};
        end else begin
            if (pk_pend && peer_ready) pk_pend <= 1'b0;
            if (sg_valid && sg_take)   sg_valid <= 1'b0;
            // Reported once, at the next instruction boundary, and then cleared:
            // a malformed burst is one fault, not a CU that faults for ever.
            if (exec_done) fault_r <= 1'b0;

            if (cd_pop && !cd_run) begin
                cd_run  <= 1'b1;
                cd_buf  <= cd_dbuf;
                cd_off  <= cd_doff;
                cd_left <= {1'b0, cd_dlen} + 9'd1;
                cd_sx   <= recv_flit[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH];
                cd_sy   <= recv_flit[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH];
                if ({cd_dacky, cd_dackx} == 8'd0) begin
                    cd_ax <= recv_flit[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH];
                    cd_ay <= recv_flit[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH];
                end else begin
                    cd_ax <= cd_dackx[POS_WIDTH-1:0];
                    cd_ay <= cd_dacky[POS_WIDTH-1:0];
                end
                cd_txn  <= recv_flit[FLIT_WIDTH-4*POS_WIDTH-5 -: 8];
                cd_flg  <= cd_dflg;
                cd_bad  <= !cd_fits;
                if (!cd_fits) fault_r <= 1'b1;
            end else if (cd_pop) begin
                cd_off  <= cd_off + 16'd1;
                cd_left <= cd_left - 9'd1;
                if (cd_left == 9'd1) begin
                    cd_run <= 1'b0;
                    // The burst is complete either way -- a rejected one is
                    // still acknowledged, or the sender waits for ever on a
                    // signal that says nothing arrived.
                    if (cd_flg[0]) begin
                        sg_flit <= { cd_ax, cd_ay,
                                     CU_X[POS_WIDTH-1:0], CU_Y[POS_WIDTH-1:0],
                                     T_CU_SIGNAL, cd_txn, 1'b1, 3'b000,
                                     SIG_DATA_RECEIVED, {24'd0, cd_buf},
                                     {PAD_SIG{1'b0}} };
                        sg_valid <= 1'b1;
                    end
                end
                // A sub-tile is PW bits and a payload is 256, so granule 2t
                // carries its low half and 2t+1 the rest in the payload's low
                // bits. The pair completes on the odd granule.
                if ((cd_buf == BUF_PEER) && !cd_bad) begin
                    if (!cd_off[0]) pk_lo <= recv_flit[255:0];
                    else begin
                        pk_hi   <= recv_flit[PW-257:0];
                        pk_idx  <= {1'b0, cd_off[15:1]};
                        pk_pend <= 1'b1;
                    end
                end
            end
        end
    end

    // ================================================ L1 placement
    // ONE BLOCK, TWO SOURCES: MAG's responses during a FILL, and a CU_DATA
    // stream from another unit. Both deliver an entry as four 256-bit words
    // tagged {entry, word} and differ only in where the tag comes from, so
    // they share the 928-bit permutation instead of building it twice.
    //
    // The two are mutually exclusive by flit type. `pl_ent` is 9 bits, one bank
    // and one 8-bit offset: a FILL takes the bank from its own instruction bit,
    // while a CU_DATA stream needs no field for it at all -- `off` counts
    // granules through the whole 512-entry buffer, so the bank is just where the
    // entry index runs off the end of a byte.
    wire        pl_data  = cd_pop && cd_run && !cd_bad && (cd_buf <= BUF_L1B);
    wire        pl_fill  = recv_valid && recv_ready && rx_resp && (st == S_FILL);
    wire        pl_valid = pl_fill || pl_data;
    wire        pl_sel   = pl_data ? cd_buf[0]    : sel_r;
    wire [8:0]  pl_ent   = pl_data ? cd_off[10:2] : {fbank_r, rtag};
    wire [1:0]  pl_word  = pl_data ? cd_off[1:0]  : rword;

    integer bi, bk, bj;
    always @(posedge clk) begin
        if (!resetn) begin
            l1_we <= 1'b0; l1_sel <= 1'b0; l1_addr <= 16'd0; l1_data <= 928'd0;
            asm_ent <= 9'd0;
        end else begin
            l1_we <= 1'b0;
            if (pl_valid) begin
            // word `pl_word` carries K-slice pl_word: elements 8*rw .. 8*rw+7.
            // Unrolled over it rather than indexed by it -- a variable
            // part-select write builds a barrel mux across all 928 bits.
            for (bk = 0; bk < 8; bk = bk + 1) begin
                if (!pl_sel) begin
                    for (bi = 0; bi < 4; bi = bi + 1) begin
                        if (pl_word == 2'd0) l1_data[(bi*32 + 0 + bk)*7 +: 7] <= recv_flit[255 - (bi*8+bk)*7 -: 7];
                        if (pl_word == 2'd1) l1_data[(bi*32 + 8 + bk)*7 +: 7] <= recv_flit[255 - (bi*8+bk)*7 -: 7];
                        if (pl_word == 2'd2) l1_data[(bi*32 +16 + bk)*7 +: 7] <= recv_flit[255 - (bi*8+bk)*7 -: 7];
                        if (pl_word == 2'd3) l1_data[(bi*32 +24 + bk)*7 +: 7] <= recv_flit[255 - (bi*8+bk)*7 -: 7];
                    end
                end else begin
                    for (bj = 0; bj < 4; bj = bj + 1) begin
                        if (pl_word == 2'd0) l1_data[(( 0+bk)*4 + bj)*7 +: 7] <= recv_flit[255 - (bk*4+bj)*7 -: 7];
                        if (pl_word == 2'd1) l1_data[(( 8+bk)*4 + bj)*7 +: 7] <= recv_flit[255 - (bk*4+bj)*7 -: 7];
                        if (pl_word == 2'd2) l1_data[((16+bk)*4 + bj)*7 +: 7] <= recv_flit[255 - (bk*4+bj)*7 -: 7];
                        if (pl_word == 2'd3) l1_data[((24+bk)*4 + bj)*7 +: 7] <= recv_flit[255 - (bk*4+bj)*7 -: 7];
                    end
                end
            end
            // scales ride in every word; take them from the first
            if (pl_word == 2'd0) begin
                asm_ent <= pl_ent;
                for (bi = 0; bi < 4; bi = bi + 1)
                    l1_data[896 + bi*8 +: 8] <= recv_flit[31 - bi*8 -: 8];
            end
            // Last word of this entry: commit it to the slot the flit named.
            // `l1_sel` is registered HERE, with the address, because `l1_we`
            // is a cycle behind the word that raised it.
            if (pl_word == 2'd3) begin
                l1_we   <= 1'b1;
                l1_addr <= {7'd0, pl_ent};
                l1_sel  <= pl_sel;
            end
            end
        end
    end

    always @(posedge clk) begin
        if (!resetn) begin
            st <= S_IDLE; req_ent <= 8'd0; rcv_ent <= 8'd0;
            inst_ready <= 1'b0;
            exec_done <= 1'b0; exec_result <= 32'd0;
            send_valid <= 1'b0; send_flit <= {FLIT_WIDTH{1'b0}};
            sel_r <= 1'b0;
            abank_r <= 1'b0; bbank_r <= 1'b0; fbank_r <= 1'b0;
            dnode_r <= 1'b0; dstx_r <= 4'd0; dsty_r <= 4'd0;
            dbuf_r <= 8'd0; dflag_r <= 8'd0;
            dackx_r <= 4'd0; dacky_r <= 4'd0;
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
            gemm_start  <= 1'b0;
            drain_start <= 1'b0;

            if (send_valid && send_ready) send_valid <= 1'b0;

            case (st)
            S_IDLE: if (inst_valid && !inst_ready) begin
                base_r <= i_addr;
                n_r    <= (i_n == 16'd0) ? 16'd1 : i_n;
                sel_r  <= i_sel;
                gm_r   <= i_gm; gn_r <= i_gn; nk_r <= i_nk; anch_r <= i_anch;
                wide_r <= i_wide;
                acc_r  <= i_acc;
                aoff_r <= i_aoff; boff_r <= i_boff; eoff_r <= i_eoff;
                abank_r <= i_abank; bbank_r <= i_bbank; fbank_r <= i_fbank;
                dnode_r <= i_dnode; dstx_r <= i_dstx; dsty_r <= i_dsty;
                dbuf_r  <= i_dbuf;  dflag_r <= i_dflag;
                dackx_r <= i_dackx; dacky_r <= i_dacky;
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
                        send_flit  <= rd_req(base_r, sel_r, !preq_r, eoff_r,
                                             n_r[7:0], peer_r, npeer_r);
                        send_valid <= 1'b1;
                    end
                    req_ent <= 8'd1;
                end
                if ({8'd0, rcv_ent} == n_r) begin
                    nfill <= nfill + 16'd1;
                    st <= S_DONE;
                end

                // The words themselves are placed by the L1 block below, which
                // a CU_DATA stream shares. `rcv_ent` is a COUNT of entries
                // completed -- it bounds the requester and ends the FILL; it is
                // not an L1 address, the flit carries that.
                if (pl_fill && (rword == 2'd3)) rcv_ent <= rcv_ent + 8'd1;
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
            //
            // `peer_open` holds a sweep out of a peer stream. The stream waits
            // for `gemm_busy` at its head and then keeps the accumulator's
            // control mux for its whole length, so the two cannot interleave.
            S_GEMM: if (!(gemm_wide ? sweep_busy : gemm_busy) &&
                        (!emit_r || !drain_busy) && !peer_open) begin
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
            S_DRAIN: if ((fuse_r || !gemm_busy) && !peer_open) begin
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

    // ================================================ result write-back
    // Each drained sub-tile is one 256-bit word: a two-flit MEM_WR_REQ,
    // descriptor then data.
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
    reg [15:0]           w_cfirst; // granule index each bank starts at
    reg [15:0]           w_sfirst;
    reg [33:0]           w_base;
    // The destination, mirrored beside `w_base` and for the same reason: a
    // fused sweep starts emitting long before its DRAIN is decoded, so it is
    // the instruction that produced the sub-tiles that says where they go.
    reg                  w_node;
    reg [3:0]            w_dx, w_dy;
    reg [7:0]            w_dbuf, w_dflag;
    reg [3:0]            w_ackx, w_acky;

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
    // ONE OUTBOUND QUEUE, AND THE FETCH DESCRIPTOR WINS. A FILL is a single
    // flit and the sequencer sits in S_FILL until its responses come back, so
    // making it wait costs a whole memory round trip; a drain flit is bulk
    // traffic behind arithmetic that has already finished, and it waits at most
    // one flit because `send_valid` clears on its first grant.
    //
    // THE COMPLETION SIGNAL WINS OVER BOTH. It is one flit, the sender is
    // blocked on it, and the receive path is stalled until it leaves -- so
    // anything queued in front of it is paid twice.
    assign tx_flit  = sg_valid ? sg_flit : send_valid ? send_flit : w_flit;
    assign tx_valid = sg_valid || send_valid || w_valid;
    assign send_ready = tx_ready && !sg_valid;
    wire   w_take   = w_valid && tx_ready && !send_valid && !sg_valid;
    // The output register is free when it is empty OR is being emptied THIS
    // cycle. Reloading only once it reads empty costs a cycle per flit, and a
    // drain is nothing but flits -- half the write port's time goes to waiting
    // for its own register, and the sweep pays for it through `emit_stall`.
    wire w_free = !w_valid || w_take;
    // The write port is idle only when nothing is buffered AND nothing is on
    // the wire. A DRAIN that retires while a burst is still buffered reports
    // completion ahead of the memory traffic it stands for.
    assign w_idle = (w_st == W_IDLE) && (w_fill == 0) && !w_valid;

    // ---- memory or a node: one descriptor, then `w_len` payload words ------
    // The two forms differ in the header and the first flit only. `w_sfirst` is
    // a GRANULE index either way -- a 32-byte word -- so the byte address and
    // the CU_DATA offset are the same number scaled differently, which is what
    // lets one `addr` field serve both.
    wire [POS_WIDTH-1:0] w_ddx = w_node ? w_dx[POS_WIDTH-1:0] : MEM_X[POS_WIDTH-1:0];
    wire [POS_WIDTH-1:0] w_ddy = w_node ? w_dy[POS_WIDTH-1:0] : MEM_Y[POS_WIDTH-1:0];
    wire [3:0]           w_dty = w_node ? T_CU_DATA : T_MEM_WR_REQ;
    wire [3:0]           w_dtd = w_node ? T_CU_DATA : T_MEM_WR_DATA;
    wire [7:0]           w_dlen = {{(8-WBW-1){1'b0}}, (w_len - 1'b1)};
    wire [255:0]         w_desc =
        w_node ? {w_dbuf, w_base[20:5] + w_sfirst, w_dlen, w_dflag,
                  w_acky, w_ackx, 208'd0}
               : {w_base + {18'd0, w_sfirst} * 34'd32, 6'd0, w_dlen, 8'd0, 200'd0};

    integer wbi;
    always @(posedge clk) begin
        if (!resetn) begin
            w_valid <= 1'b0; w_st <= W_IDLE; w_flit <= {FLIT_WIDTH{1'b0}};
            w_fill <= 0; w_send <= 0; w_len <= 0;
            w_cb <= 1'b0; w_sb <= 1'b0;
            w_cfirst <= 16'd0; w_sfirst <= 16'd0; w_base <= 34'd0;
            w_node <= 1'b0; w_dx <= 4'd0; w_dy <= 4'd0;
            w_dbuf <= 8'd0; w_dflag <= 8'd0;
            w_ackx <= 4'd0; w_acky <= 4'd0;
            for (wbi = 0; wbi < 2*WBURST; wbi = wbi + 1) w_buf[wbi] <= 256'd0;
        end else begin
            if (w_take) w_valid <= 1'b0;
            // The destination comes from whichever instruction produces the
            // sub-tiles. A fused sweep starts emitting long before its DRAIN
            // is decoded, so it carries the address itself.
            if ((st == S_DRAIN) || ((st == S_GEMM) && emit_r)) begin
                w_base  <= base_r;
                w_node  <= dnode_r;
                w_dx    <= dstx_r;   w_dy    <= dsty_r;
                w_dbuf  <= dbuf_r;   w_dflag <= dflag_r;
                w_ackx  <= dackx_r;  w_acky  <= dacky_r;
            end

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
                w_flit <= { w_ddx, w_ddy,
                            CU_X[POS_WIDTH-1:0], CU_Y[POS_WIDTH-1:0],
                            w_dty, 8'h02, 1'b0, 3'b000, w_desc };
                w_valid <= 1'b1;
                w_st    <= W_DATA;
            end
            W_DATA: if (w_free) begin
                w_flit <= { w_ddx, w_ddy,
                            CU_X[POS_WIDTH-1:0], CU_Y[POS_WIDTH-1:0],
                            w_dtd, 8'h02,
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
        // an entry's four words consecutively and a CU_DATA stream is a run. A
        // second server -- multicast, a second MAG, a reordering fetch engine,
        // two senders into one CU -- would interleave two entries into one and
        // produce a plausible wrong tile.
        if (resetn && pl_valid && (pl_word != 2'd0) && (pl_ent !== asm_ent))
            $display("%0t ERROR mx_cluster_cu: word %0d of entry %0d arrived while assembling entry %0d",
                     $time, pl_word, pl_ent, asm_ent);
        // A data flit whose SOURCE is not the open stream's. Every flit carries
        // one, so interleaving is detectable directly rather than inferred --
        // and this is why the ack destination gets its own registers: compare
        // against `cd_ax` instead and a redirected burst faults on every flit,
        // which is the one case the redirect exists for.
        if (resetn && cd_pop && cd_run &&
            ((recv_flit[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH] !== cd_sx) ||
             (recv_flit[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH] !== cd_sy)))
            $display("%0t ERROR mx_cluster_cu: CU_DATA from (%0d,%0d) spliced into the stream open from (%0d,%0d)",
                     $time, recv_flit[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH],
                     recv_flit[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH],
                     cd_sx, cd_sy);
        // `last` against the descriptor's own count. They disagree when two
        // senders interleave into this CU: the second one's descriptor is
        // consumed as the first one's data and both buffers fill with nonsense,
        // which reads as a wrong tile and nothing else.
        if (resetn && cd_pop && cd_run &&
            (recv_flit[FLIT_WIDTH-4*POS_WIDTH-13] !== (cd_left == 9'd1)))
            $display("%0t ERROR mx_cluster_cu: CU_DATA last disagrees with the descriptor's len, %0d flits left",
                     $time, cd_left);
        if (resetn && cd_pop && !cd_run && (cd_dbuf == BUF_PEER) && cd_doff[0])
            $display("%0t ERROR mx_cluster_cu: peer stream starts at odd granule %0d -- a sub-tile is two flits",
                     $time, cd_doff);
        if (resetn && cd_pop && !cd_run && !cd_fits)
            $display("%0t ERROR mx_cluster_cu: CU_DATA burst rejected -- buf %0d, off %0d, len %0d does not fit",
                     $time, cd_dbuf, cd_doff, cd_dlen);
        // Dropped rather than held, so it costs a flit instead of the CU. Say
        // which type: silent loss is the whole hazard of dropping.
        if (resetn && recv_valid && recv_ready && !rx_resp && !rx_data)
            $display("%0t ERROR mx_cluster_cu: inbound flit of type %0h discarded",
                     $time, rtype);
    end

    initial if ((PW <= 256) || (PW > 512))
        $display("ERROR mx_cluster_cu: a peer sub-tile is %0d bits; the CU_DATA peer stream is built for two flits",
                 PW);
`endif

endmodule

`default_nettype wire

// One MAG memory port: a NoC memory endpoint and the AXI master behind it.
//
//   NoC mem port ──► intake queues ──► read engine  ──► AXI AR/R
//                                  └─► write slots  ──► AXI AW/W/B
//
// WHY THIS IS A MODULE. One read engine serves one run at a time (the next one
// chained behind it) through one emit buffer, so a port is the unit read
// concurrency grows by. See docs/mas/spec.md s2.
//
// A port is therefore the unit the machine grows by: its own intake, read
// engine, write slots and AXI channel, serving ~2 clusters. It carries NO
// transform -- that is one shared slot beside the memory path, reachable only
// by the mover (mag_xform.v).
//
// Everything here is per-port state. Nothing is shared with another port except
// the address space on the far side of AXI, and the ports never write the same
// word: each owns the C tiles of its own clusters.

`default_nettype none

`define MP_HDR_TY(f)  f[FLIT_WIDTH-4*POS_WIDTH-1 -: 4]
`define MP_SRC_X(f)   f[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH]
`define MP_SRC_Y(f)   f[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH]

module mag_mem_port #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer DATA_W     = 256,
    parameter integer ADDR_W     = 40,
    parameter integer ID_W       = 4,
    // where this port's NoC endpoint sits
    parameter integer MEM_X      = 0,
    parameter integer MEM_Y      = 1,
    // TWO per node that can have a write in flight, not one: a CU discards its
    // MEM_WR_ACK and does not wait for it, so its NEXT descriptor arrives while
    // the previous burst is still on the AXI bus, every time. With one slot per
    // CU the second descriptor finds nothing free, is never popped, and blocks
    // the data flits behind it that would have freed one. Under-sizing does not
    // corrupt anything; it deadlocks.
    parameter integer WR_SLOTS   = 16,
    parameter integer Q_DEPTH    = 64,
    parameter integer Q_MARGIN   = 4,
    // "block" MEASURED AND REJECTED: -456 LUT but 330.0 -> 305.3 MHz, under the
    // 320 floor -- `wq_flit` feeds the slot match and the worst path already
    // starts at this FIFO's output, where a BRAM CLKARDCLK is far slower.
    parameter MEM_TYPE           = "distributed",
    // ---- MAG L2 staging, aperture 0. 0 generates none of it ---------------
    // A staged fill reads port A rather than issuing AR; a write uses port B.
    parameter integer STAGE         = 0,      // a store AT THIS PORT
    parameter integer AP_DECODE     = 0,      // apertures exist SOMEWHERE
    parameter integer STAGE_BANKS   = 4,      // 4 x 16 single URAM, 2 MB; never a chain
    parameter integer STAGE_ENTRIES = 16384,
    parameter integer STAGE_PIPE    = 1,
    parameter integer STAGE_RLAT    = 0,      // mag_stage RLAT; 0 = blocks deep + 1
    parameter [1:0]   MESH_ID       = 2'd0,
    // ---- streamed reads: beats per AR (the Xache's read slot), and beats
    // requested ahead (mag.v: the DRAM port's reads in flight x RD_BEATS) ----
    parameter integer RD_BEATS      = 16,
    parameter integer RD_AHEAD      = 64
)(
    input  wire                clk,
    input  wire                resetn,

    // ---- AXI4 master: this port's channel into memory --------------------
    output reg  [ID_W-1:0]     m_awid,
    output reg  [ADDR_W-1:0]   m_awaddr,
    output reg  [7:0]          m_awlen,
    output wire [2:0]          m_awsize,
    output wire [1:0]          m_awburst,
    output reg                 m_awvalid,
    input  wire                m_awready,
    (* EXTRACT_RESET = "no" *)
    output reg  [DATA_W-1:0]   m_wdata,
    output wire [DATA_W/8-1:0] m_wstrb,
    output reg                 m_wlast,
    output reg                 m_wvalid,
    input  wire                m_wready,
    input  wire [ID_W-1:0]     m_bid,
    input  wire [1:0]          m_bresp,
    input  wire                m_bvalid,
    output wire                m_bready,
    output reg  [ID_W-1:0]     m_arid,
    output reg  [ADDR_W-1:0]   m_araddr,
    output reg  [7:0]          m_arlen,
    output wire [2:0]          m_arsize,
    output wire [1:0]          m_arburst,
    output reg                 m_arvalid,
    input  wire                m_arready,
    input  wire [ID_W-1:0]     m_rid,
    input  wire [DATA_W-1:0]   m_rdata,
    input  wire [1:0]          m_rresp,
    input  wire                m_rlast,
    input  wire                m_rvalid,
    output wire                m_rready,

    // ---- NoC: memory port ------------------------------------------------
    input  wire [FLIT_WIDTH-1:0] mem_in_data,
    input  wire                  mem_in_valid,
    output wire                  mem_in_busy,
    (* EXTRACT_RESET = "no" *)
    output reg  [FLIT_WIDTH-1:0] mem_out_data,
    output reg                   mem_out_valid,
    input  wire                  mem_out_busy,

    output wire [15:0]           mem_rd_count,
    output wire [15:0]           mem_wr_count
);
    localparam integer WS_BITS = (WR_SLOTS <= 1) ? 1 : $clog2(WR_SLOTS);

    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1;
    localparam [3:0] T_MEM_RD_RESP = 4'h2, T_MEM_WR_ACK = 4'h3;
    localparam [3:0] T_MEM_WR_DATA = 4'h4;

    localparam integer LSB = $clog2(DATA_W/8);

    assign m_awsize  = LSB[2:0];
    assign m_arsize  = LSB[2:0];
    assign m_awburst = 2'b01;
    assign m_arburst = 2'b01;
    assign m_wstrb   = {(DATA_W/8){1'b1}};
    assign m_bready  = 1'b1;

    // =====================================================================
    // Intake. Backpressure MUST NOT depend on what the flit is: deciding busy
    // from the incoming type means a flit this port cannot classify right now
    // blocks the port, and the mesh being in-order behind it, everything else --
    // including the flit that would free the resource.
    //
    // TWO QUEUES, DEMUXED BY TYPE. With one, a read request at the head that
    // cannot be taken blocks the write data behind it, and that data is what
    // lets a drain finish. Busy is still "is there room in both", which depends
    // only on this module's own state, so the hazard above does not return.
    // =====================================================================
    wire [FLIT_WIDTH-1:0] rq_flit, wq_flit;
    wire                  rq_empty, rq_full, rq_almost;
    wire                  wq_empty, wq_full, wq_almost;
    reg                   rq_pop, wq_pop;
    reg [$clog2(Q_DEPTH):0] rq_cnt, wq_cnt;

    // Busy when EITHER is near full: the port carries both and the sender cannot
    // be told which is short of room. Declared above the queues because the
    // accept term reads it and Vivado rejects use-before-declaration.
    assign mem_in_busy = (
        (rq_cnt >= (Q_DEPTH - Q_MARGIN))
        || rq_almost
        || (wq_cnt >= (Q_DEPTH - Q_MARGIN))
        || wq_almost
    );

    // ACCEPT EXACTLY WHEN THE SENDER BELIEVES WE DID. The link holds a flit
    // asserted until a cycle with `busy` low, so enqueuing on "is there room"
    // writes the SAME flit once per cycle of backpressure. Either error is
    // silent and permanent: a duplicated MEM_WR_DATA overruns its slot's
    // `ws_len` and the surplus matches nothing, while a dropped one leaves the
    // slot short forever, so it never becomes ready, the source's next
    // descriptor opens a SECOND slot, and its data matches the older one.
    // docs/noc/spec.md s2.1.
    wire       mi_take = mem_in_valid && !mem_in_busy;
    wire [3:0] mi_ty = `MP_HDR_TY(mem_in_data);
    wire       mi_rd = mi_take && (mi_ty == T_MEM_RD_REQ);
    wire       mi_wr = (
        mi_take && ((mi_ty == T_MEM_WR_REQ) || (mi_ty == T_MEM_WR_DATA))
    );

    // Whole flits, though a descriptor's [157:0] is reserved-zero and unread:
    // synthesis drops the unread LUTRAM columns itself (144 LUTRAM here for 288
    // bits), and storing [287:158] explicitly measured the same 30,879 node.
    sync_fifo #(
        .DATA_WIDTH  (FLIT_WIDTH),
        .FIFO_DEPTH  (Q_DEPTH),
        .MEMORY_TYPE (MEM_TYPE)
    ) u_rdq (
        .clk       (clk),
        .rst       (!resetn),
        .wr_en     (mi_rd),
        .wr_data   (mem_in_data),
        .wr_busy   (rq_full),
        .wr_almost (rq_almost),
        .rd_en     (rq_pop),
        .rd_data   (rq_flit),
        .rd_busy   (rq_empty)
    );

    sync_fifo #(
        .DATA_WIDTH  (FLIT_WIDTH),
        .FIFO_DEPTH  (Q_DEPTH),
        .MEMORY_TYPE (MEM_TYPE)
    ) u_wrq (
        .clk       (clk),
        .rst       (!resetn),
        .wr_en     (mi_wr),
        .wr_data   (mem_in_data),
        .wr_busy   (wq_full),
        .wr_almost (wq_almost),
        .rd_en     (wq_pop),
        .rd_data   (wq_flit),
        .rd_busy   (wq_empty)
    );

    always @(posedge clk) begin
        if (!resetn) begin
            rq_cnt <= 0;
            wq_cnt <= 0;
        end else begin
            rq_cnt <= (
                rq_cnt + (mi_rd ? 1'b1 : 1'b0) - (rq_pop ? 1'b1 : 1'b0)
            );
            wq_cnt <= (
                wq_cnt + (mi_wr ? 1'b1 : 1'b0) - (wq_pop ? 1'b1 : 1'b0)
            );
        end
    end

    // ---- the read queue's head ------------------------------------------
    wire        in_valid = !rq_empty;
    wire [3:0]  in_ty  = `MP_HDR_TY(rq_flit);
    wire [POS_WIDTH-1:0] in_sx = `MP_SRC_X(rq_flit);
    wire [POS_WIDTH-1:0] in_sy = `MP_SRC_Y(rq_flit);
    // NOC_MEM_ADDR is 40 bits WHATEVER ADDR_W is: a flit contract, not a width.
    // Slicing it by ADDR_W read `addr >> 6` on a 34-bit build, silently.
    localparam integer FA = 40;
    wire [ADDR_W-1:0] in_addr = rq_flit[255 -: FA];
    wire [7:0]  in_len   = rq_flit[215 -: 8];
    wire [7:0]  in_flags = rq_flit[207 -: 8];
    wire [7:0]  in_txn   = rq_flit[FLIT_WIDTH-4*POS_WIDTH-5 -: 8];

    // ---- the write queue's head -----------------------------------------
    wire        wi_valid = !wq_empty;
    wire [3:0]  wi_ty  = `MP_HDR_TY(wq_flit);
    wire [POS_WIDTH-1:0] wi_sx = `MP_SRC_X(wq_flit);
    wire [POS_WIDTH-1:0] wi_sy = `MP_SRC_Y(wq_flit);
    wire [ADDR_W-1:0] wi_addr = wq_flit[255 -: FA];
    wire [7:0]  wi_len  = wq_flit[215 -: 8];
    wire [7:0]  wi_txn  = wq_flit[FLIT_WIDTH-4*POS_WIDTH-5 -: 8];

    // Request flags. Bits 0..3 are the spec's cache hints, unused here.
    //   [4] [5] reserved and ignored -- a fetch is never transformed, so a
    //           request cannot select a transform at all (spec/flit-format s4)
    //   [6] STREAM: this is a DESCRIPTOR, fetch `count` consecutive entries
    wire in_stream = in_flags[6];

    // Entries in a streaming fetch. Contiguous by construction -- the driver
    // stores operands tile-major precisely so a pass's entries are one run
    // (docs/isa/kernel.md s3).
    wire [7:0] in_count = (
        in_stream ? ((rq_flit[199 -: 8] == 8'd0) ? 8'd1 : rq_flit[199 -: 8])
        : 8'd1
    );

    // WORDS PER ENTRY, so a client whose line is not 128 bytes can stream too.
    // 0 KEEPS THE LEGACY 4, which is what every existing requester sends -- the
    // field is backward compatible by construction.
    wire [7:0] in_ew_raw = rq_flit[165 -: 8];
    wire [2:0] in_ew = (
        ((in_ew_raw == 8'd0) || (in_ew_raw > 8'd4)) ? 3'd4 : in_ew_raw[2:0]
    );
    // The run in beats, count x ew for ew 1..4: shifts and one add, no multiplier.
    wire [10:0] in_cnt11  = {3'd0, in_count};
    wire [10:0] in_beats = in_ew[2] ? (in_cnt11 << 2)
                         : in_ew[1] ? ((in_cnt11 << 1) + (in_ew[0] ? in_cnt11 : 11'd0))
                         : in_cnt11;

    // EXTRA DESTINATIONS. Every cluster sweeps the same rows of A, so without
    // this the entry is fetched from DRAM once per CONSUMER for a bit-identical
    // result. The requester is always a destination; these are the others.
    wire [23:0] in_peer = rq_flit[191 -: 24];
    wire [1:0]  in_nd   = rq_flit[167 -: 2];

    // ================================================================
    // The read engine, with its own state and its own return context: a run
    // occupies it for hundreds of cycles, so it runs alongside the write engine.
    // Plain reads share its AR channel and never overlap a run.
    //
    // Declared before the emit path, because `q_rdy` is set from `rs`.
    // ================================================================
    localparam [2:0] RS_IDLE = 3'd0, RS_FILL = 3'd1;
    localparam [2:0] RS_WAIT = 3'd2, RS_STG = 3'd3;
    // The run's last entry is handed off and the NEXT run's ARs are already out:
    // its context loads once that entry has finished emitting.
    localparam [2:0] RS_NEXT = 3'd4;
    reg [2:0] rs;
    reg       rd_stg;                  // this run is served from staging

    // THE NEXT RUN, taken once every AR of this one is out, so DRAM latency is
    // paid once per stream of runs rather than once per run. DRAM runs only.
    reg                 nx_v;
    reg [POS_WIDTH-1:0] nx_x, nx_y;
    reg [7:0]           nx_txn, nx_cnt;
    reg [23:0]          nx_peer;
    reg [1:0]           nx_nd, nx_elast;

    // MESH FIRST, THEN APERTURE -- the same order mag_stage decodes in, and the
    // reason a packet only transiting this mesh is never claimed.
    function stg_is;
        input [ADDR_W-1:0] a;
        begin
            stg_is = (
                (STAGE != 0)
                && a[39]
                && !a[38]
                && (a[37:36] == MESH_ID)
                && (a[35:32] == 4'h0)
            );
        end
    endfunction

    // NOT SERVABLE AT THIS PORT -- narrower than "undefined". mag_stage's
    // AP_IMPL is the architectural set; a memory port serves staging alone.
    // AP_DECODE, not STAGE: the drop holds wherever the store lives.
    function stg_unserved;
        input [ADDR_W-1:0] a;
        begin
            stg_unserved = (
                (AP_DECODE != 0)
                && a[39]
                && !a[38]
                && ((a[37:36] != MESH_ID) || (a[35:32] != 4'h0))
            );
        end
    endfunction

    reg  [ADDR_W-1:0]     rd_cur;      // the entry being fetched from staging
    reg                   stg_go;
    wire [4*DATA_W-1:0]   stg_rdata;
    wire                  stg_rvalid, stg_b_gnt;

    reg [POS_WIDTH-1:0] rd_x, rd_y;
    reg [7:0]  rd_txn;
    // `rd_cnt` entries in the run, `rd_ent` the one being captured.
    reg [7:0]  rd_cnt, rd_ent;
    reg [ADDR_W-1:0] rd_ebytes;  // entry size, for the staged walk
    reg [1:0]  rd_elast;         // last word index within an entry
    // The next staged entry's address, accumulated: base + (ent+1)*ebytes is a
    // full-width multiply.
    reg [ADDR_W-1:0] rd_anext;

    // READ-AHEAD, in BEATS (a run's entries are contiguous): a vector core's
    // 1-word entries burst like a cluster's 4-word ones, the round trip paid once.
    localparam integer BEATS_4K = 4096 >> LSB;
    reg  [10:0]       ar_left;     // beats not yet requested
    reg  [ADDR_W-1:0] ar_addr;     // where the next AR starts
    reg  [7:0]        ar_out;      // beats requested and not yet taken
    // The next AR: the run's remainder, at most RD_BEATS, never across 4 KB.
    wire [10:0] ar_to4k  = BEATS_4K[10:0] - {{(LSB-1){1'b0}}, ar_addr[11:LSB]};
    wire [10:0] ar_cap   = (ar_to4k < RD_BEATS[10:0]) ? ar_to4k : RD_BEATS[10:0];
    wire [7:0]  ar_beats = (ar_left < ar_cap) ? ar_left[7:0] : ar_cap[7:0];
    wire        ar_room  = ({1'b0, ar_out} + {1'b0, ar_beats}) <= RD_AHEAD;
    wire        ar_go    = ((rs == RS_FILL) || (rs == RS_WAIT) || (rs == RS_NEXT))
                        && !rd_stg && (ar_left != 11'd0)
                        && (!m_arvalid || m_arready) && ar_room;
    // An entry's beats ARE its operand words -- nothing converts them here.
    // They land here rather than in the emit buffer directly, because the
    // emitter may still be handing out the previous entry.
    (* EXTRACT_RESET = "no" *) reg [255:0] p_w0, p_w1, p_w2, p_w3;
    reg [1:0]   p_cnt;

    // Emit buffer: takes the captured entry in the cycle the previous one's last
    // flit leaves, while capture takes the next first beat -- one flit a cycle.
    (* EXTRACT_RESET = "no" *) reg [255:0] e_w0, e_w1, e_w2, e_w3;
    reg [7:0]   e_tag;
    reg         e_act;
    reg [1:0]   e_dst;
    reg [23:0]  rd_peer;
    reg [1:0]   rd_nd;

    // Destination 0 is the requester itself; the rest come from the list. A
    // node index is {y,x} with y in the high nibble -- the packing PROG_DST and
    // NODE_STATUS use.
    wire [7:0] e_peer_sel = (
        (e_dst == 2'd1)   ? rd_peer[7:0]
        : (e_dst == 2'd2) ? rd_peer[15:8]
        : rd_peer[23:16]
    );
    wire [POS_WIDTH-1:0] e_dx = (
        (e_dst == 2'd0) ? rd_x : e_peer_sel[POS_WIDTH-1:0]
    );
    wire [POS_WIDTH-1:0] e_dy = (
        (e_dst == 2'd0) ? rd_y : e_peer_sel[4 +: POS_WIDTH]
    );
    // The entry is complete. Remembered rather than acted on directly: if the
    // emit buffer is still busy when it completes, the read engine would
    // otherwise wait forever for an edge that already happened.
    reg         q_rdy;

    reg  [1:0]   q_emit;

    // The R channel crossed uncut both ways: 85% of m62_c1's failing paths were
    // 7-11 levels at 90% route. sb_skid's i_ready does not depend on o_ready.
    wire [DATA_W-1:0] r_data;
    wire              r_last, r_valid, r_ready;

    sb_skid #(
        .W (DATA_W + 1)
    ) u_rskid (
        .clk     (clk),
        .rst     (!resetn),
        .i_valid (m_rvalid),
        .i_ready (m_rready),
        .i_data  ({m_rlast, m_rdata}),
        .o_valid (r_valid),
        .o_ready (r_ready),
        .o_data  ({r_last, r_data})
    );

    // An entry is 4 words of DATA_W unless the request names fewer.
    localparam integer P_ENTRY_BITS  = 1024;
    localparam [ADDR_W-1:0] P_ENTRY_BYTES = P_ENTRY_BITS / 8;         // 128

    wire [ADDR_W-1:0] in_ebytes = {{(ADDR_W-3){1'b0}}, in_ew} << LSB;

    // PLAIN READS ARE PIPELINED: up to PR_MAX in flight, their return context
    // queued in AR order, which is the order AXI answers one id in.
    localparam integer PR_MAX = 8;
    reg  [3:0] pr_cnt;                  // plain reads issued and not yet ended
    wire [POS_WIDTH-1:0] pr_x, pr_y;
    wire [7:0]           pr_txn;
    wire pr_beat, pr_end;               // a plain read's beat leaves; its last
    reg [15:0] n_rd, n_wr;

    // One output register for emits, write ACKs and plain-read beats. An ACK
    // that waited out an emit takes the next cycle: a fill cannot hold a drain.
    wire out_free = !mem_out_valid || !mem_out_busy;
    wire ack_rdy;                       // the oldest posted write may be ACKed
    reg  ack_turn;                      // ... and waited out an emit for it
    wire ack_go   = ack_rdy && out_free && (!e_act || ack_turn);
    wire emit_go  = e_act && out_free && !ack_go;
    wire pr_out   = out_free && !emit_go && !ack_go;

    assign mem_rd_count = n_rd;
    assign mem_wr_count = n_wr;

    // ---- write reassembly, slots matched by source ------------------------
    // A write arrives as a descriptor flit and a data flit and the mesh can put
    // another node's flit between them, so collecting "the next flit" into the
    // open write is wrong the moment two nodes write at once. Each source gets
    // its own slot, matched by source coordinate.
    //
    // A slot walks val -> rdy -> iss -> free. All three bits are needed: with
    // only val and rdy, a slot whose write is ON THE BUS reads as {val, !rdy},
    // indistinguishable from one still waiting for its data, and the next
    // WR_DATA from that source binds to the in-flight slot.
    //
    // A SLOT HOLDS A BURST, because one burst's W beats have to be contiguous
    // on AXI while the mesh interleaves data flits freely.
    localparam integer WBURST = 8;
    localparam integer WBW    = (WBURST <= 1) ? 1 : $clog2(WBURST);

    reg                  ws_val  [0:WR_SLOTS-1];   // descriptor seen
    reg                  ws_rdy  [0:WR_SLOTS-1];   // data complete, AXI-ready
    reg                  ws_iss  [0:WR_SLOTS-1];   // issued to AXI, awaiting ack
    reg [POS_WIDTH-1:0]  ws_x    [0:WR_SLOTS-1], ws_y [0:WR_SLOTS-1];
    reg [7:0]            ws_txn  [0:WR_SLOTS-1];
    reg [ADDR_W-1:0]     ws_addr [0:WR_SLOTS-1];
    reg                  ws_stg  [0:WR_SLOTS-1];   // lands in staging, not DRAM
    reg                  ws_bad  [0:WR_SLOTS-1];   // reserved aperture: dropped
    reg [WBW:0]          ws_len  [0:WR_SLOTS-1];   // beats expected
    reg [WBW:0]          ws_cnt  [0:WR_SLOTS-1];   // beats received

    // "this slot's next beat is its last", per slot and from registered state
    // only, so `ws_rdy` sees a 1-bit select instead of mux -> add -> compare.
    wire [WR_SLOTS-1:0] ws_fill_now;
    genvar gw;
    generate
    for (gw = 0; gw < WR_SLOTS; gw = gw + 1) begin : g_fill
        assign ws_fill_now[gw] = (ws_cnt[gw] + 1'b1 == ws_len[gw]);
    end
    endgenerate

    integer wi;
    reg [WS_BITS-1:0] ws_free, ws_match;
    reg               ws_has_free, ws_has_match;

    always @(*) begin
        ws_free      = {WS_BITS{1'b0}};
        ws_has_free  = 1'b0;
        ws_match     = {WS_BITS{1'b0}};
        ws_has_match = 1'b0;
        for (wi = WR_SLOTS-1; wi >= 0; wi = wi - 1) begin
            if (!ws_val[wi]) begin
                ws_free     = wi[WS_BITS-1:0];
                ws_has_free = 1'b1;
            end
            // !ws_iss: a slot on the bus is NOT waiting for data, however much
            // its val/rdy pair looks like it is.
            if (
                ws_val[wi]
                && !ws_rdy[wi]
                && !ws_iss[wi]
                && (ws_x[wi] == wi_sx)
                && (ws_y[wi] == wi_sy)
            ) begin
                ws_match     = wi[WS_BITS-1:0];
                ws_has_match = 1'b1;
            end
        end
    end

    // Assigned below the ready-order queue, which reads `ws_issue`.
    wire               ws_has_pick;
    wire [WS_BITS-1:0] ws_pick;

    // One flit leaves each queue per cycle: a write descriptor when a slot is
    // free, its data when the slot is waiting for it, or a read when the engine
    // can take one. Anything else is simply not popped and waits.
    wire take_wr_req  = wi_valid && (wi_ty == T_MEM_WR_REQ)  && ws_has_free;
    wire take_wr_data = wi_valid && (wi_ty == T_MEM_WR_DATA) && ws_has_match;

    // A STREAM goes to the read engine; a plain read returns its beats verbatim.
    // One AXI read channel, so the two kinds never overlap.
    wire rd_free = (rs == RS_IDLE) && !e_act;
    wire nx_free = (rs == RS_FILL) && !rd_stg && (ar_left == 11'd0) && !nx_v
                && !stg_is(in_addr) && !stg_unserved(in_addr);
    wire take_rd_e = (
        in_valid
        && (in_ty == T_MEM_RD_REQ)
        && in_stream
        && (rd_free || nx_free)
        && (pr_cnt == 4'd0)
    );
    wire take_nx = take_rd_e && (rs != RS_IDLE);
    wire take_rd_p = (
        in_valid
        && (in_ty == T_MEM_RD_REQ)
        && !in_stream
        && (rs == RS_IDLE)
        && (pr_cnt != PR_MAX[3:0])
        && !m_arvalid
    );
    wire take_rd = take_rd_e || take_rd_p;

    sync_fifo #(
        .DATA_WIDTH  (2 * POS_WIDTH + 8),
        .FIFO_DEPTH  (PR_MAX),
        .MEMORY_TYPE ("distributed")
    ) u_prq (
        .clk       (clk),
        .rst       (!resetn),
        .wr_en     (take_rd_p),
        .wr_data   ({in_sx, in_sy, in_txn}),
        .wr_busy   (),
        .wr_almost (),
        .rd_en     (pr_end),
        .rd_data   ({pr_x, pr_y, pr_txn}),
        .rd_busy   ()
    );

    // THE WRITE ENGINE, one beat a cycle: next burst (nb, its AW posted at the
    // pick) -> slot RAM read (we) -> RAM output held (d) -> W register / port B.
    reg               nb_v, nb_stg, nb_bad;
    reg [WS_BITS-1:0] nb_slot;
    reg [WBW:0]       nb_len;
    reg [ADDR_W-1:0]  nb_addr;
    reg               we_act;
    reg [WS_BITS-1:0] we_slot;
    reg [WBW:0]       we_cnt, we_len;
    reg               we_stg, we_bad;
    reg [ADDR_W-1:0]  we_addr;
    reg               d_v, d_last, d_stg, d_bad;
    reg [WS_BITS-1:0] d_slot;
    reg [ADDR_W-1:0]  d_addr;
    wire [DATA_W-1:0] wd_next;
    wire ws_issue = ws_has_pick && !nb_v && !m_awvalid;

    // ISSUE IN READY ORDER, never "lowest ready". Lowest-free allocation
    // recycles freed low slots, whose refill goes ready before the next
    // pick -- so a lowest-ready pick starves a
    // ready high slot for the whole stream (measured: an L1 writeback held
    // 2,300+ cycles by an RMW stream) and lands one source's same-address
    // writes out of program order. The queue fixes both: ready order IS
    // per-source program order, and its head is at most WR_SLOTS-1
    // services from issue. Depth WR_SLOTS: it cannot overflow.
    wire [WS_BITS-1:0] rdyq_out;
    wire               rdyq_empty, rdyq_full;
    wire               rdyq_push = take_wr_data && ws_fill_now[ws_match];

    sync_fifo #(
        .DATA_WIDTH  (WS_BITS),
        .FIFO_DEPTH  (WR_SLOTS),
        .MEMORY_TYPE ("distributed")
    ) u_rdyq (
        .clk       (clk),
        .rst       (!resetn),
        .wr_en     (rdyq_push),
        .wr_data   (ws_match),
        .wr_busy   (rdyq_full),
        .wr_almost (),
        .rd_en     (ws_issue),
        .rd_data   (rdyq_out),
        .rd_busy   (rdyq_empty)
    );

    assign ws_has_pick = !rdyq_empty;
    assign ws_pick     = rdyq_out;

    always @(*) begin
        wq_pop = take_wr_req || take_wr_data;
    end

    always @(*) begin
        rq_pop = take_rd;
    end

    // ---- the staging store, aperture 0 --------------------------------------
    // Port A is the fill path, entry-wide; port B takes a staged drain's beats.
    generate
    if (STAGE != 0) begin : g_stage
        mag_stage #(
            .DATA_W  (DATA_W),
            .ADDR_W  (ADDR_W),
            .WORDS   (4),
            .BANKS   (STAGE_BANKS),
            .ENTRIES (STAGE_ENTRIES),
            .PIPE    (STAGE_PIPE),
            .RLAT    (STAGE_RLAT),
            .MESH_ID (MESH_ID)
        ) u_stage (
            .clk      (clk),
            .rst      (!resetn),
            .a_req    (stg_go),
            .a_we     (1'b0),
            .a_addr   (rd_cur),
            .a_wdata  ({(4*DATA_W){1'b0}}),
            .a_mine   (),
            .a_gnt    (),
            .a_fault  (),
            .a_rvalid (stg_rvalid),
            .a_rdata  (stg_rdata),
            .b_req    (d_v && d_stg && !d_bad),
            .b_we     (1'b1),
            .b_addr   (d_addr),
            .b_wdata  (wd_next),
            .b_wstrb  ({(DATA_W/8){1'b1}}),
            .b_mine   (),
            .b_gnt    (stg_b_gnt),
            .b_rvalid (),
            .b_rdata  ()
        );
    end else begin : g_nostage
        assign stg_rvalid = 1'b0;
        assign stg_rdata  = {(4*DATA_W){1'b0}};
        assign stg_b_gnt  = 1'b0;
    end
    endgenerate

`ifndef SYNTHESIS
    reg [13:0] pick_wait = 14'd0;
    always @(posedge clk) begin
        if (
            resetn
            && in_valid
            && (in_ty == T_MEM_RD_REQ)
            && stg_unserved(in_addr)
        ) begin
            $display("%0t ERROR mag_mem_port(mesh %0d, %0d,%0d):read at %h names a reserved aperture or another mesh -- DROPPED rather than aliased onto DRAM, so the requester will hang",
                     $time, MESH_ID, MEM_X, MEM_Y, in_addr);
        end
        if (
            resetn
            && wi_valid
            && (wi_ty == T_MEM_WR_REQ)
            && stg_unserved(wi_addr)
        ) begin
            $display("%0t ERROR mag_mem_port(mesh %0d, %0d,%0d):write at %h names a reserved aperture or another mesh -- DROPPED rather than aliased onto DRAM; its ACK carries the drop flag",
                     $time, MESH_ID, MEM_X, MEM_Y, wi_addr);
        end
        // THE DROP ITSELF, named at the moment it happens. Without this the
        // first symptom is "write data with no open write" hundreds of cycles
        // later and several modules away.
        if (resetn && ((mi_rd && rq_full) || (mi_wr && wq_full))) begin
            $display("%0t ERROR mag_mem_port(mesh %0d, %0d,%0d):input flit DROPPED -- backpressure is too late",
                     $time, MESH_ID, MEM_X, MEM_Y);
        end
        // Slot lifetime, one line per descriptor and per beat: thousands of
        // $display per run at four requesters. -d MAG_WSLOT_PROBE to see them.
`ifdef MAG_WSLOT_PROBE
        if (resetn && take_wr_req) begin
            $display("  PROBE %0t mesh %0d slot %0d opened by (%0d,%0d) addr %h len %0d",
                     $time, MESH_ID, ws_free, wi_sx, wi_sy, wi_addr, wi_len);
        end
        if (
            resetn
            && wi_valid
            && (wi_ty == T_MEM_WR_DATA)
            && ws_has_match
        ) begin
            $display("  PROBE %0t mesh %0d slot %0d took beat from (%0d,%0d)",
                     $time, MESH_ID, ws_match, wi_sx, wi_sy);
        end
`endif
        if (
            resetn
            && wi_valid
            && (wi_ty == T_MEM_WR_DATA)
            && !ws_has_match
        ) begin
            $display("%0t ERROR mag_mem_port(mesh %0d, %0d,%0d):write data from (%0d,%0d) with no open write",
                     $time, MESH_ID, MEM_X, MEM_Y, wi_sx, wi_sy);
        end
        // The ready-order queue and the slot table must never disagree, and a
        // ready head that cannot issue is a wedge whatever fairness says.
        if (resetn && rdyq_push && rdyq_full) begin
            $display("%0t ERROR mag_mem_port(mesh %0d, %0d,%0d):ready queue overflow -- table/queue desync",
                     $time, MESH_ID, MEM_X, MEM_Y);
        end
        if (resetn && ws_issue && !ws_rdy[ws_pick]) begin
            $display("%0t ERROR mag_mem_port(mesh %0d, %0d,%0d):issued slot %0d is not ready -- table/queue desync",
                     $time, MESH_ID, MEM_X, MEM_Y, ws_pick);
        end
        if (resetn) begin
            if (!ws_has_pick || ws_issue) begin
                pick_wait <= 14'd0;
            end else begin
                pick_wait <= pick_wait + 14'd1;
            end
            if (pick_wait == 14'd8192) begin
                $display("%0t ERROR mag_mem_port(mesh %0d, %0d,%0d):ready slot %0d unissued for 8192 cycles",
                         $time, MESH_ID, MEM_X, MEM_Y, ws_pick);
            end
        end
    end
`endif

`ifdef MAG_PORT_TRACE
    // Each run taken, each AR, each read entering, the read engine going idle,
    // and a stalled head or held intake every 1024 cycles it lasts.
    reg [63:0] pt_cyc;
    reg [2:0]  pt_rs;
    reg [31:0] pt_wait = 32'd0, pt_hold = 32'd0;
    always @(posedge clk) begin
        if (!resetn) begin
            pt_cyc <= 64'd0; pt_rs <= RS_IDLE;
        end else begin
            pt_cyc <= pt_cyc + 64'd1;
            pt_rs  <= rs;
            if (take_rd_e) begin
                $display("PORT %0d %0d,%0d run at %h n %0d from %0d,%0d nd %0d%s", pt_cyc, MEM_X,
                         MEM_Y, in_addr, in_count, in_sx, in_sy, in_nd, take_nx ? " (next)" : "");
            end
            if (m_arvalid && m_arready) begin
                $display("PORT %0d %0d,%0d ar %h len %0d out %0d", pt_cyc, MEM_X, MEM_Y,
                         m_araddr, m_arlen, ar_out);
            end
            if ((pt_rs != RS_IDLE) && (rs == RS_IDLE)) begin
                $display("PORT %0d %0d,%0d engine idle", pt_cyc, MEM_X, MEM_Y);
            end
            if (mi_rd) begin
                $display("PORT %0d %0d,%0d in rd from %0d,%0d at %h", pt_cyc, MEM_X, MEM_Y,
                         `MP_SRC_X(mem_in_data), `MP_SRC_Y(mem_in_data),
                         mem_in_data[255 -: 40]);
            end
            if (in_valid && !take_rd && (pt_wait[9:0] == 10'd1023)) begin
                $display("PORT %0d %0d,%0d STALL head ty %0d stream %0d at %h rs %0d e_act %0d pr %0d arv %0d rq %0d wq %0d busy %0d",
                         pt_cyc, MEM_X, MEM_Y, in_ty, in_stream, in_addr, rs, e_act,
                         pr_cnt, m_arvalid, rq_cnt, wq_cnt, mem_in_busy);
            end
            if (!in_valid && mem_in_valid && mem_in_busy && (pt_hold[9:0] == 10'd1023)) begin
                $display("PORT %0d %0d,%0d HELD intake busy rq %0d wq %0d", pt_cyc, MEM_X,
                         MEM_Y, rq_cnt, wq_cnt);
            end
            pt_wait <= (in_valid && !take_rd) ? pt_wait + 32'd1 : 32'd0;
            pt_hold <= (mem_in_valid && mem_in_busy) ? pt_hold + 32'd1 : 32'd0;
        end
    end
    // AXI data beats each way, and the cycles counted, for a utilisation table.
    reg [31:0] pt_rb = 32'd0, pt_wb = 32'd0, pt_out = 32'd0;
    always @(posedge clk) begin
        if (resetn) begin
            if (m_rvalid && m_rready) pt_rb <= pt_rb + 32'd1;
            if (m_wvalid && m_wready) pt_wb <= pt_wb + 32'd1;
            if (mem_out_valid && !mem_out_busy) pt_out <= pt_out + 32'd1;
        end
    end
    final $display("PORTRES %0d,%0d cycles %0d r_beats %0d w_beats %0d out_flits %0d", MEM_X,
                   MEM_Y, pt_cyc, pt_rb, pt_wb, pt_out);
`endif

    // A plain beat becomes a flit, so it needs the output register; RS_FILL
    // needs the capture registers empty or handing their entry over this cycle.
    wire e_free_now = !e_act || (emit_go && (q_emit == rd_elast) && !(e_dst < rd_nd));
    wire handoff    = q_rdy && e_free_now;
    wire pr_mode    = (rs == RS_IDLE) && (pr_cnt != 4'd0);
    // Behind a run's captured LAST entry the next beat is the next run's, which
    // waits for that run's geometry (RS_NEXT).
    wire rd_final   = (rd_ent + 8'd1 == rd_cnt);
    assign r_ready = (pr_mode && pr_out)
                   || ((rs == RS_FILL) && (!q_rdy || (e_free_now && !rd_final)));
    assign pr_beat = pr_mode && r_valid && r_ready;
    assign pr_end  = pr_beat && r_last;
    wire p_done = (rs == RS_FILL) && r_valid && r_ready && (p_cnt == rd_elast);

    // `d` hands a beat to the W register or port B when it has room; a
    // reserved-aperture burst is one beat that goes nowhere.
    wire d_go    = d_v && (d_bad || (d_stg ? stg_b_gnt : (!m_wvalid || m_wready)));
    wire we_rd   = we_act && (!d_v || d_go);
    wire we_end  = we_rd && (we_cnt + 1'b1 == we_len);
    wire nb_take = nb_v && (!we_act || we_end);

    // THE SLOT DATA IS BLOCK RAM: as a reg array read at three indices it cost
    // 1,218 LUT per port. Its output holds while `rd_en` is low, which is `d`.
    kohaku_sdpram #(
        .WIDTH    (DATA_W),
        .DEPTH    (WR_SLOTS * WBURST),
        .MEM_PRIM ("block"),
        .READ_LAT (1)
    ) u_wsdata (
        .clk     (clk),
        .wr_en   (take_wr_data),
        .wr_addr ({ws_match, ws_cnt[ws_match][WBW-1:0]}),
        .wr_data (wq_flit[DATA_W-1:0]),
        .rd_en   (we_rd),
        .rd_addr ({we_slot, we_cnt[WBW-1:0]}),
        .rd_data (wd_next)
    );

    always @(posedge clk) begin
        if (!resetn) begin
            nb_v <= 1'b0; we_act <= 1'b0; d_v <= 1'b0;
            m_awvalid <= 1'b0; m_wvalid <= 1'b0; m_wlast <= 1'b0;
            m_awaddr <= {ADDR_W{1'b0}}; m_awlen <= 8'd0; m_awid <= {ID_W{1'b0}};
        end else begin
            if (m_awvalid && m_awready) begin
                m_awvalid <= 1'b0;
            end
            // The AW goes out at the pick, a burst ahead of its data.
            if (ws_issue) begin
                nb_v    <= 1'b1;
                nb_slot <= ws_pick;
                nb_stg  <= ws_stg[ws_pick];
                nb_bad  <= ws_bad[ws_pick];
                nb_len  <= ws_bad[ws_pick] ? {{WBW{1'b0}}, 1'b1} : ws_len[ws_pick];
                nb_addr <= ws_addr[ws_pick];
                m_awaddr  <= ws_addr[ws_pick];
                m_awlen   <= {{(8-WBW-1){1'b0}}, ws_len[ws_pick] - 1'b1};
                m_awvalid <= !ws_stg[ws_pick] && !ws_bad[ws_pick];
            end else if (nb_take) begin
                nb_v <= 1'b0;
            end
            if (nb_take) begin
                we_act  <= 1'b1;
                we_slot <= nb_slot;
                we_stg  <= nb_stg;
                we_bad  <= nb_bad;
                we_len  <= nb_len;
                we_cnt  <= 0;
                we_addr <= nb_addr;
            end else if (we_rd) begin
                we_cnt  <= we_cnt + 1'b1;
                we_addr <= we_addr + (1 << LSB);
                if (we_end) begin
                    we_act <= 1'b0;
                end
            end
            if (we_rd) begin
                d_v    <= 1'b1;
                d_last <= (we_cnt + 1'b1 == we_len);
                d_stg  <= we_stg;
                d_bad  <= we_bad;
                d_slot <= we_slot;
                d_addr <= we_addr;
            end else if (d_go) begin
                d_v <= 1'b0;
            end
            if (d_go && !d_stg && !d_bad) begin
                m_wdata  <= wd_next;
                m_wlast  <= d_last;
                m_wvalid <= 1'b1;
            end else if (m_wready) begin
                m_wvalid <= 1'b0;
            end
        end
    end

    // POSTED WRITES: a burst's ACK is queued as its last beat leaves `d`, and
    // AXI answers the one id in order, so a B pairs with the oldest DRAM entry.
    reg  [WS_BITS:0]   b_have;          // B responses not yet paired with a slot
    wire [WS_BITS-1:0] ackq_slot;
    wire               ackq_needb, ackq_empty;
    wire wr_b = (b_have != 0);
    assign ack_rdy = !ackq_empty && (!ackq_needb || wr_b);

    sync_fifo #(
        .DATA_WIDTH  (WS_BITS + 1),
        .FIFO_DEPTH  (WR_SLOTS),
        .MEMORY_TYPE ("distributed")
    ) u_ackq (
        .clk       (clk),
        .rst       (!resetn),
        .wr_en     (d_go && d_last),
        .wr_data   ({d_slot, !d_stg && !d_bad}),
        .wr_busy   (),
        .wr_almost (),
        .rd_en     (ack_go),
        .rd_data   ({ackq_slot, ackq_needb}),
        .rd_busy   (ackq_empty)
    );

    integer wj;
    always @(posedge clk) begin
        if (!resetn) begin
            for (wj = 0; wj < WR_SLOTS; wj = wj + 1) begin
                ws_val[wj] <= 1'b0;
                ws_rdy[wj] <= 1'b0;
                ws_iss[wj] <= 1'b0;
                ws_len[wj] <= 1;
                ws_cnt[wj] <= 0;
            end
        end else begin
            if (take_wr_req) begin
                ws_val[ws_free]  <= 1'b1;
                ws_rdy[ws_free]  <= 1'b0;
                ws_iss[ws_free]  <= 1'b0;
                ws_x[ws_free]    <= wi_sx;
                ws_y[ws_free]    <= wi_sy;
                ws_txn[ws_free]  <= wi_txn;
                ws_addr[ws_free] <= wi_addr;
                ws_stg[ws_free]  <= stg_is(wi_addr);
                ws_bad[ws_free]  <= stg_unserved(wi_addr);
                // `len` is beats-minus-one: a single beat writes 0.
                ws_len[ws_free]  <= wi_len[WBW:0] + 1'b1;
                ws_cnt[ws_free]  <= 0;
            end
            if (take_wr_data) begin
                ws_cnt[ws_match] <= ws_cnt[ws_match] + 1'b1;
                // ready only when the WHOLE burst has landed
                if (ws_fill_now[ws_match]) begin
                    ws_rdy[ws_match] <= 1'b1;
                end
            end
            // picked: not pickable again. freed only once the write is acked,
            // so the source cannot reuse the slot before its data is safe.
            if (ws_issue) begin
                ws_rdy[ws_pick] <= 1'b0;
                ws_iss[ws_pick] <= 1'b1;
            end
            if (ack_go) begin
                ws_val[ackq_slot] <= 1'b0;
                ws_iss[ackq_slot] <= 1'b0;
            end
        end
    end

    always @(posedge clk) begin
        if (!resetn) begin
            pr_cnt <= 4'd0;
            m_arvalid <= 1'b0;
            m_arlen   <= 8'd0;
            m_araddr  <= {ADDR_W{1'b0}};
            m_arid    <= {ID_W{1'b0}};
            mem_out_valid <= 1'b0;
            n_rd   <= 16'd0;
            n_wr   <= 16'd0;
            b_have   <= 0;
            ack_turn <= 1'b0;
            q_emit <= 2'd0;
            rd_stg <= 1'b0;
            stg_go <= 1'b0;
            rd_cur <= {ADDR_W{1'b0}};
            rs    <= RS_IDLE;
            nx_v  <= 1'b0;
            q_rdy <= 1'b0;
            e_act <= 1'b0;
            e_tag <= 8'd0;
            rd_x   <= 0;
            rd_y   <= 0;
            rd_txn <= 0;
            rd_cnt  <= 8'd1;
            rd_ent  <= 8'd0;
            rd_peer <= 24'd0;
            rd_nd   <= 2'd0;
            e_dst   <= 2'd0;
            p_cnt   <= 2'd0;
            rd_ebytes <= P_ENTRY_BYTES;
            rd_elast  <= 2'd3;
            rd_anext  <= {ADDR_W{1'b0}};
            ar_left   <= 11'd0;
            ar_addr   <= {ADDR_W{1'b0}};
            ar_out    <= 8'd0;
            // mem_out_data, p_w* and e_w* are payload: mem_out_valid and
            // e_act/q_rdy qualify them, all reset above.
        end else begin
            if (mem_out_valid && !mem_out_busy) begin
                mem_out_valid <= 1'b0;
            end
            if (ws_issue) begin
                n_wr <= n_wr + 16'd1;
            end
            if (m_arvalid && m_arready) begin
                m_arvalid <= 1'b0;
            end
            // m_bready is tied high: every B is counted the cycle it appears.
            b_have   <= b_have + (m_bvalid ? 1'b1 : 1'b0)
                      - ((ack_go && ackq_needb) ? 1'b1 : 1'b0);
            ack_turn <= ack_rdy && emit_go;
            if (ack_go) begin
                mem_out_data <= {
                    ws_x[ackq_slot], ws_y[ackq_slot],
                    MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                    T_MEM_WR_ACK, ws_txn[ackq_slot], 1'b1, 3'b000,
                    {(FLIT_WIDTH-4*POS_WIDTH-17){1'b0}}, ws_bad[ackq_slot]
                };
                mem_out_valid <= 1'b1;
            end

            // ---- plain reads: AR at the take, beats -> MEM_RD_RESP flits
            if (take_rd_p) begin
                m_araddr  <= in_addr[ADDR_W-1:0];
                m_arlen   <= in_len;
                m_arid    <= {ID_W{1'b0}};
                m_arvalid <= 1'b1;
                n_rd <= n_rd + 16'd1;
            end
            pr_cnt <= pr_cnt + (take_rd_p ? 4'd1 : 4'd0) - (pr_end ? 4'd1 : 4'd0);
            if (pr_beat) begin
                mem_out_data <= {
                    pr_x, pr_y,
                    MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                    T_MEM_RD_RESP, pr_txn, r_last, 3'b000,
                    r_data
                };
                mem_out_valid <= 1'b1;
            end

            // ============ the read engine, running alongside ================
            // Same always block as the plain reads because both drive the one
            // output register and the AR channel.
            case (rs)
                RS_IDLE: begin
                    if (take_rd_e) begin
                        rd_x   <= in_sx;
                        rd_y   <= in_sy;
                        rd_txn <= in_txn;
                        rd_peer <= in_peer;
                        rd_nd   <= in_nd;
                        rd_cnt  <= in_count;
                        rd_ent  <= 8'd0;
                        rd_ebytes <= in_ebytes;
                        rd_anext  <= in_addr + in_ebytes;
                        rd_elast  <= in_ew[1:0] - 2'd1;
                        p_cnt  <= 2'd0;
                        rd_cur <= in_addr;
                        if (stg_is(in_addr)) begin
                            // A staged run issues no AR; it counts as one read.
                            n_rd   <= n_rd + 16'd1;
                            rd_stg <= 1'b1;
                            stg_go <= 1'b1;
                            rs <= RS_STG;
                        end else if (stg_unserved(in_addr)) begin
                            rd_stg <= 1'b0;
                            rs <= RS_IDLE;  // dropped: it hangs instead of lying
                        end else begin
                            // The ARs go out from the issuer below.
                            rd_stg   <= 1'b0;
                            ar_left  <= in_beats;
                            ar_addr  <= in_addr;
                            rs <= RS_FILL;
                        end
                    end
                end

                // One entry per port-A read, which is what the 1,024-bit line
                // is for: no AR and no beats.
                RS_STG: begin
                    stg_go <= 1'b0;
                    if (stg_rvalid) begin
                        p_w0 <= stg_rdata[0*DATA_W +: DATA_W];
                        p_w1 <= stg_rdata[1*DATA_W +: DATA_W];
                        p_w2 <= stg_rdata[2*DATA_W +: DATA_W];
                        p_w3 <= stg_rdata[3*DATA_W +: DATA_W];
                        q_rdy <= 1'b1;
                        rs <= RS_WAIT;
                    end
                end

                // A burst spans entries, so an entry ends on its word count,
                // not on `r_last`; the handoff below empties the capture.
                RS_FILL: begin
                    if (r_valid && r_ready) begin
                        case (p_cnt)
                            2'd0:    p_w0 <= r_data;
                            2'd1:    p_w1 <= r_data;
                            2'd2:    p_w2 <= r_data;
                            default: p_w3 <= r_data;
                        endcase
                        p_cnt <= p_done ? 2'd0 : (p_cnt + 2'd1);
                    end
                    if (p_done) begin
                        q_rdy <= 1'b1;
                    end
                end

                // A staged entry waits here for the handoff.
                RS_WAIT: begin
                end

                RS_NEXT: if (!e_act) begin
                    rd_x     <= nx_x;
                    rd_y     <= nx_y;
                    rd_txn   <= nx_txn;
                    rd_peer  <= nx_peer;
                    rd_nd    <= nx_nd;
                    rd_cnt   <= nx_cnt;
                    rd_elast <= nx_elast;
                    rd_ent   <= 8'd0;
                    p_cnt    <= 2'd0;
                    nx_v     <= 1'b0;
                    rs       <= RS_FILL;
                end

                default: begin
                    rs <= RS_IDLE;
                end
            endcase

            if (take_nx) begin
                nx_v     <= 1'b1;
                nx_x     <= in_sx;
                nx_y     <= in_sy;
                nx_txn   <= in_txn;
                nx_peer  <= in_peer;
                nx_nd    <= in_nd;
                nx_cnt   <= in_count;
                nx_elast <= in_ew[1:0] - 2'd1;
                ar_left  <= in_beats;
                ar_addr  <= in_addr;
            end

            // The run's issuer. Plain reads never share the AR channel with a
            // run: a run is taken only with none in flight, and one only from RS_IDLE.
            if (ar_go) begin
                m_araddr  <= ar_addr;
                m_arlen   <= ar_beats - 8'd1;
                m_arid    <= {ID_W{1'b0}};
                m_arvalid <= 1'b1;
                ar_left   <= ar_left - {3'd0, ar_beats};
                ar_addr   <= ar_addr + ({{(ADDR_W-8){1'b0}}, ar_beats} << LSB);
                n_rd      <= n_rd + 16'd1;
            end
            ar_out <= ar_out + (ar_go ? ar_beats : 8'd0)
                - ((r_valid && r_ready && (rs == RS_FILL)) ? 8'd1 : 8'd0);

            // A RESPONSE SAYS WHERE IT BELONGS. `e_tag` is the requester's own
            // entry index (the txn it sent, plus this entry's position in the
            // run) and the two spare header bits carry the word within the
            // entry, so the receiver needs no cursor and arrival order stops
            // being load-bearing. That is what makes a streaming fetch possible.
            if (emit_go) begin
                mem_out_data <= {
                    e_dx, e_dy,
                    MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                    T_MEM_RD_RESP, e_tag, (q_emit == rd_elast),
                    1'b0, q_emit,
                    (
                        (q_emit == 2'd0)   ? e_w0
                        : (q_emit == 2'd1) ? e_w1
                        : (q_emit == 2'd2) ? e_w2
                        : e_w3
                    )
                };
                mem_out_valid <= 1'b1;
                if (q_emit == rd_elast) begin
                    // Same entry, next consumer: re-send the SAME latched words
                    // with a different header, and no second AXI read.
                    if (e_dst < rd_nd) begin
                        e_dst  <= e_dst + 2'd1;
                        q_emit <= 2'd0;
                    end else begin
                        e_act <= 1'b0;
                    end
                end else begin
                    q_emit <= q_emit + 2'd1;
                end
            end

            // The captured entry moves to the emit buffer as the last flit of
            // the previous one leaves; after the emit above, so it wins e_act.
            if (handoff) begin
                e_w0 <= p_w0;
                e_w1 <= p_w1;
                e_w2 <= p_w2;
                e_w3 <= p_w3;
                e_tag  <= rd_txn + rd_ent;
                e_act  <= 1'b1;
                e_dst  <= 2'd0;
                q_emit <= 2'd0;
                q_rdy  <= p_done;
                rd_ent <= rd_ent + 8'd1;
                if (rd_final) begin
                    // A next run taken THIS cycle counts: IDLE would strand it.
                    rs <= (nx_v || take_nx) ? RS_NEXT : RS_IDLE;
                end else if (rd_stg) begin
                    // A staged run has no address in flight, so it steps here.
                    rd_cur   <= rd_anext;
                    rd_anext <= rd_anext + rd_ebytes;
                    stg_go   <= 1'b1;
                    rs <= RS_STG;
                end
            end
        end
    end

endmodule

`default_nettype wire

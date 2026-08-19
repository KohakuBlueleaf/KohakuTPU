// One MAG memory port: a NoC memory endpoint and the AXI master behind it.
//
//   NoC mem port ──► intake queues ──► read engine  ──► AXI AR/R
//                                  └─► write slots  ──► AXI AW/W/B
//
// WHY THIS IS A MODULE. The read engine fetches ONE entry at a time, so with a
// single instance every cluster in the partition queues behind one `rs` FSM and
// one emit buffer -- the constraint that stopped the machine scaling, and it
// stopped while nothing was saturated, so it was the server and not bandwidth.
// See docs/mas/spec.md s2.
//
// A port is therefore the unit the machine grows by: its own intake, read engine
// and quantiser, write slots and AXI channel, serving ~2 clusters.
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
    parameter integer STAGE_BANKS   = 4,      // 64 URAM, 2 MB
    parameter integer STAGE_ENTRIES = 16384,
    parameter integer STAGE_PIPE    = 1,
    parameter [1:0]   MESH_ID       = 2'd0
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

    localparam [3:0] T_MEM_RD_REQ  = 4'h0,
                     T_MEM_WR_REQ  = 4'h1,
                     T_MEM_RD_RESP = 4'h2,
                     T_MEM_WR_ACK  = 4'h3,
                     T_MEM_WR_DATA = 4'h4;

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
    assign mem_in_busy = (rq_cnt >= (Q_DEPTH - Q_MARGIN)) || rq_almost ||
                         (wq_cnt >= (Q_DEPTH - Q_MARGIN)) || wq_almost;

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
    wire       mi_wr = mi_take && ((mi_ty == T_MEM_WR_REQ) ||
                                   (mi_ty == T_MEM_WR_DATA));

    sync_fifo #(.DATA_WIDTH(FLIT_WIDTH), .FIFO_DEPTH(Q_DEPTH),
                .MEMORY_TYPE(MEM_TYPE))
    u_rdq (.clk(clk), .rst(!resetn),
           .wr_en(mi_rd), .wr_data(mem_in_data),
           .wr_busy(rq_full), .wr_almost(rq_almost),
           .rd_en(rq_pop), .rd_data(rq_flit), .rd_busy(rq_empty));

    sync_fifo #(.DATA_WIDTH(FLIT_WIDTH), .FIFO_DEPTH(Q_DEPTH),
                .MEMORY_TYPE(MEM_TYPE))
    u_wrq (.clk(clk), .rst(!resetn),
           .wr_en(mi_wr), .wr_data(mem_in_data),
           .wr_busy(wq_full), .wr_almost(wq_almost),
           .rd_en(wq_pop), .rd_data(wq_flit), .rd_busy(wq_empty));

    always @(posedge clk) begin
        if (!resetn) begin
            rq_cnt <= 0; wq_cnt <= 0;
        end else begin
            rq_cnt <= rq_cnt + (mi_rd ? 1'b1 : 1'b0)
                             - (rq_pop ? 1'b1 : 1'b0);
            wq_cnt <= wq_cnt + (mi_wr ? 1'b1 : 1'b0)
                             - (wq_pop ? 1'b1 : 1'b0);
        end
    end

    // ---- the read queue's head ------------------------------------------
    wire        in_valid = !rq_empty;
    wire [3:0]  in_ty  = `MP_HDR_TY(rq_flit);
    wire [POS_WIDTH-1:0] in_sx = `MP_SRC_X(rq_flit);
    wire [POS_WIDTH-1:0] in_sy = `MP_SRC_Y(rq_flit);
    // NOC_MEM_ADDR is 40 bits WHATEVER ADDR_W is -- a flit contract, not a width.
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
    //   [4] QUANT    the source is FP16; quantise it into MXFP7 operand words.
    //   [5] BLAYOUT  pack for a B operand rather than an A operand
    //   [6] STREAM   this is a DESCRIPTOR: fetch `count` consecutive entries
    wire in_quant  = in_flags[4];
    wire in_blay   = in_flags[5];
    wire in_stream = in_flags[6];

    // Entries in a streaming fetch. Contiguous by construction -- the driver
    // stores operands tile-major precisely so a pass's entries are one run
    // (docs/isa/kernel.md s3).
    wire [7:0] in_count = in_stream
                        ? ((rq_flit[199 -: 8] == 8'd0) ? 8'd1 : rq_flit[199 -: 8])
                        : 8'd1;

    // WORDS PER ENTRY, so a client whose line is not 128 bytes can stream too.
    // 0 KEEPS THE LEGACY 4, which is what every existing requester sends -- the
    // field is backward compatible by construction. A quantising read ignores
    // it: mx_quant yields four operand words whatever the source length.
    wire [7:0] in_ew_raw = rq_flit[165 -: 8];
    wire [2:0] in_ew = (in_quant || in_ew_raw == 8'd0 || in_ew_raw > 8'd4)
                     ? 3'd4 : in_ew_raw[2:0];

    // EXTRA DESTINATIONS. Every cluster sweeps the same rows of A, so without
    // this the quantiser runs once per CONSUMER rather than once per BYTE for a
    // bit-identical result. The requester is always a destination; these are
    // the others.
    wire [23:0] in_peer = rq_flit[191 -: 24];
    wire [1:0]  in_nd   = rq_flit[167 -: 2];

    // `st` is the WRITE machine plus the single-shot read that only benches
    // use. Streaming reads have their own state `rs` below and run alongside.
    //
    // The encodings are not contiguous, and that is load-bearing: benches watch
    // `st` by NUMBER, so a state is dropped by leaving a gap.
    reg [3:0]  st;
    localparam [3:0] S_IDLE = 4'd0,
                     S_RD_DATA = 4'd2,
                     S_WR_DATA = 4'd5, S_WR_ACK = 4'd6;

    // ================================================================
    // The read engine, with its own state and its own return context.
    //
    // Separate because a streaming fetch occupies the read path for the whole
    // run -- 32 entries, hundreds of cycles. Run inside `st`, S_IDLE never comes
    // round, so no write slot can be issued: the slots fill, intake jams on a
    // write descriptor nothing will accept, and the data flit behind it reports
    // "no open write". Lengthening one transaction starves the other two.
    //
    // DECLARED BEFORE THE QUANTISER, because `beat_valid` reads `rs`.
    // ================================================================
    localparam [1:0] RS_IDLE = 2'd0, RS_FILL = 2'd1, RS_WAIT = 2'd2,
                     RS_STG  = 2'd3;
    reg [1:0] rs;
    reg       rd_stg;                  // this run is served from staging

    // MESH FIRST, THEN APERTURE -- the same order mag_stage decodes in, and the
    // reason a packet only transiting this mesh is never claimed.
    function stg_is;
        input [ADDR_W-1:0] a;
        stg_is = (STAGE != 0) && a[39] && !a[38] &&
                 (a[37:36] == MESH_ID) && (a[35:32] == 4'h0);
    endfunction

    // NOT SERVABLE AT THIS PORT -- narrower than "undefined". mag_stage's
    // AP_IMPL is the architectural set; a memory port serves staging alone.
    // AP_DECODE, not STAGE: the drop holds wherever the store lives.
    function stg_unserved;
        input [ADDR_W-1:0] a;
        stg_unserved = (AP_DECODE != 0) && a[39] && !a[38] &&
                  ((a[37:36] != MESH_ID) || (a[35:32] != 4'h0));
    endfunction

    reg  [ADDR_W-1:0]     rd_cur;      // the entry being fetched from staging
    reg                   stg_go;
    wire [4*DATA_W-1:0]   stg_rdata;
    wire                  stg_rvalid, stg_b_gnt;

    reg [POS_WIDTH-1:0] rd_x, rd_y;
    reg [7:0]  rd_txn;
    // `rd_base` is where the run starts, `rd_cnt` how many entries it covers,
    // `rd_ent` which one is being fetched. Generated here because this module
    // already generates AXI burst addresses -- the same counter, one level up --
    // which removes a round trip per entry, not latency.
    reg [ADDR_W-1:0] rd_base;
    reg [7:0]  rd_cnt, rd_ent;
    // Source format for this run, off the REQUEST -- so no address map is held
    // here and none is needed.
    reg        rd_quant;
    // Entry geometry for this run. A legacy request reproduces exactly what the
    // Q_/P_ localparams used to hardcode.
    reg [ADDR_W-1:0] rd_ebytes;
    reg [1:0]  rd_elast;    // last word index within an entry
    // The next entry's address, ACCUMULATED. Computing base + (ent+1)*ebytes
    // instead cost 86 MHz: against the old localparams that product was a
    // constant multiply, i.e. a shift, and against a register it is a real
    // full-width multiplier sitting in the AR address path.
    reg [ADDR_W-1:0] rd_anext;
    // A pre-quantised entry is four beats that ARE the four operand words.
    // They land here rather than in the emit buffer directly, because the
    // emitter may still be handing out the previous entry.
    (* EXTRACT_RESET = "no" *) reg [255:0] p_w0, p_w1, p_w2, p_w3;
    reg [1:0]   p_cnt;

    // Emit buffer: the finished entry, latched so the NEXT entry's AXI read can
    // start immediately. Without it the quantiser's output IS the emit source,
    // so fetch and emit exclude each other and two independent interfaces run at
    // the sum of their times instead of the larger.
    (* EXTRACT_RESET = "no" *) reg [255:0] e_w0, e_w1, e_w2, e_w3;
    reg [7:0]   e_tag;
    reg         e_act;
    reg [1:0]   e_dst;
    reg [23:0]  rd_peer;
    reg [1:0]   rd_nd;

    // Destination 0 is the requester itself; the rest come from the list. A
    // node index is {y,x} with y in the high nibble -- the packing PROG_DST and
    // NODE_STATUS use.
    wire [7:0] e_peer_sel = (e_dst == 2'd1) ? rd_peer[7:0]
                          : (e_dst == 2'd2) ? rd_peer[15:8]
                                            : rd_peer[23:16];
    wire [POS_WIDTH-1:0] e_dx = (e_dst == 2'd0) ? rd_x : e_peer_sel[POS_WIDTH-1:0];
    wire [POS_WIDTH-1:0] e_dy = (e_dst == 2'd0) ? rd_y : e_peer_sel[4 +: POS_WIDTH];
    // `q_done` is a one-cycle pulse. If the emit buffer is still busy when it
    // fires, the entry has to be remembered or the read engine waits forever
    // for an edge that already happened.
    reg         q_rdy;

    // ---- FP16 -> MXFP7, so software never sees the internal format --------
    // One invocation converts one L1 entry: 8 AXI beats in (4 lanes x 32 FP16 =
    // 2048 bit), 4 operand words out (1024 bit). The block scale is shared along
    // K, so nothing can be emitted until the whole entry has arrived.
    //
    // ONE PER PORT: the quantiser is what every cluster behind a port contends
    // for, which is the point of having ports.
    reg          q_start, q_blay;
    wire         q_done;
    wire [255:0] q_w0, q_w1, q_w2, q_w3;
    reg  [1:0]   q_emit;

    // The R channel crossed uncut both ways: 85% of m62_c1's failing paths were
    // 7-11 levels at 90% route. sb_skid's i_ready does not depend on o_ready.
    wire [DATA_W-1:0] r_data;
    wire              r_last, r_valid, r_ready;

    sb_skid #(.W(DATA_W + 1)) u_rskid (
        .clk(clk), .rst(!resetn),
        .i_valid(m_rvalid), .i_ready(m_rready), .i_data({m_rlast, m_rdata}),
        .o_valid(r_valid), .o_ready(r_ready), .o_data({r_last, r_data})
    );

    // The read FIFO's BRAM output reached this quantiser's DSP control through
    // 9 LUT levels, 4.399 ns, and set the WNS on every SLR1 probe.
    reg          q_beat_v;
    reg  [255:0] q_beat;
    always @(posedge clk) begin
        q_beat <= r_data;
        if (!resetn) q_beat_v <= 1'b0;
        else q_beat_v <= r_ready && r_valid && (rs == RS_FILL) && rd_quant;
    end

    mx_quant u_quant (
        .clk(clk), .rst(!resetn),
        .start(q_start),
        .b_layout(q_blay),
        .beat(q_beat),
        .beat_valid(q_beat_v),
        .need_beat(), .done(q_done),
        .word0(q_w0), .word1(q_w1), .word2(q_w2), .word3(q_w3)
    );

    // One L1 entry is 4 lanes x 32 elements whatever the bus is wide: 2048 bits
    // as FP16, 1024 as MXFP7. Derived, not written as 7 and 3 -- hardcoding them
    // makes a wider bus a silent correctness change rather than a parameter.
    localparam integer Q_ENTRY_BITS  = 2048;
    localparam integer P_ENTRY_BITS  = 1024;
    localparam [ADDR_W-1:0] Q_ENTRY_BYTES = Q_ENTRY_BITS / 8;         // 256
    localparam [ADDR_W-1:0] P_ENTRY_BYTES = P_ENTRY_BITS / 8;         // 128
    localparam [7:0]   Q_ARLEN       = Q_ENTRY_BITS / DATA_W - 1;     // 7 at 256b
    localparam [7:0]   P_ARLEN       = P_ENTRY_BITS / DATA_W - 1;     // 3 at 256b

    // Declared HERE, not beside `in_ew`: it reads Q_ENTRY_BYTES, and xvlog
    // rejects the forward reference that synthesis had accepted silently.
    wire [ADDR_W-1:0] in_ebytes =
        in_quant ? Q_ENTRY_BYTES : ({{(ADDR_W-3){1'b0}}, in_ew} << LSB);

    // The WRITE path's own return context. Shared with the read path, a read and
    // a write cannot overlap despite using disjoint AXI channels.
    reg [POS_WIDTH-1:0] rq_x, rq_y;
    reg [7:0]  rq_txn;
    reg [15:0] n_rd, n_wr;

    // Output arbitration, in one place. The emitter and the `st` machine both
    // produce response flits into one output register, so exactly one may drive
    // it per cycle. The emitter wins and cannot starve the other: four flits per
    // entry against a fetch of at least Q_ARLEN+1 beats leaves most cycles free.
    //
    // `out_free` covers the cycle a flit is being ACCEPTED, not just an empty
    // register: waiting for the register to read empty halves the response rate.
    wire out_free = !mem_out_valid || !mem_out_busy;
    wire emit_go  = e_act && out_free;
    wire st_out   = out_free && !emit_go;

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
    reg                  ws_rdy  [0:WR_SLOTS-1];   // data complete, ready for AXI
    reg                  ws_iss  [0:WR_SLOTS-1];   // issued to AXI, awaiting ack
    reg [POS_WIDTH-1:0]  ws_x    [0:WR_SLOTS-1], ws_y [0:WR_SLOTS-1];
    reg [7:0]            ws_txn  [0:WR_SLOTS-1];
    reg [ADDR_W-1:0]     ws_addr [0:WR_SLOTS-1];
    reg                  ws_stg  [0:WR_SLOTS-1];   // lands in staging, not DRAM
    reg                  ws_bad  [0:WR_SLOTS-1];   // reserved aperture: goes nowhere
    reg [WBW:0]          ws_len  [0:WR_SLOTS-1];   // beats expected
    reg [WBW:0]          ws_cnt  [0:WR_SLOTS-1];   // beats received
    reg [DATA_W-1:0]     ws_data [0:WR_SLOTS*WBURST-1];

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
    reg [WS_BITS-1:0] ws_free, ws_match, ws_pick;
    reg               ws_has_free, ws_has_match, ws_has_pick;

    always @(*) begin
        ws_free = {WS_BITS{1'b0}};  ws_has_free  = 1'b0;
        ws_match = {WS_BITS{1'b0}}; ws_has_match = 1'b0;
        ws_pick = {WS_BITS{1'b0}};  ws_has_pick  = 1'b0;
        for (wi = WR_SLOTS-1; wi >= 0; wi = wi - 1) begin
            if (!ws_val[wi]) begin
                ws_free = wi[WS_BITS-1:0]; ws_has_free = 1'b1;
            end
            // !ws_iss: a slot on the bus is NOT waiting for data, however much
            // its val/rdy pair looks like it is.
            if (ws_val[wi] && !ws_rdy[wi] && !ws_iss[wi] &&
                (ws_x[wi] == wi_sx) && (ws_y[wi] == wi_sy)) begin
                ws_match = wi[WS_BITS-1:0]; ws_has_match = 1'b1;
            end
            if (ws_val[wi] && ws_rdy[wi]) begin
                ws_pick = wi[WS_BITS-1:0]; ws_has_pick = 1'b1;
            end
        end
    end

    // One flit leaves each queue per cycle: a write descriptor when a slot is
    // free, its data when the slot is waiting for it, or a read when the engine
    // can take one. Anything else is simply not popped and waits.
    wire take_wr_req  = wi_valid && (wi_ty == T_MEM_WR_REQ)  && ws_has_free;
    wire take_wr_data = wi_valid && (wi_ty == T_MEM_WR_DATA) && ws_has_match;

    // Reads split by kind, because they live in different machines. An ENTRY
    // read -- streaming, or quantised, or both -- is taken by the read engine
    // and needs only the AR channel free. A single-shot raw read still runs
    // inside `st` and returns beats verbatim; benches use it. Both must not be
    // in flight at once, since there is one AXI read channel.
    wire rd_free      = (rs == RS_IDLE) && !e_act;
    wire take_rd_e    = in_valid && (in_ty == T_MEM_RD_REQ) &&
                        (in_stream || in_quant) &&
                        rd_free && (st != S_RD_DATA);
    wire take_rd_p    = in_valid && (in_ty == T_MEM_RD_REQ) &&
                        !in_stream && !in_quant &&
                        rd_free && (st == S_IDLE);
    wire take_rd      = take_rd_e || take_rd_p;

    // A picked slot stops being pickable IMMEDIATELY, not at its write ack:
    // releasing at ack leaves it ready for the cycle the FSM spends re-entering
    // S_IDLE, so the same write is issued twice and the next write's turn never
    // comes. Only the PLAIN read competes here -- a quantised read has its own
    // engine and AXI channel, and making writes wait for it would reintroduce
    // the starvation that splitting them fixed.
    wire ws_issue = (st == S_IDLE) && ws_has_pick && !take_rd_p;

    always @(*) wq_pop = take_wr_req || take_wr_data;
    always @(*) rq_pop = take_rd;

    // ---- the staging store, aperture 0 --------------------------------------
    // Port A is the fill path, entry-wide; port B takes a staged drain's beats.
    generate if (STAGE != 0) begin : g_stage
        mag_stage #(.DATA_W(DATA_W), .ADDR_W(ADDR_W), .WORDS(4),
                    .BANKS(STAGE_BANKS), .ENTRIES(STAGE_ENTRIES),
                    .PIPE(STAGE_PIPE), .MESH_ID(MESH_ID)) u_stage (
            .clk(clk), .rst(!resetn),
            .a_req(stg_go), .a_we(1'b0), .a_addr(rd_cur),
            .a_wdata({(4*DATA_W){1'b0}}),
            .a_mine(), .a_gnt(), .a_fault(),
            .a_rvalid(stg_rvalid), .a_rdata(stg_rdata),
            .b_req((st == S_WR_DATA) && wr_stg), .b_we(1'b1),
            .b_addr(m_awaddr + {{(ADDR_W-WBW-1-LSB){1'b0}}, wb_cnt, {LSB{1'b0}}}),
            .b_wdata(ws_data[{ws_cur, wb_cnt[WBW-1:0]}]),
            .b_mine(), .b_gnt(stg_b_gnt), .b_rvalid(), .b_rdata()
        );
    end else begin : g_nostage
        assign stg_rvalid = 1'b0;
        assign stg_rdata  = {(4*DATA_W){1'b0}};
        assign stg_b_gnt  = 1'b0;
    end endgenerate

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (resetn && in_valid && (in_ty == T_MEM_RD_REQ) && stg_unserved(in_addr))
            $display("%0t ERROR mag_mem_port(mesh %0d, %0d,%0d):read at %h names a reserved aperture or another mesh -- DROPPED rather than aliased onto DRAM, so the requester will hang",
                     $time, MESH_ID, MEM_X, MEM_Y, in_addr);
        if (resetn && wi_valid && (wi_ty == T_MEM_WR_REQ) && stg_unserved(wi_addr))
            $display("%0t ERROR mag_mem_port(mesh %0d, %0d,%0d):write at %h names a reserved aperture or another mesh -- DROPPED rather than aliased onto DRAM, so the drain will never ack",
                     $time, MESH_ID, MEM_X, MEM_Y, wi_addr);
        // THE DROP ITSELF, named at the moment it happens. Without this the
        // first symptom is "write data with no open write" hundreds of cycles
        // later and several modules away.
        if (resetn && ((mi_rd && rq_full) || (mi_wr && wq_full)))
            $display("%0t ERROR mag_mem_port(mesh %0d, %0d,%0d):input flit DROPPED -- backpressure is too late",
                     $time, MESH_ID, MEM_X, MEM_Y);
        if (resetn && take_wr_req)
            $display("  PROBE %0t mesh %0d slot %0d opened by (%0d,%0d) addr %h len %0d",
                     $time, MESH_ID, ws_free, wi_sx, wi_sy, wi_addr, wi_len);
        if (resetn && wi_valid && (wi_ty == T_MEM_WR_DATA) && ws_has_match)
            $display("  PROBE %0t mesh %0d slot %0d took beat from (%0d,%0d)",
                     $time, MESH_ID, ws_match, wi_sx, wi_sy);
        if (resetn && wi_valid && (wi_ty == T_MEM_WR_DATA) && !ws_has_match)
            $display("%0t ERROR mag_mem_port(mesh %0d, %0d,%0d):write data from (%0d,%0d) with no open write",
                     $time, MESH_ID, MEM_X, MEM_Y, wi_sx, wi_sy);
    end
`endif

    // S_RD_DATA turns each AXI beat straight into a response flit, so it may
    // take a beat only when the output register is free -- accepting one
    // unconditionally overwrites a flit the NoC has not taken and the beat is
    // gone. RS_FILL is safe unconditionally: it buffers into the quantiser or
    // p_w0..3 and emits later, behind the emit buffer's own guard.
    assign r_ready    = ((st == S_RD_DATA) && st_out) ||
                        ((rs == RS_FILL) && !q_start);

    // ---- write intake, independent of the service FSM --------------------
    reg [WS_BITS-1:0] ws_cur;      // slot being written to AXI
    reg               ws_done;     // one-cycle release
    reg [WBW:0]       wb_cnt;      // beat within the burst on the bus
    reg [WBW:0]       wb_len;      // beats in it
    reg               wr_stg;      // the write on the bus is staged
    reg               wr_bad;      // ... or names a reserved aperture
    wire [WBW:0]      wb_nxt = wb_cnt + 1'b1;

    // The NoC write path's B latch. m_bready is tied high, so the slave's
    // response is consumed the cycle it appears whether or not S_WR_ACK can act
    // on it -- and often it cannot, the read emitter owning the output register.
    // A missed B never comes again.
    reg wr_b;

    integer wj;
    always @(posedge clk) begin
        if (!resetn) begin
            for (wj = 0; wj < WR_SLOTS; wj = wj + 1) begin
                ws_val[wj] <= 1'b0; ws_rdy[wj] <= 1'b0; ws_iss[wj] <= 1'b0;
                ws_len[wj] <= 1;    ws_cnt[wj] <= 0;
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
                // `len` is beats-minus-one. A requester that sends a single
                // beat writes 0 and gets exactly the old behaviour.
                ws_len[ws_free]  <= wi_len[WBW:0] + 1'b1;
                ws_cnt[ws_free]  <= 0;
            end
            if (take_wr_data) begin
                ws_data[{ws_match, ws_cnt[ws_match][WBW-1:0]}] <= wq_flit[DATA_W-1:0];
                ws_cnt[ws_match] <= ws_cnt[ws_match] + 1'b1;
                // ready only when the WHOLE burst has landed
                if (ws_fill_now[ws_match]) ws_rdy[ws_match] <= 1'b1;
            end
            // picked: not pickable again. freed only once the write is acked,
            // so the source cannot reuse the slot before its data is safe.
            if (ws_issue) begin
                ws_rdy[ws_pick] <= 1'b0;
                ws_iss[ws_pick] <= 1'b1;
            end
            if (ws_done) begin
                ws_val[ws_cur] <= 1'b0;
                ws_iss[ws_cur] <= 1'b0;
            end
        end
    end

    always @(posedge clk) begin
        if (!resetn) begin
            st <= S_IDLE;
            m_awvalid <= 1'b0; m_wvalid <= 1'b0; m_arvalid <= 1'b0;
            m_awlen <= 8'd0; m_arlen <= 8'd0; m_wlast <= 1'b0;
            m_awaddr <= {ADDR_W{1'b0}}; m_araddr <= {ADDR_W{1'b0}};
            m_awid <= {ID_W{1'b0}}; m_arid <= {ID_W{1'b0}};
            mem_out_valid <= 1'b0;
            rq_x <= 0; rq_y <= 0; rq_txn <= 0;
            n_rd <= 16'd0; n_wr <= 16'd0;
            wr_b <= 1'b0;
            q_start <= 1'b0; q_blay <= 1'b0; q_emit <= 2'd0;
            ws_cur <= {WS_BITS{1'b0}}; ws_done <= 1'b0;
            wb_cnt <= 0; wb_len <= 0; wr_stg <= 1'b0; wr_bad <= 1'b0;
            rd_stg <= 1'b0; stg_go <= 1'b0; rd_cur <= {ADDR_W{1'b0}};
            rs <= RS_IDLE; q_rdy <= 1'b0; e_act <= 1'b0; e_tag <= 8'd0;
            rd_x <= 0; rd_y <= 0; rd_txn <= 0;
            rd_base <= {ADDR_W{1'b0}}; rd_cnt <= 8'd1; rd_ent <= 8'd0;
            rd_peer <= 24'd0; rd_nd <= 2'd0; e_dst <= 2'd0;
            rd_quant <= 1'b1; p_cnt <= 2'd0;
            rd_ebytes <= P_ENTRY_BYTES; rd_elast <= 2'd3;
            rd_anext  <= {ADDR_W{1'b0}};
            // m_wdata, mem_out_data, p_w* and e_w* are payload: m_wvalid,
            // mem_out_valid, e_act/q_rdy and `st` qualify them, all reset above.
        end else begin
            ws_done <= 1'b0;
            if (mem_out_valid && !mem_out_busy) mem_out_valid <= 1'b0;
            if (m_awvalid && m_awready) m_awvalid <= 1'b0;
            if (m_arvalid && m_arready) m_arvalid <= 1'b0;
            // Catch the B response the cycle it appears, whatever else is
            // happening. Cleared by S_WR_ACK below, which runs later in this
            // block, so a same-cycle arrive-and-consume ends correctly at 0.
            if (m_bvalid && ((st == S_WR_DATA) || (st == S_WR_ACK)))
                wr_b <= 1'b1;

            case (st)
            // ---------------------------------------------------------
            S_IDLE: begin
                if (take_rd_p) begin
                    rq_x <= in_sx; rq_y <= in_sy;
                    rq_txn <= in_txn;
                    m_araddr  <= in_addr[ADDR_W-1:0];
                    m_arlen   <= in_len;
                    m_arid    <= {ID_W{1'b0}};
                    m_arvalid <= 1'b1;
                    n_rd <= n_rd + 16'd1;
                    st <= S_RD_DATA;
                end else if (ws_issue) begin
                    // a reassembled write is complete: put it on AXI. Reads win
                    // above because a stalled read stalls a cluster, while a
                    // queued write has already been accepted.
                    ws_cur    <= ws_pick;
                    rq_x      <= ws_x[ws_pick];
                    rq_y      <= ws_y[ws_pick];
                    rq_txn    <= ws_txn[ws_pick];
                    m_awaddr  <= ws_addr[ws_pick][ADDR_W-1:0];
                    m_awlen   <= {{(8-WBW-1){1'b0}}, (ws_len[ws_pick] - 1'b1)};
                    m_awid    <= {ID_W{1'b0}};
                    // A staged write never reaches AXI: no AW, no W, no B. Nor
                    // does one naming a reserved aperture -- it is dropped.
                    m_awvalid <= !ws_stg[ws_pick] && !ws_bad[ws_pick];
                    wr_stg    <= ws_stg[ws_pick];
                    wr_bad    <= ws_bad[ws_pick];
                    wb_cnt    <= 0;
                    wb_len    <= ws_len[ws_pick];
                    n_wr <= n_wr + 16'd1;
                    st <= S_WR_DATA;
                end
            end

            // ---------------- NoC read: AXI beats -> MEM_RD_RESP flits
            S_RD_DATA: if (r_valid && r_ready) begin
                mem_out_data <= { rq_x, rq_y,
                                  MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                                  T_MEM_RD_RESP, rq_txn, r_last, 3'b000,
                                  r_data };
                mem_out_valid <= 1'b1;
                if (r_last) st <= S_IDLE;
            end

            // ---------------- NoC write: one reassembled slot -> AXI
            // The data came from the slot, matched to its descriptor by source,
            // so nothing here depends on flit arrival order. The beat COUNTER,
            // not the flit stream, decides where the burst ends: a requester
            // that miscounts its own data must not desynchronise the response.
            // Dropped, and the slot freed rather than wedged: no ack, so the
            // requester hangs loudly instead of being told a lie.
            S_WR_DATA: if (wr_bad) begin
                ws_done <= 1'b1;
                st      <= S_IDLE;
            end else if (wr_stg) begin
                // One word per beat through port B, which is the granularity
                // the host window uses and needs no burst.
                if (stg_b_gnt) begin
                    wb_cnt <= wb_nxt;
                    if (wb_nxt == wb_len) begin
                        wr_b <= 1'b1;
                        st   <= S_WR_ACK;
                    end
                end
            end else begin
                if (!m_wvalid) begin
                    m_wdata  <= ws_data[{ws_cur, wb_cnt[WBW-1:0]}];
                    m_wlast  <= (wb_nxt == wb_len);
                    m_wvalid <= 1'b1;
                end else if (m_wready) begin
                    if (m_wlast) begin
                        m_wvalid <= 1'b0; m_wlast <= 1'b0;
                        st <= S_WR_ACK;
                    end else begin
                        // The NEXT beat's data, indexed by the next count --
                        // reading ws_data at the current count here would
                        // resend beat 0 for the whole burst.
                        m_wdata <= ws_data[{ws_cur, wb_nxt[WBW-1:0]}];
                        m_wlast <= (wb_nxt + 1'b1 == wb_len);
                        wb_cnt  <= wb_nxt;
                    end
                end
            end
            S_WR_ACK: if ((m_bvalid || wr_b) && st_out) begin
                wr_b <= 1'b0;
                mem_out_data <= { rq_x, rq_y,
                                  MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                                  T_MEM_WR_ACK, rq_txn, 1'b1, 3'b000,
                                  {(FLIT_WIDTH-4*POS_WIDTH-16){1'b0}} };
                mem_out_valid <= 1'b1;
                ws_done <= 1'b1;        // release the slot for its next write
                st <= S_IDLE;
            end

            default: st <= S_IDLE;
            endcase

            // ============ the read engine, running alongside ================
            // Same always block as `st` because both drive the one output
            // register; separate state because making one wait for the other is
            // what starved the write path.
            if (q_done) q_rdy <= 1'b1;

            case (rs)
            RS_IDLE: if (take_rd_e) begin
                rd_x <= in_sx; rd_y <= in_sy; rd_txn <= in_txn;
                rd_peer <= in_peer; rd_nd <= in_nd;
                rd_base <= in_addr;
                rd_cnt  <= in_count;            // 1 unless STREAM is set
                rd_ent  <= 8'd0;
                rd_quant <= in_quant;
                rd_ebytes <= in_ebytes;
                rd_anext  <= in_addr + in_ebytes;
                rd_elast  <= in_ew[1:0] - 2'd1;
                p_cnt    <= 2'd0;
                rd_cur   <= in_addr;
                q_blay  <= in_blay;
                n_rd <= n_rd + 16'd1;
                // Staging holds operand words verbatim, so it answers only a
                // non-quantising fetch; a QUANT read of it belongs to DRAM.
                if (stg_is(in_addr) && !in_quant) begin
                    rd_stg  <= 1'b1;
                    stg_go  <= 1'b1;
                    q_start <= 1'b0;
                    rs <= RS_STG;
                end else if (stg_unserved(in_addr)) begin
                    rd_stg <= 1'b0;
                    rs <= RS_IDLE;      // dropped, so it hangs instead of lying
                end else begin
                    rd_stg    <= 1'b0;
                    m_araddr  <= in_addr[ADDR_W-1:0];
                    m_arlen   <= in_quant ? Q_ARLEN : ({5'd0, in_ew} - 8'd1);
                    m_arid    <= {ID_W{1'b0}};
                    m_arvalid <= 1'b1;
                    q_start <= in_quant;
                    rs <= RS_FILL;
                end
            end
            // One entry per port-A read, which is what the 1,024-bit line is
            // for: no AR, no beats, no quantiser.
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
            // Issue the NEXT entry's address the moment this one's last beat
            // lands, not after the quantiser has finished with it. The returning
            // data runs ahead only as far as the R skid's two entries, which
            // this overlaps only the AR-to-first-beat latency, which would
            // otherwise be paid once per entry.
            RS_FILL: begin
                q_start <= 1'b0;
                // A pre-quantised beat IS an operand word. Captured here, not
                // written to the emit buffer directly, so the fetch never has
                // to wait for the previous entry to finish leaving.
                if (!rd_quant && r_valid && r_ready) begin
                    case (p_cnt)
                        2'd0:    p_w0 <= r_data;
                        2'd1:    p_w1 <= r_data;
                        2'd2:    p_w2 <= r_data;
                        default: p_w3 <= r_data;
                    endcase
                    p_cnt <= p_cnt + 2'd1;
                    if (r_last) q_rdy <= 1'b1;
                end
                if (r_valid && r_ready && r_last) begin
                    rs <= RS_WAIT;
                    if (rd_ent + 8'd1 < rd_cnt) begin
                        m_araddr  <= rd_anext[ADDR_W-1:0];
                        rd_anext  <= rd_anext + rd_ebytes;
                        m_arlen   <= rd_quant ? Q_ARLEN : {6'd0, rd_elast};
                        m_arvalid <= 1'b1;
                        n_rd      <= n_rd + 16'd1;
                    end
                end
            end
            // Hand the finished entry to the emit buffer and start the NEXT
            // fetch in the SAME cycle: the AXI read of entry n+1 and the NoC
            // emit of entry n use different wires, and sharing the quantiser's
            // output registers is the only thing that would serialise them.
            RS_WAIT: if (q_rdy && !e_act) begin
                e_w0 <= rd_quant ? q_w0 : p_w0;
                e_w1 <= rd_quant ? q_w1 : p_w1;
                e_w2 <= rd_quant ? q_w2 : p_w2;
                e_w3 <= rd_quant ? q_w3 : p_w3;
                e_tag  <= rd_txn + rd_ent;
                e_act  <= 1'b1;
                e_dst  <= 2'd0;
                q_emit <= 2'd0;
                q_rdy  <= 1'b0;
                // The address was already issued in RS_FILL; this only arms the
                // quantiser for the entry whose beats are already waiting.
                if (rd_ent + 8'd1 < rd_cnt) begin
                    rd_ent  <= rd_ent + 8'd1;
                    q_start <= rd_quant && !rd_stg;
                    p_cnt   <= 2'd0;
                    // A staged run has no address in flight -- RS_FILL issues
                    // the next AR early, RS_STG cannot -- so step it here.
                    if (rd_stg) begin
                        rd_cur   <= rd_anext;
                        rd_anext <= rd_anext + rd_ebytes;
                        stg_go   <= 1'b1;
                        rs <= RS_STG;
                    end else rs <= RS_FILL;
                end else rs <= RS_IDLE;
            end
            default: rs <= RS_IDLE;
            endcase

            // A RESPONSE SAYS WHERE IT BELONGS. `e_tag` is the requester's own
            // entry index (the txn it sent, plus this entry's position in the
            // run) and the two spare header bits carry the word within the
            // entry, so the receiver needs no cursor and arrival order stops
            // being load-bearing. That is what makes a streaming fetch possible.
            if (emit_go) begin
                mem_out_data <= { e_dx, e_dy,
                                  MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                                  T_MEM_RD_RESP, e_tag, (q_emit == rd_elast),
                                  1'b0, q_emit,
                                  (q_emit == 2'd0) ? e_w0 :
                                  (q_emit == 2'd1) ? e_w1 :
                                  (q_emit == 2'd2) ? e_w2 : e_w3 };
                mem_out_valid <= 1'b1;
                if (q_emit == rd_elast) begin
                    // Same entry, next consumer: re-send the SAME latched words
                    // with a different header. No second AXI read and no second
                    // pass of the quantiser.
                    if (e_dst < rd_nd) begin
                        e_dst  <= e_dst + 2'd1;
                        q_emit <= 2'd0;
                    end else e_act <= 1'b0;
                end else q_emit <= q_emit + 2'd1;
            end
        end
    end

endmodule

`default_nettype wire

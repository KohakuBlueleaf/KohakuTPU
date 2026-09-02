// A layout, gather and fill engine with its own AXI master. Two mx_tdesc
// walkers: the DESTINATION defines the iteration space, so stride 0 broadcasts.

// src_valid low injects MV_IMM, which is how padding works; dst_valid low
// suppresses the write. Word granular, so a misaligned destination faults.

// An ISSUE engine folds consecutive addresses into bursts and keeps k reads in
// flight; a WRITE engine drains the FIFO behind it and never waits for B.

// MODE_XFORM puts the transform slot ON THE READ-RETURN PATH, between R and the
// FIFO, so one pass is mem -> occupant -> mem with the walker feeding the
// occupant directly. An entry is IN_BEATS source words in, OUT_WORDS out; the
// SOURCE walker defines the iteration space and the destination steps once per
// entry. The reservation is OUT_WORDS per entry, taken before the entry's ARs.

// mag_dram_port's return path is shared, so a burst's FIFO space is reserved
// before its AR and its AW waits until the data is resident.

// WRITE COALESCING IS flags[3], off by default and now SAFE everywhere:
// mag_ilink's splitter streams a burst since it learned s_awlen. Worth 7.2x.

// 40-bit map: [39] aperture, [38] rsvd, [37:36] mesh, [35:0] local. Nothing here
// masks the top four, so an aperture descriptor reaches MAG's L2 unchanged.

// THE 4 KB RULE ALREADY SPLITS AT THE MESH BOUNDARY. A mesh is 64 GB and a burst
// may not leave its 4 KB page, so no coalesced burst can straddle bit 36.

`default_nettype none

module mm_mover #(
    parameter integer DATA_W    = 256,
    parameter integer ADDR_W    = 40,
    parameter integer ID_W      = 4,
    // 8 indices per word. 256, not 128: a 256-bit port is 4 RAMB36 at any depth
    // to 512, and at 128 ix_wr_a's top bit was dropped silently (Synth 8-689).
    parameter integer IDX_WORDS = 256,
    // Read data staged between the two engines. 512 x 256 b is 4 RAMB36, the
    // same tile count as any shallower depth, and covers MAX_OUT full bursts.
    parameter integer FIFO_D    = 512,
    // Write commands queued ahead. With coalescing off a command is one word,
    // so at 16 the read round trip was re-exposed once every 16 words.
    parameter integer CMD_D     = 128,
    parameter integer MAX_OUT   = 16,       // read bursts in flight
    // Write bursts awaiting B. At 8 this alone held single-beat writes to
    // 6.8 cycles a word against a 50-cycle write-to-B; the slave throttles.
    parameter integer MAX_WOUT  = 32,
    // 4 KB / 32 B. The AXI4 boundary rule makes any longer burst illegal.
    parameter integer BURST_MAX = 128,
    // The transform slot. OUT_WORDS is at most 4: the bank presents word0..word3.
    parameter integer XID_W     = 4,
    parameter integer XMODE_W   = 4,
    parameter integer XF_IN_BITS   = 2048,
    parameter integer XF_OUT_WORDS = 4
)(
    input  wire                clk,
    input  wire                resetn,

    // ---- configuration: 64-bit register writes ---------------------------
    input  wire                cfg_en,
    input  wire [7:0]          cfg_addr,
    input  wire [63:0]         cfg_data,

    output wire                stat_busy,
    output reg  [3:0]          stat_fault,
    output reg  [31:0]         stat_done,

    // ---- AXI4 master -----------------------------------------------------
    output reg  [ID_W-1:0]     m_awid,
    output reg  [ADDR_W-1:0]   m_awaddr,
    output reg  [7:0]          m_awlen,
    output wire [2:0]          m_awsize,
    output wire [1:0]          m_awburst,
    output reg                 m_awvalid,
    input  wire                m_awready,
    output wire [DATA_W-1:0]   m_wdata,
    output wire [DATA_W/8-1:0] m_wstrb,
    output wire                m_wlast,
    output wire                m_wvalid,
    input  wire                m_wready,
    input  wire [ID_W-1:0]     m_bid,
    input  wire [1:0]          m_bresp,
    input  wire                m_bvalid,
    output wire                m_bready,
    output wire [ID_W-1:0]     m_arid,
    output wire [ADDR_W-1:0]   m_araddr,
    output wire [7:0]          m_arlen,
    output wire [2:0]          m_arsize,
    output wire [1:0]          m_arburst,
    output wire                m_arvalid,
    input  wire                m_arready,
    input  wire [ID_W-1:0]     m_rid,
    input  wire [DATA_W-1:0]   m_rdata,
    input  wire [1:0]          m_rresp,
    input  wire                m_rlast,
    input  wire                m_rvalid,
    output wire                m_rready,

    // ---- the transform slot, on the read-return path ---------------------
    output reg                 x_req,
    input  wire                x_gnt,
    output reg                 x_start,
    output reg  [XID_W-1:0]    x_id,
    output reg  [XMODE_W-1:0]  x_mode,
    output reg  [DATA_W-1:0]   x_beat,
    output reg                 x_beat_valid,
    input  wire                x_done,
    input  wire [DATA_W-1:0]   x_w0, x_w1, x_w2, x_w3
);
    localparam [2:0] MODE_COPY = 3'd0, MODE_TRANSPOSE = 3'd1;
    localparam [2:0] MODE_GATHER = 3'd2, MODE_GENERATE = 3'd3, MODE_FILL = 3'd4;
    localparam [2:0] MODE_XFORM = 3'd5;

    localparam [3:0] F_NONE = 4'd0, F_IDXLEN = 4'd1, F_RANGE = 4'd2;
    localparam [3:0] F_AXI = 4'd3, F_MODE = 4'd4, F_EWIDTH = 4'd5;
    localparam [3:0] F_ALIGN = 4'd6, F_XPAD = 4'd7;

    // One entry: IN_BEATS source words in, XF_OUT_WORDS destination words out.
    localparam integer IN_BEATS = XF_IN_BITS / DATA_W;
    localparam [8:0]   OUT_W9   = XF_OUT_WORDS[8:0];

    // The issue engine. I_GA1..3 are the gather address pipeline and I_LAT is
    // the element latch; everything else walks one element per cycle in I_RUN.
    localparam [3:0] I_IDLE = 4'd0, I_IXA = 4'd1, I_IXD = 4'd2, I_IXW = 4'd3;
    localparam [3:0] I_GO = 4'd4, I_GA1 = 4'd5, I_GA2 = 4'd6, I_GA3 = 4'd7;
    localparam [3:0] I_LAT = 4'd8, I_RUN = 4'd9, I_FLUSH = 4'd10;
    localparam [3:0] I_DRAIN = 4'd11, I_DONE = 4'd12, I_FAULT = 4'd13;
    // One cycle between the walkers' start and the element latch: their
    // queues (mx_tdesc OREG) hold element 0 from the start and need the cycle
    // to step past it, so element 1 is there when I_LAT pops.
    localparam [3:0] I_GO2 = 4'd14;

    localparam [1:0] W_IDLE = 2'd0, W_ARM = 2'd1, W_GEN = 2'd2, W_DATA = 2'd3;

    // What a destination element's data comes from. K_SKIP is a suppressed
    // write: no read, no command, and it breaks both runs.
    localparam [1:0] K_RD = 2'd0, K_FILL = 2'd1, K_GEN = 2'd2, K_SKIP = 2'd3;

    localparam integer CNT_W = 16;
    localparam integer CMD_W = ADDR_W + 10;

    // ADDR_W IS 40 AND SAYS SO BY NAME. Not a floor: the map is ABSOLUTE -- [39]
    // aperture, [37:36] mesh -- so a narrower build is a DIFFERENT map, not the

    // bottom corner of this one, and mag_ilink's mesh decode slides with it.
    // Out of range the failure was a part-select naming neither cause nor bound.
    generate
    if (ADDR_W != 40) begin : g_addr_w
        mm_mover_ADDR_W_must_be_40_the_address_map_is_absolute u_bad_param ();
    end
    endgenerate

    // ================================================== configuration state
    reg [2:0]  mode;
    reg [1:0]  ewidth;
    reg [7:0]  flags;
    reg [63:0] seed;
    reg [31:0] imm;
    reg [XID_W-1:0]   xf_id;
    reg [XMODE_W-1:0] xf_mode;
    // Declared here, not beside its reader: a wire used before its declaration
    // elaborates cleanly in some tools and as a 1-bit net in others.
    reg [ADDR_W-1:0] idx_base, dst_base, d_src_base;
    reg [15:0] idx_count;

    reg [31:0] gath_pitch;
    reg [15:0] gath_words;

    reg        ld_sel;
    reg [2:0]  ld_dim;
    reg [15:0] ld_count;
    reg signed [31:0] ld_stride;

    reg        d_hdr_en, d_dim_en, d_ax_en;
    reg [1:0]  d_axis;
    reg signed [15:0] d_astep;
    reg [ADDR_W-1:0]  d_base;
    reg [2:0]  d_ndim;
    reg        d_ax_sel;
    reg signed [15:0] d_abase;
    reg [15:0] d_aext;

    reg [3:0]  ist;
    reg [1:0]  wst;
    reg        go;

    // The configuration write lands in a register first: the processor is
    // most of a die away and the wire alone was 3.5 ns at the v8t5 route.
    // `stat_busy` covers the cycle through `go`, so a poll after the write
    // sees busy exactly when it did.
    reg        cfg_en_q;
    reg [7:0]  cfg_addr_q;
    reg [63:0] cfg_data_q;
    always @(posedge clk) begin
        cfg_en_q   <= cfg_en && resetn;
        cfg_addr_q <= cfg_addr;
        cfg_data_q <= cfg_data;
    end
    wire [7:0] reg_sel = {cfg_addr_q[7:3], 3'b000};

    // Burst caps. flags[7:5] is a log2 cap with 0 meaning BURST_MAX, so a
    // driver can reproduce the pre-burst behaviour with flags[7:5] = 1.
    wire [8:0] cap = (flags[7:5] == 3'd0) ? BURST_MAX[8:0]
                                          : (9'd1 << (flags[7:5] - 3'd1));
    wire       wcoal = flags[3];

    // ================================================== descriptor walkers
    wire [ADDR_W-1:0] src_addr, dst_addr;
    wire src_last, dst_last, src_valid, dst_valid, src_active, dst_active;
    wire dst_low_nz;                 // dst_addr[4:0] != 0, off the walker's registers

    reg  elem_adv;                          // combinational, driven below

    // A transform move iterates SOURCE words and its dst walker steps once per
    // ENTRY, so a dst descriptor counts entries, not words.
    wire xf = (mode == MODE_XFORM);
    reg  [15:0] xb_cnt;                     // source word within the entry
    wire ent_first = (xb_cnt == 16'd0);
    wire ent_last  = (xb_cnt == (IN_BEATS[15:0] - 16'd1));
    // The walkers run ONE ELEMENT AHEAD of the element latch, so the dst walker
    // must reach entry e+1 while the last element of entry e is being latched --
    // one step before `ent_last`, not on it. Advancing on `ent_last` puts every
    // entry's words at the PREVIOUS entry's address.
    wire ent_pen   = (IN_BEATS == 1) ? 1'b1
                                     : (xb_cnt == (IN_BEATS[15:0] - 16'd2));

    wire walk_last  = xf ? src_last : dst_last;
    wire desc_start = (ist == I_GO);
    wire desc_next  = elem_adv && !walk_last;
    wire dst_next   = xf ? (desc_next && ent_pen) : desc_next;

    // OREG 1: the walkers present their element from a two-entry queue, so
    // `proc` reaches 43 flops each, not every walker enable.
    mx_tdesc #(.NDIM(6), .AW(ADDR_W), .CW(16), .SW(32), .XW(16),
               .OREG(1)) u_src (
        .clk(clk), .rst(!resetn),
        .ld_dim_en(d_dim_en && (ld_sel == 1'b0)), .ld_dim(ld_dim),
        .ld_count(ld_count), .ld_stride(ld_stride),
        .ld_axis(d_axis), .ld_astep(d_astep),
        .ld_hdr_en(d_hdr_en && (ld_sel == 1'b0)), .ld_base(d_base),
        .ld_ndim(d_ndim),
        .ld_ax_en(d_ax_en && (ld_sel == 1'b0)), .ld_ax_sel(d_ax_sel),
        .ld_abase(d_abase), .ld_aext(d_aext),
        .start(desc_start), .next(desc_next),
        .active(src_active), .last(src_last), .valid(src_valid), .addr(src_addr),
        .low_nz()
    );

    mx_tdesc #(.NDIM(6), .AW(ADDR_W), .CW(16), .SW(32), .XW(16),
               .OREG(1)) u_dst (
        .clk(clk), .rst(!resetn),
        .ld_dim_en(d_dim_en && (ld_sel == 1'b1)), .ld_dim(ld_dim),
        .ld_count(ld_count), .ld_stride(ld_stride),
        .ld_axis(d_axis), .ld_astep(d_astep),
        .ld_hdr_en(d_hdr_en && (ld_sel == 1'b1)), .ld_base(d_base),
        .ld_ndim(d_ndim),
        .ld_ax_en(d_ax_en && (ld_sel == 1'b1)), .ld_ax_sel(d_ax_sel),
        .ld_abase(d_abase), .ld_aext(d_aext),
        .start(desc_start), .next(dst_next),
        .active(dst_active), .last(dst_last), .valid(dst_valid), .addr(dst_addr),
        .low_nz(dst_low_nz)
    );

    // ================================================== index buffer
    // `ix_we` is registered, so address and data must be registered WITH it:
    // ix_waddr and m_rdata direct write the next address with data already gone.
    reg  [7:0]   ix_waddr, ix_raddr, ix_wr_a;
    reg  [255:0] ix_data;
    reg          ix_we, ix_active;
    wire [255:0] ix_q;
    kohaku_sdpram #(.WIDTH(256), .DEPTH(IDX_WORDS), .MEM_PRIM("block"),
                    .READ_LAT(1)) u_ixbuf (
        .clk(clk), .wr_en(ix_we), .wr_addr(ix_wr_a), .wr_data(ix_data),
        .rd_en(1'b1), .rd_addr(ix_raddr), .rd_data(ix_q)
    );

    reg [15:0] ix_got;
    reg [15:0] g_row, g_word;
    wire [2:0] g_lane = g_row[2:0];
    wire [31:0] g_index = ix_q[g_lane*32 +: 32];

    // ================================================== PRNG
    reg          pr_start;
    reg  [127:0] pr_ctr;
    reg          pr_half;
    reg  [127:0] pr_lo;
    wire         pr_busy, pr_valid;
    wire [127:0] pr_out;

    mm_prng #(.ROUNDS(10)) u_prng (
        .clk(clk), .rst(!resetn),
        .start(pr_start), .key_in(seed), .ctr_in(pr_ctr),
        .busy(pr_busy), .out_valid(pr_valid), .out(pr_out)
    );

    // ================================================== fill pattern
    reg [DATA_W-1:0] fill_word;
    integer fi;
    always @(*) begin
        fill_word = {DATA_W{1'b0}};
        for (fi = 0; fi < 32; fi = fi + 1) begin
            case (ewidth)
                2'd0: fill_word[fi*8 +: 8] = imm[7:0];
                2'd1: begin
                    if (fi < 16) begin
                        fill_word[fi*16 +: 16] = imm[15:0];
                    end
                end
                default: begin
                    if (fi < 8) begin
                        fill_word[fi*32 +: 32] = imm[31:0];
                    end
                end
            endcase
        end
    end

    // ================================================== AXI statics
    assign m_awsize  = 3'd5;              // 32 bytes
    assign m_arsize  = 3'd5;
    assign m_awburst = 2'b01;
    assign m_arburst = 2'b01;
    assign m_wstrb   = {(DATA_W/8){1'b1}};
    assign m_bready  = 1'b1;
    // Space for a whole burst is reserved before its AR goes out, so the read
    // return can never be refused and never backs up into the shared FIFO.
    assign m_rready  = 1'b1;
    assign stat_busy = (ist != I_IDLE) || go;

    // TWO registers before the address: BRAM output straight into a 32x32
    // multiply measured 188 MHz, so the index is captured before the multiplier.
    reg [31:0]       idx_r, prod_r;
    reg [ADDR_W-1:0] row_base;

    wire [ADDR_W-1:0] gath_addr =
        row_base + {{(ADDR_W-21){1'b0}}, g_word[15:0], 5'd0};

    // ================================================== element latch
    reg [ADDR_W-1:0] e_rd, e_wr;
    reg [1:0]        e_kind;
    reg              e_last, e_flt, e_xp;

    wire [ADDR_W-1:0] lt_rd = (mode == MODE_GATHER) ? gath_addr : src_addr;
    wire              lt_rv = (mode == MODE_GATHER) ? 1'b1      : src_valid;
    wire              lt_ma = dst_low_nz;
    // A padded element issues no read, and a transform counts IN_BEATS off the
    // return -- a bound axis leaves the occupant a beat short, forever. Faulted.
    wire              lt_xpad = xf && !lt_rv;
    wire [1:0]        lt_kind = (!dst_valid || lt_ma)  ? K_SKIP
                              : (mode == MODE_FILL)     ? K_FILL
                              : (mode == MODE_GENERATE) ? K_GEN
                              : lt_rv                   ? K_RD
                              :                           K_FILL;

    // ================================================== the transform slot
    // Between R and the FIFO: the FIFO holds CONVERTED words and the walker
    // feeds the occupant directly, so a strided source needs no gather pass.
    //
    // `start` LEADS the first beat. mx_quant is `if (start) ... else if (filling
    // && beat_valid)`, so a beat presented WITH start is silently dropped.
    reg  [15:0]       xr_cnt;               // source word within the entry
    reg               xb_v1;
    reg  [DATA_W-1:0] xb_d1;
    wire              xr_beat = xf && m_rvalid && !ix_active;

    // ONE ENTRY IN THE SLOT: the occupant is not double-buffered, so `start`
    // would reset the entry still packing. Entry k's WRITE still overlaps k+1's
    // reads -- the command FIFO already decoupled those.
    reg               ent_busy, xo_busy, xf_pend;
    reg  [1:0]        xo_sel;
    reg  [DATA_W-1:0] xo0, xo1, xo2, xo3;
    wire [DATA_W-1:0] xo_word = (xo_sel == 2'd0) ? xo0
                              : (xo_sel == 2'd1) ? xo1
                              : (xo_sel == 2'd2) ? xo2 : xo3;

    // ================================================== staging FIFO
    reg  [CNT_W-1:0] occ;                   // reserved + present
    reg  [CNT_W-1:0] fcnt;                  // present

    // THE ROOM LIMIT IS A CONSTANT PER RUN, computed at config so the hot path
    // is one compare against a register, not an add-then-compare with `mode` in
    // it. `occ + occ_need <= FIFO_D` is `occ <= FIFO_D - occ_need`, and occ_need
    // is one of two compile-time constants, so the subtraction folds. This was
    // the node's last cone: mode_reg -> the adder -> stall -> the command FIFO
    // write enable, 12 levels, WNS -0.081.
    localparam [CNT_W-1:0] XF_OCC = {{(CNT_W-9){1'b0}}, OUT_W9};
    localparam [CNT_W-1:0] XF_ROOM_LIM = FIFO_D[CNT_W-1:0] - XF_OCC;
    localparam [CNT_W-1:0] CP_ROOM_LIM = FIFO_D[CNT_W-1:0] - 16'd1;
    reg  [CNT_W-1:0] room_lim;
    wire             f_wr = xf ? xo_busy : (m_rvalid && !ix_active);
    wire [DATA_W-1:0] f_din = xf ? xo_word : m_rdata;
    wire             f_rd;                  // driven by the write engine
    wire [DATA_W-1:0] f_dout;
    wire             f_empty, f_full;

    sync_fifo #(.DATA_WIDTH(DATA_W), .FIFO_DEPTH(FIFO_D),
                .MEMORY_TYPE("block")) u_dfifo (
        .clk(clk), .rst(!resetn),
        .wr_en(f_wr), .wr_data(f_din), .wr_busy(f_full), .wr_almost(),
        .rd_en(f_rd), .rd_data(f_dout), .rd_busy(f_empty)
    );

    // ================================================== command FIFO
    wire             c_wr, c_rd;
    wire [CMD_W-1:0] c_din, c_dout;
    wire             c_full, c_empty;

    sync_fifo #(.DATA_WIDTH(CMD_W), .FIFO_DEPTH(CMD_D),
                .MEMORY_TYPE("distributed")) u_cfifo (
        .clk(clk), .rst(!resetn),
        .wr_en(c_wr), .wr_data(c_din), .wr_busy(c_full), .wr_almost(),
        .rd_en(c_rd), .rd_data(c_dout), .rd_busy(c_empty)
    );

    wire [ADDR_W-1:0] c_addr = c_dout[ADDR_W-1:0];
    wire [7:0]        c_len  = c_dout[ADDR_W +: 8];    // beats - 1
    wire [1:0]        c_kind = c_dout[ADDR_W+8 +: 2];

    // ================================================== run accumulators
    reg [ADDR_W-1:0] ra_base, wa_base;
    reg [8:0]        ra_n,    wa_n;
    reg              ra_open, wa_open;
    reg [1:0]        wa_kind;
    reg [7:0]        ar_out, wr_out;
    // Both sums off the REGISTERED count, so the late enable only selects.
    wire [7:0]       wr_up = wr_out + 8'd1;
    wire [7:0]       wr_dn = wr_out - 8'd1;
    // The room test REGISTERED: as an expression it puts an 8-bit compare on
    // the m_wready path, -0.109 ns at 3.333 in ktpu_ship_2x2_6c2v_il_pump.
    reg              wr_room;

    // The address a run extends to, and the words it may still take, are CARRIED
    // rather than computed: as expressions they put two adders in series on the

    // path to the command FIFO's write enable -- 14 levels, -0.155 ns at 3.33
    // (OOC, xcvu13p-2L). Registering both is worth 40 MHz.
    reg [ADDR_W-1:0] ra_nxt, wa_nxt;
    reg [7:0]        ra_room, wa_room;

    wire [ADDR_W-1:0] w32 = {{(ADDR_W-6){1'b0}}, 6'd32};
    // 4 KB / 32 B minus the offset already used, clamped by the burst cap.
    wire [7:0] cap_room = cap[7:0] - 8'd1;
    wire [7:0] pg_rd    = 8'd127 - {1'b0, e_rd[11:5]};
    wire [7:0] pg_wr    = 8'd127 - {1'b0, e_wr[11:5]};
    wire [7:0] ra_room0 = (pg_rd < cap_room) ? pg_rd : cap_room;
    wire [7:0] wa_room0 = (pg_wr < cap_room) ? pg_wr : cap_room;

    wire ra_ext = ra_open && (e_kind == K_RD) && (e_rd == ra_nxt)
                  && (ra_room != 8'd0);
    wire wa_ext = wa_open && wcoal && (e_kind == wa_kind) && (e_kind != K_GEN)
                  && (e_kind != K_SKIP) && (e_wr == wa_nxt)
                  && (wa_room != 8'd0);

    wire close_ar = ra_open && !ra_ext;
    wire close_wc = wa_open && !wa_ext;

    // The AR ready came back from u_dram combinationally into ar_slot -> ar_ok
    // -> stall -> proc -> c_wr: 1,336 paths at 12-13 levels after the arbiter
    // shrink took them from 15. sb_skid's i_ready is a flop, so this cuts it.
    reg  [ADDR_W-1:0] ar_addr_i;
    reg  [7:0]        ar_len_i;
    reg               ar_valid_i;
    wire              ar_ready_i;

    sb_skid #(.W(ADDR_W + 8)) u_arskid (
        .clk(clk), .rst(!resetn),
        .i_valid(ar_valid_i), .i_ready(ar_ready_i),
        .i_data({ar_addr_i, ar_len_i}),
        .o_valid(m_arvalid), .o_ready(m_arready),
        .o_data({m_araddr, m_arlen})
    );

    assign m_arid = {ID_W{1'b0}};

    wire ar_slot  = !ar_valid_i || ar_ready_i;
    wire ar_ok    = ar_slot && (ar_out < MAX_OUT[7:0]);
    // A transform entry reserves OUT_WORDS at once: still a static count, still
    // known before the AR, which is what m_rready = 1 rests on.
    wire [CNT_W-1:0] occ_need = xf ? {{(CNT_W-9){1'b0}}, OUT_W9}
                                   : {{(CNT_W-1){1'b0}}, 1'b1};
    wire fifo_room = (occ <= room_lim);
    wire ent_gate  = !xf || ent_first;
    wire xf_cmd    = xf && ent_first && (e_kind == K_RD);

    wire stall_ar   = close_ar && !ar_ok;
    wire stall_cmd  = xf ? (xf_cmd && c_full) : (close_wc && c_full);
    wire stall_fifo = (e_kind == K_RD) && ent_gate && !fifo_room;
    wire stall_slot = xf_cmd && (ent_busy || !x_gnt);
    wire stall      = stall_ar || stall_cmd || stall_fifo || stall_slot;

    wire proc = (ist == I_RUN) && !stall;

    // ================================================== write engine
    reg [ADDR_W-1:0] w_addr;
    reg [8:0]        w_left;
    reg [1:0]        w_kind;
    reg [DATA_W-1:0] gdata;
    reg              wv_r;

    // A command loads from W_IDLE or straight off the last beat of the burst
    // before it, so back-to-back bursts cost n+1 cycles rather than n+2.
    wire w_take = (
        (
            (wst == W_IDLE)
            || ((wst == W_DATA) && (w_left == 9'd1) && m_wvalid && m_wready)
        )
        && !c_empty
        && (wr_out < MAX_WOUT[7:0])
    );
    wire w_endbst = (wst == W_DATA) && m_wvalid && m_wready
                    && (w_left == 9'd1);

    // A structural `wv_r && !f_empty` here was MEASURED AT -0.182 ns, 18 MHz, on
    // the m_wready path, and closed nothing `rd_ok` does not. Residency only.
    assign m_wvalid = wv_r;

    assign c_rd = w_take;
    assign f_rd = (wst == W_DATA) && (w_kind == K_RD) && m_wvalid && m_wready;
    // Registering `fill_word` here to keep the generator out of this cone was
    // MEASURED at +665 LUT in the processor, not the -256 predicted: it breaks
    // sharing between the generator and the walkers. Left combinational.
    assign m_wdata = (w_kind == K_RD) ? f_dout
                   : (w_kind == K_FILL) ? fill_word : gdata;
    assign m_wlast = (w_left == 9'd1);

    // The counter is the destination's ABSOLUTE word address, so one fill and
    // four fills of its quarters produce identical bytes -- prng.md s3.2.
    // Zero-extended to the 35 bits a 40-bit address needs, so a narrower ADDR_W
    // still elaborates and 40 is the module's ceiling.

    // The top slice sits ABOVE the half select, so a region under 4 GB generates
    // exactly the bytes it did when the word address was 32 bits.
    wire [34:0] wpos = {{(40-ADDR_W){1'b0}}, w_addr[ADDR_W-1:5]};
    wire [34:0] cpos = {{(40-ADDR_W){1'b0}}, c_addr[ADDR_W-1:5]};

    wire [127:0] gctr_lo = {92'd0, cpos[34:32], 1'b0, cpos[31:0]};
    wire [127:0] gctr_hi = {92'd0, wpos[34:32], 1'b1, wpos[31:0]};

    // The whole burst must be resident before AW, so a granted write streams at
    // one beat per cycle and never parks mag_dram_port's write mux.
    wire [8:0] c_beats  = {1'b0, c_len} + 9'd1;
    // Net of the beat leaving this cycle: a chained burst starting one word
    // short drained the FIFO past empty and repeated the previous word.
    wire [CNT_W-1:0] fcnt_av = fcnt - (f_rd ? {{(CNT_W-1){1'b0}}, 1'b1}
                                            : {CNT_W{1'b0}});
    // `fcnt` COUNTS A WORD BEFORE THE FIFO PRESENTS IT. xpm_fifo_sync in fwft
    // deasserts `empty` some cycles after the write, measured 2 to 6 here, so a

    // pop timed on the count alone samples X -- one word, silently, and only
    // when the write engine happens to pounce inside that window.
    wire rd_ok      = !f_empty;
    // `f_rd` LAST: it carries downstream ready and inside `fcnt_av` it sat ahead
    // of the comparator. Exactly `fcnt_av >= n`; `>` alone hangs at exact residency.
    wire fcnt_hi  = |fcnt[CNT_W-1:9];
    wire ge_w     = fcnt_hi || (fcnt[8:0] >= w_left);
    wire gt_w     = fcnt_hi || (fcnt[8:0] >  w_left);
    wire ge_c     = fcnt_hi || (fcnt[8:0] >= c_beats);
    wire gt_c     = fcnt_hi || (fcnt[8:0] >  c_beats);
    wire w_ready_rd = (w_kind != K_RD) || (rd_ok && (f_rd ? gt_w : ge_w));
    wire c_ready    = (c_kind != K_RD) || (rd_ok && (f_rd ? gt_c : ge_c));
    // Straight into W_DATA when the data is already there: stopping in W_ARM
    // would cost a third cycle on every single-beat write.
    wire w_now      = w_take && (c_kind != K_GEN) && c_ready;
    wire aw_fire    = w_now
                   || ((wst == W_ARM) && w_ready_rd && (wr_out < MAX_WOUT[7:0]));

    // ================================================== burst hand-off

    // A run held open across a FULL COMMAND FIFO deadlocks only if the write
    // engine is starved of data; otherwise a flush chops bursts to one word.

    // REGISTERED. Starvation persists until data arrives, so a flush one cycle
    // late still breaks it, and combinational it put m_wready on the AR enable.
    reg  w_starve;
    wire w_starve_d = ((wst == W_ARM) && !w_ready_rd)
                 || ((wst == W_IDLE) && !c_empty && (c_kind == K_RD) && !c_ready);
    wire rflush   = (ist == I_RUN) && stall_cmd && ra_open && ar_ok && w_starve;
    wire flush_ar = (ist == I_FLUSH) && ra_open && ar_ok;
    wire flush_wc = (ist == I_FLUSH) && !ra_open && wa_open && !c_full;
    // A transform entry's run MUST close at the boundary: held open across the
    // stall waiting for `x_done`, its AR never goes out and the wait is forever.
    wire xflush   = xf_pend && ra_open && ar_ok;

    wire ar_load = (proc && close_ar) || flush_ar || rflush || xflush;

    // A transform writes ONE burst of OUT_WORDS per entry, named when the entry
    // opens; the run accumulator is a copy-mode device and stays idle.
    assign c_wr  = xf ? (proc && xf_cmd) : ((proc && close_wc) || flush_wc);
    assign c_din = xf ? {K_RD, OUT_W9[7:0] - 8'd1, e_wr}
                      : {wa_kind, wa_n[7:0] - 8'd1, wa_base};

    reg  ar_dec, occ_up, occ_dn;
    always @(*) begin
        ar_dec = m_rvalid && m_rlast && (ar_out != 8'd0);
        occ_up = proc && (e_kind == K_RD) && ent_gate;
        occ_dn = f_rd;
        // The walkers step on every element the issue engine consumes, except
        // in GATHER where the address pipeline re-latches from I_LAT.
        elem_adv = (ist == I_LAT)
                || (proc && !e_last && !e_flt && !e_xp
                    && (mode != MODE_GATHER));
    end

    wire err_ax = (m_rvalid && (m_rresp != 2'b00))
               || (m_bvalid && (m_bresp != 2'b00));

    // ================================================== control
    always @(posedge clk) begin
        if (!resetn) begin
            ist <= I_IDLE; wst <= W_IDLE; go <= 1'b0;
            mode <= 3'd0; ewidth <= 2'd1; flags <= 8'd0;
            seed <= 64'd0; imm <= 32'd0;
            idx_base <= {ADDR_W{1'b0}}; idx_count <= 16'd0;
            dst_base <= {ADDR_W{1'b0}}; d_src_base <= {ADDR_W{1'b0}};
            gath_pitch <= 32'd0; gath_words <= 16'd1;
            d_hdr_en <= 1'b0; d_dim_en <= 1'b0; d_ax_en <= 1'b0;
            ld_sel <= 1'b0; ld_dim <= 3'd0; ld_count <= 16'd1; ld_stride <= 32'd0;
            d_axis <= 2'd0; d_astep <= 16'd0; d_base <= {ADDR_W{1'b0}};
            d_ndim <= 3'd1; d_ax_sel <= 1'b0; d_abase <= 16'd0; d_aext <= 16'd0;
            // AXI payload dropped: the valids qualify it. Config, PRNG seed
            // state, occupancy counters and status registers all keep theirs.
            m_awvalid <= 1'b0; wv_r <= 1'b0; ar_valid_i <= 1'b0;
            stat_fault <= F_NONE; stat_done <= 32'd0;
            ix_we <= 1'b0; ix_waddr <= 8'd0; ix_raddr <= 8'd0; ix_got <= 16'd0;
            ix_wr_a <= 8'd0; ix_active <= 1'b0;
            row_base <= {ADDR_W{1'b0}}; idx_r <= 32'd0; prod_r <= 32'd0;
            g_row <= 16'd0; g_word <= 16'd0;
            pr_start <= 1'b0; pr_ctr <= 128'd0; pr_half <= 1'b0;
            pr_lo <= 128'd0;
            // RESET-RISK: e_rd/e_wr, ra_base/wa_base, ra_nxt/wa_nxt and w_addr
            // are run accumulators, qualified by e_kind/ra_open/wa_open/w_kind.
            e_kind <= K_SKIP; e_last <= 1'b0; e_flt <= 1'b0; e_xp <= 1'b0;
            xf_id <= {XID_W{1'b0}}; xf_mode <= {XMODE_W{1'b0}};
            x_req <= 1'b0; x_start <= 1'b0; x_beat_valid <= 1'b0;
            x_id <= {XID_W{1'b0}}; x_mode <= {XMODE_W{1'b0}};
            xb_cnt <= 16'd0; xr_cnt <= 16'd0; xb_v1 <= 1'b0;
            ent_busy <= 1'b0; xo_busy <= 1'b0; xf_pend <= 1'b0;
            xo_sel <= 2'd0;
            ra_room <= 8'd0; wa_room <= 8'd0;
            ra_n <= 9'd0; wa_n <= 9'd0;
            ra_open <= 1'b0; wa_open <= 1'b0; wa_kind <= K_SKIP;
            ar_out <= 8'd0; wr_out <= 8'd0; w_starve <= 1'b0;
            occ <= {CNT_W{1'b0}}; fcnt <= {CNT_W{1'b0}};
            room_lim <= CP_ROOM_LIM;        // mode resets to COPY
            w_left <= 9'd0; w_kind <= K_SKIP;
        end else begin
            d_hdr_en <= 1'b0; d_dim_en <= 1'b0; d_ax_en <= 1'b0;
            ix_we <= 1'b0; pr_start <= 1'b0; go <= 1'b0;
            idx_r    <= g_index;
            prod_r   <= idx_r * gath_pitch;
            row_base <= d_src_base + {{(ADDR_W-32){1'b0}}, prod_r};

            occ  <= occ  + (occ_up ? occ_need : {CNT_W{1'b0}})
                         - (occ_dn ? {{(CNT_W-1){1'b0}}, 1'b1} : {CNT_W{1'b0}});
            fcnt <= fcnt + (f_wr   ? 16'd1 : 16'd0) - (f_rd   ? 16'd1 : 16'd0);

            // ---- the slot's return side ----
            // Beats two registers behind R, `start` one, so start never shares a
            // cycle with a beat.
            xb_d1        <= m_rdata;
            xb_v1        <= xr_beat;
            x_beat       <= xb_d1;
            x_beat_valid <= xb_v1;
            x_start      <= xr_beat && (xr_cnt == 16'd0);
            if (xr_beat) begin
                xr_cnt <= (xr_cnt == (IN_BEATS[15:0] - 16'd1))
                        ? 16'd0 : xr_cnt + 16'd1;
            end

            // The occupant emits OUT_WORDS in parallel and the FIFO takes one a
            // cycle, so `done` starts a serialiser rather than writing directly.
            if (x_done) begin
                xo0 <= x_w0; xo1 <= x_w1; xo2 <= x_w2; xo3 <= x_w3;
                xo_sel   <= 2'd0;
                xo_busy  <= 1'b1;
                ent_busy <= 1'b0;
            end
            else if (xo_busy) begin
                if (xo_sel == (OUT_W9[1:0] - 2'd1)) begin
                    xo_busy <= 1'b0;
                end
                xo_sel <= xo_sel + 2'd1;
            end

            // ---- register writes ----
            if (cfg_en_q) begin
                case (reg_sel)
                    8'h00: begin
                        mode   <= cfg_data_q[2:0];
                        ewidth <= cfg_data_q[4:3];
                        flags  <= cfg_data_q[15:8];
                        go     <= cfg_data_q[16];
                        room_lim <= (cfg_data_q[2:0] == MODE_XFORM)
                                  ? XF_ROOM_LIM : CP_ROOM_LIM;
                    end
                    8'h10: begin
                        ld_sel   <= cfg_data_q[0];
                        d_base   <= cfg_data_q[4 +: ADDR_W];
                        d_ndim   <= cfg_data_q[46:44];
                        d_hdr_en <= 1'b1;
                        if (cfg_data_q[0]) begin
                            dst_base   <= cfg_data_q[4 +: ADDR_W];
                        end
                        else begin
                            d_src_base <= cfg_data_q[4 +: ADDR_W];
                            // The transform applies to the READ side, so its id
                            // and mode ride the source header's free upper bits.
                            xf_id      <= cfg_data_q[47 +: XID_W];
                            xf_mode    <= cfg_data_q[55 +: XMODE_W];
                        end
                    end
                    8'h18: begin
                        ld_sel    <= cfg_data_q[0];
                        ld_dim    <= cfg_data_q[3:1];
                        ld_count  <= cfg_data_q[19:4];
                        ld_stride <= cfg_data_q[51:20];
                    end
                    8'h20: begin
                        d_axis   <= cfg_data_q[1:0];
                        d_astep  <= cfg_data_q[17:2];
                        d_dim_en <= 1'b1;
                    end
                    8'h28: begin
                        ld_sel   <= cfg_data_q[0];
                        d_ax_sel <= cfg_data_q[1];
                        d_abase  <= cfg_data_q[17:2];
                        d_aext   <= cfg_data_q[33:18];
                        d_ax_en  <= 1'b1;
                    end
                    8'h30: begin
                        idx_base  <= cfg_data_q[ADDR_W-1:0];
                        idx_count <= cfg_data_q[55:40];
                    end
                    8'h38: seed <= cfg_data_q;
                    8'h40: imm  <= cfg_data_q[31:0];
                    8'h50: begin
                        gath_pitch <= cfg_data_q[31:0];
                        gath_words <= cfg_data_q[47:32];
                    end
                    default: ;
                endcase
            end

            // ================================================ read issue
            if (ar_load) begin
                ar_addr_i  <= ra_base;
                ar_len_i   <= ra_n[7:0] - 8'd1;
                ar_valid_i <= 1'b1;
            end else if (ar_valid_i && ar_ready_i) begin
                ar_valid_i <= 1'b0;
            end

            ar_out <= ar_out + (ar_load ? 8'd1 : 8'd0)
                             - (ar_dec  ? 8'd1 : 8'd0);

            // A memory error stops the walk but never the drain: outstanding
            // bursts still retire, so a fault reports rather than hangs.
            if (err_ax && (ist != I_IDLE)) begin
                stat_fault <= F_AXI;
            end

            case (ist)
                // ------------------------------------------------------------
                I_IDLE: if (go) begin
                    stat_fault <= F_NONE;
                    g_row <= 16'd0; g_word <= 16'd0;
                    ix_got <= 16'd0; ix_waddr <= 8'd0; ix_raddr <= 8'd0;
                    pr_half <= 1'b0;
                    ra_open <= 1'b0; wa_open <= 1'b0;
                    xb_cnt <= 16'd0; xr_cnt <= 16'd0;
                    xf_pend <= 1'b0; ent_busy <= 1'b0; xo_busy <= 1'b0;
                    // The grant is held for the whole run, so it is taken once
                    // here and the first entry waits on it.
                    x_req  <= xf;
                    x_id   <= xf_id;
                    x_mode <= xf_mode;
                    if (ewidth == 2'd3) begin
                        stat_fault <= F_EWIDTH; ist <= I_FAULT;
                    end else if (mode == MODE_TRANSPOSE) begin
                        stat_fault <= F_MODE; ist <= I_FAULT;
                    end else if (mode == MODE_GATHER) begin
                        if (idx_count > {IDX_WORDS[12:0], 3'd0}) begin
                            stat_fault <= F_IDXLEN; ist <= I_FAULT;
                        end else begin
                            ar_addr_i  <= idx_base;
                            ar_len_i   <= 8'd0;
                            ar_valid_i <= 1'b1;
                            ix_active <= 1'b1;
                            ist <= I_IXA;
                        end
                    end else begin
                        ist <= I_GO;
                    end
                end

                // ---- gather: pull the whole index vector in first ----
                I_IXA: if (ar_valid_i && ar_ready_i) begin
                    ar_valid_i <= 1'b0;
                    ist <= I_IXD;
                end
                I_IXD: if (m_rvalid) begin
                    ix_we    <= 1'b1;
                    ix_wr_a  <= ix_waddr;
                    ix_data  <= m_rdata;
                    ix_got   <= ix_got + 16'd8;
                    ix_waddr <= ix_waddr + 8'd1;
                    if (ix_got + 16'd8 >= idx_count) begin
                        ix_active <= 1'b0;
                        ist <= I_IXW;
                    end else begin
                        ar_addr_i  <= ar_addr_i + {{(ADDR_W-6){1'b0}}, 6'd32};
                        ar_valid_i <= 1'b1;
                        ist <= I_IXA;
                    end
                end

                // The last index word is written during THIS cycle; reading it in
                // the same cycle would return the old contents (read_first).
                I_IXW: ist <= I_GO;

                I_GO: begin
                    ix_raddr <= 8'd0;
                    ist <= (mode == MODE_GATHER) ? I_GA1 : I_GO2;
                end
                I_GO2: ist <= I_LAT;

                // One cycle for the index to leave the buffer into `idx_r`, one
                // for the multiply, one for the base add.
                I_GA1: ist <= I_GA2;
                I_GA2: ist <= I_GA3;
                I_GA3: ist <= I_LAT;

                I_LAT: begin
                    e_rd   <= lt_rd;
                    e_wr   <= dst_addr;
                    e_kind <= lt_kind;
                    e_last <= walk_last;
                    e_flt  <= dst_valid && lt_ma;
                    e_xp   <= lt_xpad;
                    ist    <= I_RUN;
                end

                // ------------------------------------------------------------
                I_RUN: begin
                    if (proc) begin
                        if (e_kind == K_RD) begin
                            if (ra_ext) begin
                                ra_n    <= ra_n + 9'd1;
                                ra_nxt  <= ra_nxt + w32;
                                ra_room <= ra_room - 8'd1;
                            end else begin
                                ra_base <= e_rd; ra_n <= 9'd1; ra_open <= 1'b1;
                                ra_nxt  <= e_rd + w32;
                                ra_room <= ra_room0;
                            end
                        end else if (close_ar) begin
                            ra_open <= 1'b0;
                        end

                        // The write-run accumulator is a COPY-mode device: a
                        // transform's burst is named once per entry above.
                        if (!xf) begin
                            if (e_kind != K_SKIP) begin
                                if (wa_ext) begin
                                    wa_n    <= wa_n + 9'd1;
                                    wa_nxt  <= wa_nxt + w32;
                                    wa_room <= wa_room - 8'd1;
                                end else begin
                                    wa_base <= e_wr; wa_n <= 9'd1;
                                    wa_kind <= e_kind; wa_open <= 1'b1;
                                    wa_nxt  <= e_wr + w32;
                                    wa_room <= wa_room0;
                                end
                            end else if (close_wc) begin
                                wa_open <= 1'b0;
                            end
                        end

                        if (xf) begin
                            if (ent_first) begin
                                ent_busy <= 1'b1;
                            end
                            if (ent_last) begin
                                xb_cnt  <= 16'd0;
                                xf_pend <= 1'b1;
                            end
                            else begin
                                xb_cnt <= xb_cnt + 16'd1;
                            end
                        end

                        if (e_flt) begin
                            stat_fault <= F_ALIGN;
                            ist <= I_FLUSH;
                        end else if (e_xp) begin
                            stat_fault <= F_XPAD;
                            ist <= I_FLUSH;
                        end else if (e_last || (stat_fault == F_AXI)) begin
                            ist <= I_FLUSH;
                        end else if (mode == MODE_GATHER) begin
                            if (g_word + 16'd1 == gath_words) begin
                                g_word   <= 16'd0;
                                g_row    <= g_row + 16'd1;
                                ix_raddr <= (g_row + 16'd1) >> 3;
                            end
                            else begin
                                g_word <= g_word + 16'd1;
                            end
                            ist <= I_GA1;
                        end else begin
                            e_rd   <= lt_rd;
                            e_wr   <= dst_addr;
                            e_kind <= lt_kind;
                            e_last <= walk_last;
                            e_flt  <= dst_valid && lt_ma;
                            e_xp   <= lt_xpad;
                        end
                    end else if (rflush) begin
                        ra_open <= 1'b0;
                    end else if (xflush) begin
                        ra_open <= 1'b0;
                        xf_pend <= 1'b0;
                    end
                end

                // Close whatever the last element left open, read side first so a
                // command never reaches the write engine before its data is asked.
                I_FLUSH: begin
                    if (ra_open) begin
                        if (ar_ok) begin
                            ra_open <= 1'b0;
                        end
                    end else if (wa_open) begin
                        if (!c_full) begin
                            wa_open <= 1'b0;
                        end
                    end
                    else begin
                        ist <= I_DRAIN;
                    end
                end

                // arch.md s8: a move is not done until its writes have retired.
                // The slot counts too: an entry still packing owes the FIFO
                // words the reservation has already promised.
                I_DRAIN: if ((wst == W_IDLE) && c_empty && (wr_out == 8'd0)
                             && (ar_out == 8'd0) && (occ == {CNT_W{1'b0}})
                             && !ent_busy && !xo_busy)
                    ist <= I_DONE;

                I_DONE: begin
                    if (stat_fault == F_NONE) begin
                        stat_done <= stat_done + 32'd1;
                    end
                    x_req <= 1'b0;
                    ist   <= I_IDLE;
                end

                I_FAULT: begin
                    x_req <= 1'b0;
                    ist   <= I_IDLE;
                end
                default: ist <= I_IDLE;
            endcase

            // ================================================ write engine
            if (m_awvalid && m_awready) begin
                m_awvalid <= 1'b0;
            end

            if ((wst == W_DATA) && m_wvalid && m_wready) begin
                w_addr <= w_addr + {{(ADDR_W-6){1'b0}}, 6'd32};
                w_left <= w_left - 9'd1;
            end

            if (w_take) begin
                w_addr <= c_addr;
                w_left <= c_beats;
                w_kind <= c_kind;
                if (c_kind == K_GEN) begin
                    pr_ctr   <= gctr_lo;
                    pr_start <= 1'b1;
                    pr_half  <= 1'b0;
                    wv_r <= 1'b0;
                    wst      <= W_GEN;
                end else if (c_ready) begin
                    m_awaddr  <= c_addr;
                    m_awlen   <= c_len;
                    m_awvalid <= 1'b1;
                    wv_r      <= 1'b1;
                    wst       <= W_DATA;
                end else begin
                    wv_r <= 1'b0;
                    wst      <= W_ARM;
                end
            end else if (w_endbst) begin
                wv_r <= 1'b0;
                wst      <= W_IDLE;
            end else if (wst == W_GEN) begin
                if (pr_valid) begin
                    if (!pr_half) begin
                        pr_lo    <= pr_out;
                        pr_half  <= 1'b1;
                        pr_ctr   <= gctr_hi;
                        pr_start <= 1'b1;
                    end else begin
                        gdata   <= {pr_out, pr_lo};
                        pr_half <= 1'b0;
                        wst     <= W_ARM;
                    end
                end
            end else if (wst == W_ARM) begin
                if (w_ready_rd && (wr_out < MAX_WOUT[7:0])) begin
                    m_awaddr  <= w_addr;
                    m_awlen   <= w_left[7:0] - 8'd1;
                    m_awvalid <= 1'b1;
                    wv_r      <= 1'b1;
                    wst       <= W_DATA;
                end
            end

            // MUX, not carry-in: `aw_fire` comes off m_wready through w_take and
            // was 12 levels into this adder, -0.147 ns at 3.333 in mm_mesh.
            case ({aw_fire, m_bvalid})
                2'b10:   begin wr_out <= wr_up;
                               wr_room <= (wr_up < MAX_WOUT[7:0]); end
                2'b01:   begin wr_out <= wr_dn;
                               wr_room <= (wr_dn < MAX_WOUT[7:0]); end
                default: ;
            endcase

            w_starve <= w_starve_d;
        end
    end

endmodule

`default_nettype wire

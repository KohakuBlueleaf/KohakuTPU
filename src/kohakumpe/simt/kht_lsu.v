// kht_lsu -- the SIMT PE's load/store unit: a queue of memory instructions, the
// coalescer, and the L1 with its fill buffers (compute-unit.md s5).
//
// A MEMORY INSTRUCTION RETIRES INTO THE QUEUE with every lane's address and
// store data; the core has already checked that each active lane is DRAM and
// aligned. Every entry comes back once on `ret`, a load with the wave's words.
//
// THE COALESCER SERVES A LINE, NOT A LANE: the head entry's lowest outstanding
// lane names a 32-byte line and every outstanding lane in it leaves with that
// one access. A miss takes a fill buffer, and the scanner requests every other
// missing line of the head entry and the next one, up to MSHRS in flight.
//
// A LINE THE HEAD IS WAITING ON IS SERVED FROM ITS BUFFER and only then written
// into the L1, so two lines that conflict in the L1 cannot evict each other into
// a livelock. Every array write is driven from state registers in the cycle it
// happens, so a line is never in neither the L1 nor a buffer.
//
// NO READ OVERTAKES A WRITE OF ITS LINE (memory-protocol.md s7.2): a fill whose
// line sits in `wr_*` waits for it, and kht_req holds it until the ack.

`default_nettype none

module kht_lsu #(
    parameter integer LANES    = 8,
    parameter integer WAVES    = 16,
    parameter integer WID      = (WAVES > 1) ? $clog2(WAVES) : 1,
    parameter integer DEPTH    = 4,       // queued instructions, 1..4
    parameter integer LINES    = 128,     // power of two, 32 B each
    parameter integer MSHRS    = 4,       // fill buffers, 1..8
    parameter integer COALESCE = 1,       // 0: one lane per access
    parameter integer WB       = 1,       // 1 write-back, 0 write-through
    parameter [7:0]   TAG_BASE = 8'h40,   // fill buffer f reads with txn TAG_BASE+f
    parameter         MEM_PRIM = "block",
    // Queued entries the scanner walks, from the head; capped at DEPTH. With 2
    // stream on kht_sys ran 10,317 cycles, with 4 (all of DEPTH 4) 7,358.
    parameter integer SCAN     = 2,
    // 1: the scanner steps to the next lane on a different line, not lane by
    // lane (stream 7,358 -> 6,385; +42 LUT).
    parameter integer SKIP     = 0,
    // 1: a candidate with no free buffer holds the walk until one frees (gather,
    // MSHRS 8: 26,110 -> 25,830); 0: it is dropped and found again (MSHRS 2: 47,623 -> 43,836).
    parameter integer HOLD     = 1
)(
    input  wire                  clk,
    input  wire                  resetn,

    input  wire                  req_v,
    output wire                  req_ready,
    input  wire [WID-1:0]        req_wave,
    input  wire [4:0]            req_rd,
    input  wire                  req_st,
    input  wire [1:0]            req_w,       // 0 byte, 1 half, 2 word
    input  wire                  req_sx,
    input  wire [LANES-1:0]      req_mask,
    input  wire [32*LANES-1:0]   req_addr,
    input  wire [32*LANES-1:0]   req_data,
    output wire                  pop,         // an entry left the queue

    output reg                   ret_v,
    input  wire                  ret_ready,
    output reg  [WID-1:0]        ret_wave,
    output reg  [4:0]            ret_rd,
    output reg                   ret_ld,
    output reg  [LANES-1:0]      ret_mask,
    output reg  [32*LANES-1:0]   ret_data,

    input  wire                  flush,       // pulse: write every dirty line back
    input  wire                  inval,       // pulse: ... and drop every line
    output wire                  busy,

    output wire                  rd_v,
    input  wire                  rd_ready,
    output wire [25:0]           rd_line,
    output wire [7:0]            rd_tag,
    input  wire                  rsp_v,
    input  wire [7:0]            rsp_tag,
    input  wire [255:0]          rsp_data,
    output reg                   wr_v,
    input  wire                  wr_ready,
    output reg  [25:0]           wr_line,
    output reg  [255:0]          wr_data,
    input  wire                  wr_idle,     // every write acknowledged

    output reg  [31:0]           acc_ctr,     // line accesses served
    output reg  [31:0]           fill_ctr     // fill buffers allocated
);
    localparam integer VW   = 32 * LANES;
    localparam integer LNW  = (LANES > 1) ? $clog2(LANES) : 1;
    localparam integer IDX  = $clog2(LINES);
    localparam integer TAGW = 26 - IDX;
    localparam integer TW   = TAGW + 2;                  // {valid, dirty, tag}
    localparam integer FBW  = (MSHRS > 1) ? $clog2(MSHRS) : 1;
    localparam integer QS   = (DEPTH < 2) ? 2 : DEPTH;   // the RAM needs 2
    localparam integer QW   = $clog2(QS);
    localparam integer LOOK = (DEPTH < 2) ? 1 : (SCAN < 1) ? 1 : (SCAN > DEPTH) ? DEPTH : SCAN;

    localparam [2:0] H_IDLE = 3'd0, H_LEAD = 3'd1, H_PICK = 3'd2, H_L1 = 3'd3;
    localparam [2:0] H_SW = 3'd4, H_FB = 3'd5, H_WT = 3'd6, H_RET = 3'd7;
    localparam [2:0] I_IDLE = 3'd0, I_VRD = 3'd1, I_VSEND = 3'd2, I_WR = 3'd3;
    localparam [2:0] I_CLR = 3'd4, I_SRD = 3'd5, I_SSEND = 3'd6, I_DRAIN = 3'd7;
    localparam [1:0] F_FREE = 2'd0, F_REQ = 2'd1, F_WAIT = 2'd2, F_DATA = 2'd3;

    reg  [2:0]       hst, ist;
    reg  [LANES-1:0] todo, cur_same;
    reg  [25:0]      cur_line;
    reg  [FBW-1:0]   cur_fb, ins_fb;
    reg  [VW-1:0]    res;
    reg  [255:0]     st_line;
    reg  [IDX-1:0]   scan;
    reg              fl_pend, fl_inval;

    // ======================= the queue =================================
    reg [WID-1:0]   q_wave [0:QS-1];
    reg [4:0]       q_rd   [0:QS-1];
    reg             q_st   [0:QS-1];
    reg [1:0]       q_w    [0:QS-1];
    reg             q_sx   [0:QS-1];
    reg [LANES-1:0] q_mask [0:QS-1];
    reg [QW-1:0]    q_hd, q_tl;
    reg [QW:0]      q_n;

    function [QW-1:0] nextp;
        input [QW-1:0] p;
        nextp = (p == DEPTH - 1) ? {QW{1'b0}} : (p + 1'b1);
    endfunction

    assign req_ready = (q_n < DEPTH);
    wire q_push = req_v && req_ready;
    assign pop = (hst == H_RET) && (!ret_v || ret_ready);

    always @(posedge clk) begin
        if (q_push) begin
            q_wave[q_tl] <= req_wave;
            q_rd[q_tl]   <= req_rd;
            q_st[q_tl]   <= req_st;
            q_w[q_tl]    <= req_w;
            q_sx[q_tl]   <= req_sx;
            q_mask[q_tl] <= req_mask;
        end
        if (!resetn) begin
            q_hd <= {QW{1'b0}};
            q_tl <= {QW{1'b0}};
            q_n  <= {(QW+1){1'b0}};
        end else begin
            if (q_push) q_tl <= nextp(q_tl);
            if (pop)    q_hd <= nextp(q_hd);
            q_n <= q_n + {{QW{1'b0}}, q_push} - {{QW{1'b0}}, pop};
        end
    end

    // Addresses twice (the head and the scanner each read one), data once.
    // The scanned entry is a REGISTER, `sc_ptr`, with its distance from the head
    // beside it: as `q_hd + sc_off` the adder sat in front of the queue's
    // LUTRAM address and the scan path closed at -0.245 ns.
    reg  [QW-1:0]   sc_ptr, sc_off;
    reg  [LNW-1:0]  sc_lane;
    reg  [25:0]     c_line;
    reg             c_v;
    wire [QW-1:0]   sc_e = sc_ptr;
    wire [VW-1:0]   h_addr, h_data, s_addr;
    kohaku_sdpram #(.WIDTH(VW), .DEPTH(QS), .MEM_PRIM("distributed"),
                    .READ_LAT(0)) u_qah (
        .clk(clk), .wr_en(q_push), .wr_addr(q_tl), .wr_data(req_addr),
        .rd_en(1'b1), .rd_addr(q_hd), .rd_data(h_addr)
    );
    kohaku_sdpram #(.WIDTH(VW), .DEPTH(QS), .MEM_PRIM("distributed"),
                    .READ_LAT(0)) u_qas (
        .clk(clk), .wr_en(q_push), .wr_addr(q_tl), .wr_data(req_addr),
        .rd_en(1'b1), .rd_addr(sc_e), .rd_data(s_addr)
    );
    kohaku_sdpram #(.WIDTH(VW), .DEPTH(QS), .MEM_PRIM("distributed"),
                    .READ_LAT(0)) u_qd (
        .clk(clk), .wr_en(q_push), .wr_addr(q_tl), .wr_data(req_data),
        .rd_en(1'b1), .rd_addr(q_hd), .rd_data(h_data)
    );
    wire             h_st = q_st[q_hd];
    wire [1:0]       h_w  = q_w[q_hd];
    wire             h_sx = q_sx[q_hd];

    // ======================= fill buffers =============================
    reg [1:0]   fb_st    [0:MSHRS-1];
    reg [25:0]  fb_line  [0:MSHRS-1];
    reg [255:0] fb_data  [0:MSHRS-1];
    reg         fb_dirty [0:MSHRS-1];

    // The read request is registered (combinational, it reached kht_req's count
    // at -0.530 ns); the buffer on offer is left out of the next pick.
    reg             rq_v;
    reg [FBW-1:0]   rq_fb;
    reg [FBW:0] fb_free, fb_req;
    reg         fb_any;
    integer ff;
    always @(*) begin
        fb_free = {1'b0, {FBW{1'b0}}};
        fb_req  = {1'b0, {FBW{1'b0}}};
        fb_any  = 1'b0;
        for (ff = MSHRS - 1; ff >= 0; ff = ff - 1) begin
            if (fb_st[ff] == F_FREE) fb_free = {1'b1, ff[FBW-1:0]};
            if ((fb_st[ff] == F_REQ) && !(rq_v && (rq_fb == ff[FBW-1:0])))
                fb_req = {1'b1, ff[FBW-1:0]};
            if (fb_st[ff] != F_FREE) fb_any  = 1'b1;
        end
    end

    // ======================= the L1 arrays ============================
    // {valid, dirty, tag} in three distributed copies: the head's, the install
    // and sweep's, and the scanner candidate's, so an install never stalls it.
    wire           tag_we;
    wire [IDX-1:0] tag_wa;
    wire [TW-1:0]  tag_wd;
    wire [IDX-1:0] tag_ra_s;
    wire [TW-1:0]  tag_h, tag_s, tag_c;
    kohaku_sdpram #(.WIDTH(TW), .DEPTH(LINES), .MEM_PRIM("distributed"),
                    .READ_LAT(0)) u_tagh (
        .clk(clk), .wr_en(tag_we), .wr_addr(tag_wa), .wr_data(tag_wd),
        .rd_en(1'b1), .rd_addr(cur_line[IDX-1:0]), .rd_data(tag_h)
    );
    kohaku_sdpram #(.WIDTH(TW), .DEPTH(LINES), .MEM_PRIM("distributed"),
                    .READ_LAT(0)) u_tags (
        .clk(clk), .wr_en(tag_we), .wr_addr(tag_wa), .wr_data(tag_wd),
        .rd_en(1'b1), .rd_addr(tag_ra_s), .rd_data(tag_s)
    );
    kohaku_sdpram #(.WIDTH(TW), .DEPTH(LINES), .MEM_PRIM("distributed"),
                    .READ_LAT(0)) u_tagc (
        .clk(clk), .wr_en(tag_we), .wr_addr(tag_wa), .wr_data(tag_wd),
        .rd_en(1'b1), .rd_addr(c_line[IDX-1:0]), .rd_data(tag_c)
    );

    wire           dat_we;
    wire [IDX-1:0] dat_wa;
    wire [255:0]   dat_wd;
    wire [IDX-1:0] dat_ra;
    wire [255:0]   dat_q;
    kohaku_sdpram #(.WIDTH(256), .DEPTH(LINES), .MEM_PRIM(MEM_PRIM),
                    .READ_LAT(1)) u_data (
        .clk(clk), .wr_en(dat_we), .wr_addr(dat_wa), .wr_data(dat_wd),
        .rd_en(1'b1), .rd_addr(dat_ra), .rd_data(dat_q)
    );

    // ======================= the head: coalescing ======================
    reg [LNW-1:0] lead;
    integer li;
    always @(*) begin
        lead = {LNW{1'b0}};
        for (li = LANES - 1; li >= 0; li = li - 1) begin
            if (todo[li]) lead = li[LNW-1:0];
        end
    end
    wire [25:0] lead_line = h_addr[32*lead + 5 +: 26];

    reg [LANES-1:0] same_c;
    integer si;
    always @(*) begin
        for (si = 0; si < LANES; si = si + 1) begin
            same_c[si] = todo[si] && ((COALESCE != 0)
                                      ? (h_addr[32*si + 5 +: 26] == lead_line)
                                      : (si == lead));
        end
    end

    // Which buffer holds the head's line and the scanner's candidate, as
    // {found, index}. Inline, not a function: a function reading the arrays from
    // module scope is re-evaluated only when its argument changes, and goes stale.
    wire [25:0]  sc_line;
    reg  [FBW:0] cfb, sc_fb;
    integer fi;
    always @(*) begin
        cfb   = {1'b0, {FBW{1'b0}}};
        sc_fb = {1'b0, {FBW{1'b0}}};
        for (fi = MSHRS - 1; fi >= 0; fi = fi - 1) begin
            if ((fb_st[fi] != F_FREE) && (fb_line[fi] == cur_line)) cfb   = {1'b1, fi[FBW-1:0]};
            if ((fb_st[fi] != F_FREE) && (fb_line[fi] == c_line))   sc_fb = {1'b1, fi[FBW-1:0]};
        end
    end
    wire         h_hit    = tag_h[TW-1] && (tag_h[TAGW-1:0] == cur_line[25:IDX]);
    wire         cfb_busy = (ist != I_IDLE) && (ins_fb == cfb[FBW-1:0]);
    wire         head_l1  = (hst == H_PICK) && !cfb[FBW] && h_hit && (ist == I_IDLE);
    wire         alloc_h  = (hst == H_PICK) && !cfb[FBW] && !h_hit && fb_free[FBW];

    // ---- serving a line ------------------------------------------------------
    wire [255:0] base = (hst == H_FB) ? fb_data[cur_fb] : dat_q;

    function [31:0] ld_word;
        input [255:0] ln;
        input [31:0]  a;
        input [1:0]   w;
        input         sx;
        reg   [31:0]  wd;
        reg   [15:0]  hw;
        reg   [7:0]   by;
        begin
            wd = ln[{a[4:2], 5'd0} +: 32];
            hw = a[1] ? wd[31:16] : wd[15:0];
            by = wd[{a[1:0], 3'd0} +: 8];
            ld_word = (w == 2'd2) ? wd
                    : (w == 2'd1) ? (sx ? {{16{hw[15]}}, hw} : {16'd0, hw})
                                  : (sx ? {{24{by[7]}}, by} : {24'd0, by});
        end
    endfunction

    function [3:0] be_of;
        input [1:0] w;
        input [1:0] a;
        be_of = (w == 2'd2) ? 4'hF
              : (w == 2'd1) ? (a[1] ? 4'b1100 : 4'b0011)
                            : (4'b0001 << a);
    endfunction

    // Lane order ascending, so the highest lane storing a byte wins, as the
    // golden model applies them.
    reg [255:0] merged;
    reg [31:0]  rep;
    reg [3:0]   be;
    integer mi, mb;
    always @(*) begin
        merged = base;
        for (mi = 0; mi < LANES; mi = mi + 1) begin
            rep = (h_w == 2'd0) ? {4{h_data[32*mi +: 8]}}
                : (h_w == 2'd1) ? {2{h_data[32*mi +: 16]}}
                                : h_data[32*mi +: 32];
            be  = be_of(h_w, h_addr[32*mi +: 2]);
            for (mb = 0; mb < 4; mb = mb + 1) begin
                if (cur_same[mi] && be[mb]) begin
                    merged[{h_addr[32*mi + 2 +: 3], mb[1:0], 3'd0} +: 8] = rep[8*mb +: 8];
                end
            end
        end
    end

    // ======================= the scanner ===============================
    // TWO STAGES: the walk registers a candidate line (c_v, c_line), which is
    // looked up and takes a buffer next; in one cycle it was 13 levels, -0.164 ns.
    // A candidate whose entry has popped is a wasted buffer, never a wrong one.
    assign       sc_line = s_addr[32*sc_lane + 5 +: 26];
    wire         sc_have = (q_n > {1'b0, sc_off});
    wire         sc_on   = sc_have && q_mask[sc_e][sc_lane];
    wire         sc_inl1 = tag_c[TW-1] && (tag_c[TAGW-1:0] == c_line[25:IDX]);
    wire         sc_need = c_v && !sc_inl1 && !sc_fb[FBW];
    // Not while the tags are cleared or swept: the install shares nothing with it.
    wire         sc_live = (ist == I_IDLE) || (ist == I_VRD) || (ist == I_VSEND)
                        || (ist == I_WR);
    wire         alloc_s = sc_live && sc_need && fb_free[FBW] && !alloc_h;

    // The scanner's next lane: the next one, or (SKIP) the next on another line.
    reg  [LNW:0] sk_nx;
    integer sk;
    always @(*) begin
        sk_nx = LANES;
        for (sk = LANES - 1; sk >= 0; sk = sk - 1) begin
            if ((sk > sc_lane) && (s_addr[32*sk + 5 +: 26] != sc_line)) sk_nx = sk;
        end
    end
    wire [LNW:0] sc_step = (SKIP != 0) ? sk_nx : ({1'b0, sc_lane} + 1'b1);
    // The entry is done, or gone: the next entry, or back to the head after LOOK
    // or at the queue's last entry (which covers an entry that is gone).
    wire         sc_wrap = (sc_step >= LANES) || !sc_have;
    wire         sc_back = (sc_off == LOOK - 1) || (q_n <= {1'b0, sc_off} + 1'b1);

    // ======================= install and sweep =========================
    // The installable buffer: arrived, and not the line the head is serving.
    reg [FBW:0] inst_c;
    integer ic;
    always @(*) begin
        inst_c = {1'b0, {FBW{1'b0}}};
        for (ic = MSHRS - 1; ic >= 0; ic = ic - 1) begin
            if ((fb_st[ic] == F_DATA)
                && !(((hst == H_PICK) || (hst == H_FB)) && (fb_line[ic] == cur_line))) begin
                inst_c = {1'b1, ic[FBW-1:0]};
            end
        end
    end
    wire fl_go  = fl_pend && (ist == I_IDLE) && (q_n == 0) && (hst == H_IDLE)
               && !fb_any;
    wire ins_go = (ist == I_IDLE) && !fl_go && inst_c[FBW] && !head_l1
               && (hst != H_L1) && (hst != H_SW);
    wire [IDX-1:0] ins_idx = fb_line[ins_fb][IDX-1:0];
    wire           in_ins  = (ist == I_VRD) || (ist == I_VSEND) || (ist == I_WR);
    wire           in_swp  = (ist == I_SRD) || (ist == I_SSEND);

    assign tag_ra_s = in_ins ? ins_idx : scan;
    assign dat_ra   = in_ins ? ins_idx : in_swp ? scan : cur_line[IDX-1:0];

    wire vic_dirty = tag_s[TW-1] && tag_s[TW-2];
    wire sw_dirty  = tag_s[TW-2];                     // dirty implies valid
    wire head_wr   = (hst == H_WT) && !wr_v;
    wire vic_send  = (ist == I_VSEND) && vic_dirty && !wr_v && !head_wr;
    wire sw_step   = (ist == I_SSEND) && (!sw_dirty || (!wr_v && !head_wr));

    // Every array write is a function of state registers.
    assign dat_we = (hst == H_SW) || (ist == I_WR);
    assign dat_wa = (ist == I_WR) ? ins_idx : cur_line[IDX-1:0];
    assign dat_wd = (ist == I_WR) ? fb_data[ins_fb] : st_line;
    assign tag_we = (hst == H_SW) || (ist == I_WR) || (ist == I_CLR)
                 || (sw_step && (sw_dirty || fl_inval));
    assign tag_wa = (ist == I_WR) ? ins_idx
                  : ((ist == I_CLR) || (ist == I_SSEND)) ? scan
                                                         : cur_line[IDX-1:0];
    assign tag_wd = (ist == I_WR)    ? {1'b1, fb_dirty[ins_fb], fb_line[ins_fb][25:IDX]}
                  : (ist == I_CLR)   ? {TW{1'b0}}
                  : (ist == I_SSEND) ? (fl_inval ? {TW{1'b0}} : {2'b10, tag_s[TAGW-1:0]})
                                     : {1'b1, (WB != 0), cur_line[25:IDX]};

    // ======================= the memory port ===========================
    // A write not yet sent is either in `wr_*` or, write-through, in `st_line`
    // while the head waits in H_WT; the line may already have been evicted.
    // Checked at load and good while offered: a write is of a line in the L1 or
    // the head's, and a requested line is neither until its own fill installs.
    wire rd_block = (wr_v && (wr_line == fb_line[fb_req[FBW-1:0]]))
                 || ((hst == H_WT) && (cur_line == fb_line[fb_req[FBW-1:0]]));
    assign rd_v    = rq_v;
    assign rd_line = fb_line[rq_fb];
    assign rd_tag  = TAG_BASE + {{(8-FBW){1'b0}}, rq_fb};
    wire [7:0] rsp_i    = rsp_tag - TAG_BASE;
    wire       rsp_mine = rsp_v && (rsp_i < MSHRS);

    assign busy = (q_n != 0) || (hst != H_IDLE) || (ist != I_IDLE) || fb_any
               || ret_v || fl_pend || wr_v;

    // ======================= the sequential part ========================
    integer k;
    always @(posedge clk) begin
        if (!resetn) begin
            hst      <= H_IDLE;
            ist      <= I_CLR;              // the tag arrays have no reset
            scan     <= {IDX{1'b0}};
            fl_pend  <= 1'b0;
            fl_inval <= 1'b0;
            todo     <= {LANES{1'b0}};
            ret_v    <= 1'b0;
            wr_v     <= 1'b0;
            sc_off   <= {QW{1'b0}};
            sc_ptr   <= {QW{1'b0}};
            sc_lane  <= {LNW{1'b0}};
            c_v      <= 1'b0;
            rq_v     <= 1'b0;
            acc_ctr  <= 32'd0;
            fill_ctr <= 32'd0;
            for (k = 0; k < MSHRS; k = k + 1) begin
                fb_st[k]    <= F_FREE;
                fb_dirty[k] <= 1'b0;
            end
        end else begin
            if (ret_v && ret_ready) ret_v <= 1'b0;
            if (wr_v && wr_ready)   wr_v  <= 1'b0;
            if (flush || inval) begin
                fl_pend  <= 1'b1;
                fl_inval <= fl_inval || inval;
            end

            // ---- fill buffers ----
            if (rd_v && rd_ready) fb_st[rq_fb] <= F_WAIT;
            if (!rq_v || rd_ready) begin
                rq_v  <= fb_req[FBW] && !rd_block;
                rq_fb <= fb_req[FBW-1:0];
            end
            if (rsp_mine) begin
                fb_st[rsp_i[FBW-1:0]]   <= F_DATA;
                fb_data[rsp_i[FBW-1:0]] <= rsp_data;
            end
            if (alloc_h || alloc_s) begin
                fb_st[fb_free[FBW-1:0]]    <= F_REQ;
                fb_line[fb_free[FBW-1:0]]  <= alloc_h ? cur_line : c_line;
                fb_dirty[fb_free[FBW-1:0]] <= 1'b0;
                fill_ctr <= fill_ctr + 32'd1;
            end

            // ---- the scanner waits on a line it cannot yet get a buffer for ----
            // A pop moves every entry one place toward the head: the offset
            // follows, and an entry popped under the scanner restarts it.
            if (sc_live && !((HOLD != 0) && sc_need && !alloc_s)) begin
                c_v    <= sc_on;
                c_line <= sc_line;
                if (sc_wrap) begin
                    sc_lane <= {LNW{1'b0}};
                    if (sc_back) begin
                        sc_ptr <= pop ? nextp(q_hd) : q_hd;
                        sc_off <= {QW{1'b0}};
                    end else begin
                        sc_ptr <= nextp(sc_ptr);
                        sc_off <= pop ? sc_off : sc_off + 1'b1;
                    end
                end else if (pop && (sc_off == 0)) begin
                    sc_lane <= {LNW{1'b0}};
                    sc_ptr  <= nextp(q_hd);
                end else begin
                    sc_lane <= sc_step[LNW-1:0];
                    if (pop) sc_off <= sc_off - 1'b1;
                end
            end else if (pop) begin
                if (sc_off == 0) begin
                    sc_lane <= {LNW{1'b0}};
                    sc_ptr  <= nextp(q_hd);
                end else begin
                    sc_off <= sc_off - 1'b1;
                end
            end

            // ---- the head ----
            case (hst)
                H_IDLE: if ((q_n != 0) && (ist != I_CLR)) begin
                    todo <= q_mask[q_hd];
                    res  <= {VW{1'b0}};
                    hst  <= H_LEAD;
                end

                H_LEAD: if (todo == {LANES{1'b0}}) begin
                    hst <= H_RET;
                end else begin
                    cur_same <= same_c;
                    cur_line <= lead_line;
                    hst      <= H_PICK;
                end

                // In a buffer: served from it once arrived. In the L1: the data
                // array is read this cycle. Neither: take a buffer and wait here.
                H_PICK: if (cfb[FBW]) begin
                    if ((fb_st[cfb[FBW-1:0]] == F_DATA) && !cfb_busy) begin
                        cur_fb <= cfb[FBW-1:0];
                        hst    <= H_FB;
                    end
                end else if (head_l1) begin
                    hst <= H_L1;
                end

                H_L1, H_FB: begin
                    acc_ctr <= acc_ctr + 32'd1;
                    todo    <= todo & ~cur_same;
                    for (k = 0; k < LANES; k = k + 1) begin
                        if (cur_same[k] && !h_st) begin
                            res[32*k +: 32] <= ld_word(base, h_addr[32*k +: 32], h_w, h_sx);
                        end
                    end
                    st_line <= merged;
                    if (h_st && (hst == H_FB)) begin
                        fb_data[cur_fb]  <= merged;
                        fb_dirty[cur_fb] <= fb_dirty[cur_fb] || (WB != 0);
                    end
                    hst <= !h_st         ? H_LEAD
                         : (hst == H_L1) ? H_SW
                         : (WB != 0)     ? H_LEAD
                                         : H_WT;
                end

                H_SW: hst <= (WB != 0) ? H_LEAD : H_WT;

                // Write-through: the line goes out before the next access.
                H_WT: if (head_wr) begin
                    wr_v    <= 1'b1;
                    wr_line <= cur_line;
                    wr_data <= st_line;
                    hst     <= H_LEAD;
                end

                H_RET: if (!ret_v || ret_ready) begin
                    ret_v    <= 1'b1;
                    ret_wave <= q_wave[q_hd];
                    ret_rd   <= q_rd[q_hd];
                    ret_ld   <= !h_st;
                    ret_mask <= q_mask[q_hd];
                    ret_data <= res;
                    hst      <= H_IDLE;
                end

                default: hst <= H_IDLE;
            endcase

            // ---- install and sweep ----
            case (ist)
                I_IDLE: if (fl_go) begin
                    scan <= {IDX{1'b0}};
                    ist  <= I_SRD;
                end else if (ins_go) begin
                    ins_fb <= inst_c[FBW-1:0];
                    ist    <= I_VRD;
                end

                I_VRD: ist <= I_VSEND;

                I_VSEND: if (vic_send) begin
                    wr_v    <= 1'b1;
                    wr_line <= {tag_s[TAGW-1:0], ins_idx};
                    wr_data <= dat_q;
                    ist     <= I_WR;
                end else if (!vic_dirty) begin
                    ist <= I_WR;
                end

                I_WR: begin
                    fb_st[ins_fb] <= F_FREE;
                    ist <= I_IDLE;
                end

                I_CLR: begin
                    scan <= scan + 1'b1;
                    if (scan == LINES - 1) ist <= I_IDLE;
                end

                I_SRD: ist <= I_SSEND;

                I_SSEND: if (sw_step) begin
                    if (sw_dirty) begin
                        wr_v    <= 1'b1;
                        wr_line <= {tag_s[TAGW-1:0], scan};
                        wr_data <= dat_q;
                    end
                    scan <= scan + 1'b1;
                    ist  <= (scan == LINES - 1) ? I_DRAIN : I_SRD;
                end

                I_DRAIN: if (!wr_v && wr_idle) begin
                    fl_pend  <= flush || inval;
                    fl_inval <= inval;
                    ist      <= I_IDLE;
                end

                default: ist <= I_IDLE;
            endcase
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) if (resetn) begin
        if (rsp_v && !rsp_mine) begin
            $display("%0t ERROR kht_lsu: read response txn %0h names no fill buffer",
                     $time, rsp_tag);
        end
        if (rsp_mine && (fb_st[rsp_i[FBW-1:0]] != F_WAIT)) begin
            $display("%0t ERROR kht_lsu: response for fill buffer %0d in state %0d -- a line is overwritten",
                     $time, rsp_i, fb_st[rsp_i[FBW-1:0]]);
        end
        if (req_v && !req_ready) begin
            $display("%0t ERROR kht_lsu: an instruction offered to a full queue", $time);
        end
        if ((hst == H_SW) && (ist != I_IDLE)) begin
            $display("%0t ERROR kht_lsu: a store hit and an install share the arrays", $time);
        end
    end
`endif

endmodule

`default_nettype wire

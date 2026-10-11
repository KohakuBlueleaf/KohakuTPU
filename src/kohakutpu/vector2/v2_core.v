// The V2 vector core: one instruction stream, four decoupled engines.
//
// Same ports as vec_core, so vec_cu frames it unchanged. docs/projects/
// kohakutpu/vector-core-v2.md is the ISA and the design.
//
//   FRONT END  fetch, decode, scalar state, loops; dispatches one instruction
//              a cycle into the queue of the engine that runs it.
//   FILL  (FQ) VFILL walks (v2_agu walker 0); DRAIN (DQ) VDRAIN walks
//              (walker 1): a load and a send run at once.
//   UNPACK(UQ, UQ1) L1 -> GT4 -> fp16/fp32 -> E8 -> register file, a word a
//              cycle, from whichever of its queues can start (UQ1: DUALQ).
//   MATH  (MQ) one beat a cycle into v2_lanes.
//   PACK  (PQ, PQ1) register file -> E8 -> fp16/fp32 -> GT4 -> L1, a word a
//              cycle, likewise.
//
// REGISTER HAZARDS never stall dispatch. Each register counts, per access kind,
// how many were dispatched and how many have finished: W (all writes), UW
// (unpack writes), MR (math reads), PR (pack reads). A queue entry carries the
// dispatched counts it must wait for, read at its own dispatch, so it waits
// for exactly the OLDER accesses; its engine starts it once each finished
// count reaches its snapshot. One W serves every RAW because the writes to a
// register finish in dispatch order: math in its pipeline, and every write
// (math or unpack, either unpack queue) waits for the register's older ones;
// pack reads of a register likewise finish in order across the pack queues.
//
// L1 HAZARDS are the program's: VSYNC makes one engine queue (or the front
// end) wait until another kind has finished every instruction dispatched
// before the VSYNC, or before a mark (VSYNC W_MK).
//
// L1 is two banks (word address MSB), each one read and one write port. Fill
// writes win their bank and pack yields; unpack reads win and drain yields.

`default_nettype none

module v2_core #(
    parameter integer MODEL      = 1,
    parameter integer VLMAX      = 128,
    parameter integer IMEM_DEPTH = 1024,       // 512 or 1024; RUN starts below 512
    parameter integer L1_DEPTH   = 512,
    parameter         L1_PRIM    = "block",
    parameter         RF_PRIM    = "block",    // accepted for vec_cu; unused
    parameter integer RF_PAD     = 24,         // accepted for vec_cu; unused
    parameter integer RF_PACK    = 1,          // accepted for vec_cu; unused
    parameter integer AW         = 40,
    parameter integer QD         = 8,          // instruction queue depth
    // 1: a second unpack and pack queue (UQ1, PQ1). 0: an instruction naming
    // queue 1 runs on queue 0, and a VSYNC on K_U1/K_P1 waits on K_U/K_P.
`ifdef V2_DUALQ
    parameter integer DUALQ      = 1,
`else
    parameter integer DUALQ      = 0,
`endif
    parameter integer HAS_EXPD   = 1,
    parameter integer TAU        = 8,
    // VPACK mode 3, MX7: a register to one MXFP7 L1 entry (v2_mxq).
    parameter integer HAS_MX     = 1,
    // The operand crossbar (v2_xbar): 0 one 16:1 select a lane, 1 two 4:1 levels.
    parameter integer XNET       = 1
)(
    input  wire         clk,
    input  wire         rst,

    input  wire         ld_en,
    input  wire         ld_kind,
    input  wire [8:0]   ld_addr,
    input  wire [AW-1:0] ld_data,

    input  wire         start,
    input  wire [8:0]   start_pc,
    output reg          busy,
    output reg          halted,
    output reg          fault,
    output reg  [7:0]   fault_code,
    output reg  [31:0]  cycles,

    output reg          rd_req_valid,
    output reg  [AW-1:0] rd_req_addr,
    output reg  [8:0]   rd_req_tag,
    output reg  [7:0]   rd_req_cnt,
    input  wire         rd_req_ready,
    input  wire         rr_valid,
    input  wire [8:0]   rr_tag,
    input  wire [255:0] rr_data,

    input  wire         cd_valid,
    input  wire [8:0]   cd_addr,
    input  wire [255:0] cd_data,
    input  wire         cd_fault,

    output reg          wr_req_valid,
    output reg  [AW-1:0] wr_req_addr,
    output reg  [255:0] wr_req_data,
    output reg          wr_req_first,
    output reg  [3:0]   wr_req_cnt,
    input  wire         wr_req_ready,

    output reg          nd_valid,
    output reg  [3:0]   nd_x,
    output reg  [3:0]   nd_y,
    output reg  [3:0]   nd_buf,
    output reg  [15:0]  nd_off,
    output reg  [7:0]   nd_len,
    output reg          nd_sig,
    output reg  [7:0]   nd_ack,
    output reg  [1:0]   nd_mesh,
    output reg  [7:0]   nd_fin
);
    // ================================================== encoding
    localparam [4:0] O_EXP2D = 5'h12, O_VCFG = 5'h13, O_VUNPK = 5'h14;
    localparam [4:0] O_VPACK = 5'h15, O_VSYNC = 5'h16, O_VSKIPZ = 5'h17;
    localparam [4:0] O_VSETVL = 5'h18, O_VSETI = 5'h1A, O_VLOOP = 5'h1B;
    localparam [4:0] O_VBAR = 5'h1C, O_VFILL = 5'h1D, O_VDRAIN = 5'h1E;
    localparam [4:0] O_VHALT = 5'h1F;

    localparam [1:0] SRC_V = 2'd0, SRC_S = 2'd1, SRC_K = 2'd3;
    localparam [1:0] MD_F16 = 2'd0, MD_GT4 = 2'd1, MD_F32 = 2'd2, MD_MX7 = 2'd3;

    // VSYNC: who waits, and on whom. W_A is the fill queue, W_D the drain
    // queue; W_MK records a mark (a kind and its dispatched count).
    localparam [2:0] W_FE = 3'd0, W_U = 3'd1, W_M = 3'd2, W_P = 3'd3, W_A = 3'd4;
    localparam [2:0] W_MK = 3'd5, W_D = 3'd6;
    // K_U / K_P count unpack / pack queue 0, K_U1 / K_P1 queue 1.
    localparam [2:0] K_F = 3'd0, K_U = 3'd1, K_M = 3'd2, K_P = 3'd3, K_D = 3'd4;
    localparam [2:0] K_U1 = 3'd5, K_P1 = 3'd6;

    localparam [7:0] F_OPCODE = 8'd4, F_LEN = 8'd5, F_LOOP = 8'd6, F_VL = 8'd7;
    localparam [7:0] F_CUDATA = 8'd9, F_GT4 = 8'd10, F_SRC = 8'd11, F_MODE = 8'd12;
    localparam [7:0] F_SYNC = 8'd13;

    localparam [23:0] E8_ONE = 24'h3F8000;
    localparam integer LAW   = $clog2(L1_DEPTH);
    localparam integer PCW   = $clog2(IMEM_DEPTH);
    localparam integer HD    = L1_DEPTH / 2;           // words per L1 bank
    localparam integer L1_LAT = (L1_PRIM == "ultra") ? 2 : 1;
    localparam integer DR_TAP = L1_LAT;

    integer ii;

    // ================================================== soft reset
    // `start` clears every queue and engine; the register file and L1 keep.
    reg  srst;
    wire erst = rst | srst;

    // ================================================== front end state
    reg  [PCW-1:0] f_pc, d_pc;
    reg         f_v, d_v, run;
    reg  [31:0] ir;
    reg  [23:0] sreg [0:15];
    reg  [23:0] kreg [0:3];
    reg  [7:0]  vl;
    reg  [PCW-1:0] lp_top, lp_end;
    reg  [23:0] lp_cnt;
    reg         lp_act;
    reg  [15:0] c_chk;
    reg  [14:0] c_xb;
    reg  [LAW-1:0] c_uptr, c_uinc, c_pptr, c_pinc;
    // A walk's word w is at base + (w % 4) * s0 + (w / 4) * s1; {s1, s0}.
    reg  [2*LAW-1:0] c_ustr, c_pstr;
    localparam [2*LAW-1:0] STR_FLAT = (4 << LAW) | 1;     // word w at base + w
    // DMA: a `rel` VFILL/VDRAIN adds its descriptor's offset (bytes), which
    // then advances by that descriptor's increment. The offset is as wide as
    // an address, so a stream of any length stays inside one RUN.
    reg  [AW-1:0]      a_off [0:7];
    reg  signed [17:0] a_inc [0:7];
    reg         cd_err;

    // Instructions dispatched, and finished, per kind (K_*): VSYNC compares.
    reg  [15:0] n_disp [0:6];
    reg  [15:0] n_done [0:6];
    // Marks: a VSYNC can wait for "everything of kind mk_on dispatched before
    // mark i", whatever has been dispatched since.
    reg  [2:0]  mk_on  [0:7];
    reg  [15:0] mk_tgt [0:7];
    // For a front-end VSYNC, both as registers: n_disp - n_done per kind
    // (entry 7 stays 0), and per mark the completions of its kind still owed.
    reg  [7:0]  n_out  [0:7];
    reg  [7:0]  mk_cnt [0:7];

    // ================================================== memories
    wire [31:0] im_q;
    reg  [PCW-1:0] im_addr;
    // A load's address bit 9 rides in ld_data[32] (vec_cu: flit bit 242).
    wire [9:0]  im_wa = {ld_data[32], ld_addr};
    kohaku_sdpram #(.WIDTH(32), .DEPTH(IMEM_DEPTH), .MEM_PRIM("block"), .READ_LAT(1)) u_imem (
        .clk(clk), .wr_en(ld_en && !ld_kind), .wr_addr(im_wa[PCW-1:0]), .wr_data(ld_data[31:0]),
        .rd_en(1'b1), .rd_addr(im_addr), .rd_data(im_q));

    reg              l1_we [0:1];
    reg  [LAW-2:0]   l1_wa [0:1];
    reg  [255:0]     l1_wd [0:1];
    reg  [LAW-2:0]   l1_ra [0:1];
    wire [255:0]     l1_q  [0:1];
    genvar gb;
    generate
    for (gb = 0; gb < 2; gb = gb + 1) begin : g_l1
        kohaku_sdpram #(.WIDTH(256), .DEPTH(HD), .MEM_PRIM(L1_PRIM), .READ_LAT(L1_LAT)) u_l1 (
            .clk(clk), .wr_en(l1_we[gb]), .wr_addr(l1_wa[gb]), .wr_data(l1_wd[gb]),
            .rd_en(1'b1), .rd_addr(l1_ra[gb]), .rd_data(l1_q[gb]));
    end
    endgenerate

    // ================================================== decode
    wire [4:0] d_op  = ir[31:27];
    wire [4:0] d_vd  = ir[26:22], d_va = ir[21:17], d_vb = ir[16:12], d_vc = ir[11:7];
    wire [1:0] d_sa  = ir[6:5], d_sb = ir[4:3], d_sc = ir[2:1];
    wire       d_om  = ir[0];
    wire [3:0] d_csel = ir[26:23];
    wire [22:0] d_cpay = ir[22:0];
    wire [1:0] d_mode = ir[21:20];
    wire [2:0] d_n    = ir[19:17];               // chunks - 1
    wire [2:0] d_cb   = ir[16:14];
    wire       d_rel  = ir[13];
    wire [12:0] d_off = ir[12:0];
    wire [2:0] d_wt   = ir[26:24], d_on_ir = ir[23:21];
    wire [2:0] d_on   = (DUALQ != 0) ? d_on_ir : (d_on_ir == K_U1) ? K_U
                      : (d_on_ir == K_P1) ? K_P : d_on_ir;
    wire [5:0] d_slack = ir[20:15];
    wire       d_mk    = ir[14];                 // wait on mark d_mid, not on d_on
    wire [2:0] d_mid   = ir[13:11];
    wire [9:0] d_body = ir[16:7];
    wire [7:0] d_skip = ir[7:0];
    // VFILL/VDRAIN: exactly vec_core's layout, so hw/vector.py encodes both.
    wire [2:0] d_ad   = ir[23:21];
    wire [13:0] d_l1  = ir[13:0];

    wire is_math = (d_op <= O_EXP2D);
    wire is_cmp  = (d_op >= 5'h0B) && (d_op <= 5'h0D);
    wire [4:0] alu_op = (d_op == O_EXP2D) ? 5'd20
                      : (d_op >= 5'h0E && d_op <= 5'h11) ? (d_op + 5'd2) : d_op;
    // Which operands an op reads. An unread one becomes K at dispatch, so it
    // raises no hazard and holds no read count. With `xc` the c operand is the
    // crossbar output of b's register, so an op reading c reads that register.
    wire op_b = (d_op == 5'h05) || (d_op == 5'h06) || (d_op == 5'h07) || (d_op == 5'h08)
             || (d_op == 5'h09) || (d_op == 5'h0A) || is_cmp || (d_op == O_EXP2D);
    wire op_c = (d_op == 5'h03) || (d_op == 5'h04) || (d_op == 5'h06) || (d_op == 5'h07)
             || (d_op == 5'h0A);
    wire xc_now = d_om && c_xb[10];
    wire [1:0] e_sa_d = d_sa;
    wire [1:0] e_sb_d = (op_b || (xc_now && op_c)) ? d_sb : SRC_K;
    wire [1:0] e_sc_d = op_c ? d_sc : SRC_K;
    wire uses_a = (e_sa_d == SRC_V), uses_b = (e_sb_d == SRC_V), uses_c = (e_sc_d == SRC_V);

    function [23:0] pick;
        input [1:0] sel;
        input [4:0] rn;
        begin
            pick = (sel == SRC_S) ? sreg[rn[3:0]]
                 : (sel == SRC_K) ? kreg[rn[1:0]] : 24'd0;
        end
    endfunction

    // ================================================== scoreboard
    // Dispatched (d) and finished (c) counts, modulo 64: at most 31 of a kind
    // are outstanding on one register (8 queued plus a pipeline of VL-1 ops).
    reg [5:0] Wd  [0:31], Wc  [0:31];     // writes, math and unpack
    reg [5:0] UWd [0:31], UWc [0:31];     // unpack writes
    reg [5:0] MRd [0:31], MRc [0:31];     // math reads
    reg [5:0] PRd [0:31], PRc [0:31];     // pack reads
    // Compares ISSUED and not yet retired: math issues in order, so these are
    // all older than the head.
    reg [4:0] pP [0:3];

    // `c` has reached snapshot `s`.
    function reached;
        input [5:0] c;
        input [5:0] s;
        reg   [5:0] df;
        begin
            df = c - s;
            reached = !df[5];
        end
    endfunction

    // ================================================== queues
    // MQ entry, MSB first: aop vd va vb vc sa sb sc ka kb kc nb tl chk xb cmp
    // expd wr, then the snapshots Wd of a, b, c and UWd, PRd of vd.
    localparam integer MQW = 5 + 20 + 6 + 72 + 4 + 4 + 16 + 15 + 3 + 30;
    // UQ/PQ entry: sync vreg mode n cb addr s1 s0 snap | sync: on target. An
    // unpack's snapshot is Wd MRd PRd of vd, a pack's Wd of vs.
    localparam integer SNW = 24;
    localparam integer UQW = 1 + 5 + 2 + 3 + 3 + 3*LAW + SNW;
    // FQ (fills) / DQ (drains) entry: sync offset ir | sync: on target
    localparam integer AQW = 1 + AW + 32;

    reg               mq_push, uq_push, pq_push, fq_push, dq_push;
    reg  [MQW-1:0]    mq_din;
    reg  [UQW-1:0]    uq_din, pq_din;
    reg  [AQW-1:0]    aq_din;
    wire              mq_pop, uq_pop, pq_pop, fq_pop, dq_pop;
    wire [MQW-1:0]    mq_q;
    wire [UQW-1:0]    uq_q, pq_q;
    wire [AQW-1:0]    fq_q, dq_q;
    wire              mq_e, uq_e, pq_e, fq_e, dq_e, mq_f, uq_f, pq_f, fq_f, dq_f;

    v2_fifo #(.W(MQW), .D(QD)) u_mq (.clk(clk), .rst(erst), .push(mq_push), .din(mq_din),
        .pop(mq_pop), .dout(mq_q), .empty(mq_e), .full(mq_f), .count());
    // Unpack and pack each have two queues; an instruction names its own
    // (d_q). The engine serves queue 0's head when it can start, else queue
    // 1's, so one stream waiting on L1 data does not hold the other.
    wire              d_q = (DUALQ != 0)
                         && (((d_op == O_VUNPK) || (d_op == O_VPACK)) ? ir[12] : ir[10]);
    wire [UQW-1:0]    uq0_q, uq1_q, pq0_q, pq1_q;
    wire              uq0_e, uq1_e, pq0_e, pq1_e, uq0_f, uq1_f, pq0_f, pq1_f;
    wire              uq0_pop, uq1_pop, pq0_pop, pq1_pop;
    v2_fifo #(.W(UQW), .D(QD)) u_uq (.clk(clk), .rst(erst), .push(uq_push && !d_q),
        .din(uq_din), .pop(uq0_pop), .dout(uq0_q), .empty(uq0_e), .full(uq0_f), .count());
    v2_fifo #(.W(UQW), .D(QD)) u_pq (.clk(clk), .rst(erst), .push(pq_push && !d_q),
        .din(pq_din), .pop(pq0_pop), .dout(pq0_q), .empty(pq0_e), .full(pq0_f), .count());
    generate
    if (DUALQ != 0) begin : g_q1
        v2_fifo #(.W(UQW), .D(QD)) u_uq1 (.clk(clk), .rst(erst), .push(uq_push && d_q),
            .din(uq_din), .pop(uq1_pop), .dout(uq1_q), .empty(uq1_e), .full(uq1_f), .count());
        v2_fifo #(.W(UQW), .D(QD)) u_pq1 (.clk(clk), .rst(erst), .push(pq_push && d_q),
            .din(pq_din), .pop(pq1_pop), .dout(pq1_q), .empty(pq1_e), .full(pq1_f), .count());
    end else begin : g_q1
        assign uq1_q = {UQW{1'b0}}; assign uq1_e = 1'b1; assign uq1_f = 1'b0;
        assign pq1_q = {UQW{1'b0}}; assign pq1_e = 1'b1; assign pq1_f = 1'b0;
    end
    endgenerate
    assign uq_e = uq0_e && uq1_e;
    assign pq_e = pq0_e && pq1_e;
    assign uq_f = d_q ? uq1_f : uq0_f;
    assign pq_f = d_q ? pq1_f : pq0_f;
    v2_fifo #(.W(AQW), .D(4)) u_fq (.clk(clk), .rst(erst), .push(fq_push), .din(aq_din),
        .pop(fq_pop), .dout(fq_q), .empty(fq_e), .full(fq_f), .count());
    v2_fifo #(.W(AQW), .D(4)) u_dq (.clk(clk), .rst(erst), .push(dq_push), .din(aq_din),
        .pop(dq_pop), .dout(dq_q), .empty(dq_e), .full(dq_f), .count());

    // A sync entry's condition: every instruction of kind `on` dispatched
    // before it has finished.
    function sync_ok;
        input [2:0]  on;
        input [15:0] tgt;
        begin
            sync_ok = ((n_done[on] - tgt) < 16'h8000);
        end
    endfunction

    // ================================================== front end: dispatch
    wire [3:0] nb_vl = vl[7:4] + {3'd0, |vl[3:0]};       // beats, 1..8

    // Who blocks this instruction now.
    wire gt4_bad = (d_mode == MD_GT4) && (d_n[1:0] != 2'b11);
    // MX7 packs a whole register: 8 chunks, chunk base 0 or 1 (1: B packing).
    wire mx_bad  = (HAS_MX == 0) || (d_n != 3'd7) || (d_cb[2:1] != 2'd0);

    wire all_idle;
    // A sync waits for kind s_on to reach s_tgt: from the instruction, or a mark.
    wire [15:0] d_tgt = n_disp[d_on] - {10'd0, d_slack};
    wire [2:0]  s_on  = d_mk ? mk_on[d_mid]  : d_on;
    wire [15:0] s_tgt = d_mk ? mk_tgt[d_mid] : d_tgt;

    // Slow conditions (front-end VSYNC, VSETVL, VSKIPZ, VSETI, VHALT) are
    // evaluated into registers; such an instruction leaves on its second cycle
    // in `ir` (d_old), so no counter or sreg read sits in front of dispatch.
    // Nothing else dispatches meanwhile, and what it waits on only moves its way.
    reg  d_old, sl_ok_q, sl_vl_q, sl_skip_q;
    wire [23:0] sr_va = sreg[d_va[3:0]];
    always @(posedge clk) begin
        sl_ok_q   <= (d_op == O_VSYNC) ? (d_mk ? (mk_cnt[d_mid] == 8'd0)
                                               : (n_out[d_on] <= {2'b0, d_slack}))
                   : (d_op == O_VSETI) ? (f_v && (f_pc == d_pc + 1'b1))
                   : all_idle;
        sl_vl_q   <= (sr_va == 24'd0) || (sr_va > VLMAX);
        sl_skip_q <= (sreg[d_vd[3:0]] == 24'd0);
    end

    reg  [7:0] fe_fault;      // nonzero: this instruction faults
    reg        fe_ok;         // this instruction leaves the front end now
    always @(*) begin
        fe_fault = 8'd0;
        fe_ok    = 1'b0;
        if (d_v && run) begin
            if (cd_err) begin
                fe_fault = F_CUDATA;
            end else if (is_math) begin
                if ((d_sa == 2'd2) || (d_sb == 2'd2) || (d_sc == 2'd2)) fe_fault = F_SRC;
                else fe_ok = !mq_f;
            end else begin
                case (d_op)
                    O_VCFG:   if ((d_csel == 4'd10) && ({1'b0, d_cpay[22:16]} >= VLMAX))
                                  fe_fault = F_VL;
                              else fe_ok = 1'b1;
                    O_VUNPK:  if (d_mode == 2'd3) fe_fault = F_MODE;
                              else if (gt4_bad) fe_fault = F_GT4;
                              else fe_ok = !uq_f;
                    O_VPACK:  if ((d_mode == MD_MX7) && mx_bad) fe_fault = F_MODE;
                              else if (gt4_bad) fe_fault = F_GT4;
                              else fe_ok = !pq_f;
                    O_VSYNC:  if (d_on > K_P1 || d_wt > W_D) fe_fault = F_SYNC;
                              else case (d_wt)
                                  W_FE: fe_ok = d_old && sl_ok_q;
                                  W_U:  fe_ok = !uq_f;
                                  W_M:  fe_ok = !mq_f;
                                  W_P:  fe_ok = !pq_f;
                                  W_MK: fe_ok = 1'b1;
                                  W_D:  fe_ok = !dq_f;
                                  default: fe_ok = !fq_f;
                              endcase
                    O_VBAR:   fe_ok = !uq_f;
                    O_VSKIPZ: fe_ok = d_old;
                    O_VSETVL: if (d_old && sl_vl_q) fe_fault = F_VL;
                              else fe_ok = d_old;
                    // The immediate is the word after; it must be on im_q.
                    O_VSETI:  fe_ok = d_old && sl_ok_q;
                    O_VLOOP:  if (lp_act) fe_fault = F_LOOP; else fe_ok = 1'b1;
                    O_VFILL:  fe_ok = !fq_f;
                    O_VDRAIN: fe_ok = !dq_f;
                    O_VHALT:  fe_ok = d_old && sl_ok_q;
                    default:  fe_fault = F_OPCODE;
                endcase
            end
        end
    end

    // A taken redirect re-presents the fetch address; the word in `ir` is dropped.
    wire        skip_take = (d_op == O_VSKIPZ) && sl_skip_q;
    wire        redirect  = fe_ok && ((d_op == O_VLOOP) || (d_op == O_VSKIPZ && skip_take));
    wire [PCW-1:0] redir_pc = d_pc + 1'b1 + ((d_op == O_VLOOP) ? 8'd0 : d_skip);
    wire        adv       = !d_v || fe_ok;

    // Loop-aware next fetch address; fetch is the only place a loop closes.
    wire [PCW-1:0] f_inc = f_pc + 1'b1;
    wire        lp_back = lp_act && (f_inc == lp_end) && (lp_cnt > 24'd1);
    wire        lp_exit = lp_act && (f_inc == lp_end) && (lp_cnt <= 24'd1);
    wire [PCW-1:0] f_next = lp_back ? lp_top : f_inc;

    always @(*) begin
        if (!run) im_addr = start_pc;
        else if (redirect) im_addr = redir_pc;
        else if (adv) im_addr = f_next;
        else im_addr = f_pc;
    end

    // ================================================== the engines' state
    // math
    reg               m_act;
    reg  [MQW-1:0]    m_e;
    reg  [2:0]        m_i;
    // unpack / pack
    reg               u_act, p_act;
    reg  [UQW-1:0]    u_e, p_e;
    reg  [3:0]        u_w, p_w;
    reg  [LAW-1:0]    u_ra, p_wa, u_gb, p_gb;
    reg  [3:0]        u_occ, p_occ;
    // DMA: the fill and the drain engine walk the same states.
    localparam [2:0] E_IDLE = 3'd0, E_AGW = 3'd1, E_W1 = 3'd2, E_W2 = 3'd3;
    localparam [2:0] E_W3 = 3'd4, E_MEM0 = 3'd5, E_RUN = 3'd6;
    reg  [2:0]        f_st, d_st;

    // ================================================== math engine
    wire [4:0]  h_aop, h_vd, h_va, h_vb, h_vc;
    wire [1:0]  h_sa, h_sb, h_sc;
    wire [71:0] h_k;
    wire [3:0]  h_nb, h_tl;
    wire [15:0] h_chk;
    wire [14:0] h_xb;
    wire        h_cmp, h_expd, h_wr;
    wire [5:0]  hs_a, hs_b, hs_c, hs_uw, hs_pr;
    assign {h_aop, h_vd, h_va, h_vb, h_vc, h_sa, h_sb, h_sc, h_k, h_nb, h_tl, h_chk, h_xb,
            h_cmp, h_expd, h_wr, hs_a, hs_b, hs_c, hs_uw, hs_pr} = mq_q;
    wire [1:0]  h_pm = h_xb[12:11], h_pr = h_xb[14:13];
    // A sync entry rides the MQ as aop 31 with the target in the constants.
    wire        h_sync = (h_aop == 5'd31);
    wire        h_haz  = ((h_sa == SRC_V) && !reached(Wc[h_va], hs_a))
                       || ((h_sb == SRC_V) && !reached(Wc[h_vb], hs_b))
                       || ((h_sc == SRC_V) && !reached(Wc[h_vc], hs_c))
                       || (h_wr && !(reached(UWc[h_vd], hs_uw) && reached(PRc[h_vd], hs_pr)))
                       || ((h_pm != 2'd0) && (|pP[h_pr]));

    wire [4:0]  e_aop, e_vd, e_va, e_vb, e_vc;
    wire [1:0]  e_sa, e_sb, e_sc;
    wire [71:0] e_k;
    wire [3:0]  e_nb, e_tl;
    wire [15:0] e_chk;
    wire [14:0] e_xb;
    wire        e_cmp, e_expd, e_wr;
    wire [29:0] e_snap;
    assign {e_aop, e_vd, e_va, e_vb, e_vc, e_sa, e_sb, e_sc, e_k, e_nb, e_tl, e_chk, e_xb,
            e_cmp, e_expd, e_wr, e_snap} = m_e;

    wire m_end  = m_act && ({1'b0, m_i} + 4'd1 == e_nb);
    wire m_free = !m_act || m_end;
    wire m_sync_go = !mq_e && h_sync && sync_ok(h_k[2:0], h_k[23:8]);
    wire m_take = !mq_e && !h_sync && !h_haz && m_free;
    assign mq_pop = m_take || (m_sync_go && m_free);

    // Chunk of beat i for an operand nibble {base[2:0], stride}.
    function [2:0] chk_of;
        input [3:0] nib;
        input [2:0] i;
        begin
            chk_of = nib[3:1] + (nib[0] ? i : 3'd0);
        end
    endfunction

    reg         iss_valid, iss_last, iss_wr, iss_cmp, iss_expd, iss_xc;
    reg  [4:0]  iss_op;
    reg  [7:0]  iss_ra, iss_rb, iss_rc, iss_wa;
    reg  [1:0]  iss_sa, iss_sb, iss_sc, iss_pr, iss_pm;
    reg  [23:0] iss_ka, iss_kb, iss_kc;
    reg  [2:0]  iss_xm;
    reg  [3:0]  iss_xk;
    reg  [15:0] iss_tm;

    wire [2:0] xb_xm = e_xb[2:0];
    wire [3:0] xb_xk = e_xb[6:3];
    wire [2:0] xb_sh = e_xb[9:7];
    wire       xb_xc = e_xb[10];
    wire [15:0] tm_part = (16'd1 << e_tl) - 16'd1;
    wire        last_part = ({1'b0, m_i} + 4'd1 == e_nb) && (e_tl != 4'd0);

    always @(posedge clk) begin
        if (erst) begin
            m_act <= 1'b0; m_i <= 3'd0; iss_valid <= 1'b0;
        end else begin
            iss_valid <= m_act;
            if (m_take) begin
                m_e <= mq_q; m_act <= 1'b1; m_i <= 3'd0;
            end else if (m_end) begin
                m_act <= 1'b0;
            end else if (m_act) begin
                m_i <= m_i + 3'd1;
            end
        end
        iss_last <= m_end;
        iss_op   <= e_aop;
        iss_ra   <= {e_va, chk_of(e_chk[3:0],   m_i)};
        iss_rb   <= {e_vb, chk_of(e_chk[7:4],   m_i)};
        iss_rc   <= {e_vc, chk_of(e_chk[11:8],  m_i)};
        iss_wa   <= {e_vd, chk_of(e_chk[15:12], m_i)};
        iss_sa <= e_sa; iss_sb <= e_sb; iss_sc <= e_sc;
        iss_ka <= e_k[71:48]; iss_kb <= e_k[47:24]; iss_kc <= e_k[23:0];
        iss_xm <= xb_xm;
        iss_xk <= (xb_xm == 3'd3) ? (xb_xk + ({1'b0, m_i} >> xb_sh)) : xb_xk;
        iss_xc <= xb_xc;
        iss_pm <= e_xb[12:11]; iss_pr <= e_xb[14:13];
        iss_wr <= e_wr; iss_cmp <= e_cmp; iss_expd <= e_expd;
        iss_tm <= last_part ? tm_part : 16'hFFFF;
    end

    // ================================================== lanes
    wire         uw_we;
    wire [7:0]   uw_wa;
    wire [383:0] uw_wd;
    wire [1:0]   mw_bank_next;
    reg  [7:0]   pk_ra;
    wire [383:0] pk_rd;
    wire         wb_last, wb_wr, wb_cmp, pipe_empty;
    wire [4:0]   wb_vreg;
    wire [1:0]   wb_pr;
    reg          stk_clr;
    wire [15:0]  sticky;

    v2_lanes #(.MODEL(MODEL), .HAS_EXPD(HAS_EXPD), .TAU(TAU), .XNET(XNET)) u_lanes (
        .clk(clk), .rst(erst),
        .iss_valid(iss_valid), .iss_last(iss_last), .iss_op(iss_op),
        .iss_ra(iss_ra), .iss_rb(iss_rb), .iss_rc(iss_rc),
        .iss_sa(iss_sa), .iss_sb(iss_sb), .iss_sc(iss_sc),
        .iss_ka(iss_ka), .iss_kb(iss_kb), .iss_kc(iss_kc),
        .iss_xm(iss_xm), .iss_xk(iss_xk), .iss_xc(iss_xc),
        .iss_wa(iss_wa), .iss_wr(iss_wr), .iss_cmp(iss_cmp),
        .iss_pr(iss_pr), .iss_pm(iss_pm), .iss_tmask(iss_tm), .iss_expd(iss_expd),
        .uw_we(uw_we), .uw_wa(uw_wa), .uw_wd(uw_wd), .mw_bank_next(mw_bank_next),
        .pk_ra(pk_ra), .pk_rd(pk_rd),
        .wb_last(wb_last), .wb_vreg(wb_vreg), .wb_wr(wb_wr), .wb_cmp(wb_cmp),
        .wb_pr(wb_pr), .pipe_empty(pipe_empty),
        .stk_clr(stk_clr), .sticky(sticky)
    );

    // ================================================== unpack engine
    // Entry fields.
    // Each queue's head: a sync pops on its own once met; an unpack starts
    // when its registers are clear. Older MATH writes are Wc - UWc against
    // Wd - UWd; older unpack writes (either queue) UWc against UWd; then
    // math reads and pack reads. Snapshot {Wd, UWd, MRd, PRd}.
    function u_clear;
        input [UQW-1:0] q;
        reg   [4:0] vd;
        begin
            vd = q[UQW-2 -: 5];
            u_clear = reached(Wc[vd] - UWc[vd], q[23:18] - q[17:12])
                   && reached(UWc[vd], q[17:12])
                   && reached(MRc[vd], q[11:6])
                   && reached(PRc[vd], q[5:0]);
        end
    endfunction
    wire u0_sync = !uq0_e && uq0_q[UQW-1];
    wire u1_sync = !uq1_e && uq1_q[UQW-1];
    wire u0_go   = u0_sync && sync_ok(uq0_q[2:0], uq0_q[18:3]);
    wire u1_go   = u1_sync && sync_ok(uq1_q[2:0], uq1_q[18:3]);
    wire u0_rdy  = !uq0_e && !u0_sync && u_clear(uq0_q);
    wire u1_rdy  = !uq1_e && !u1_sync && u_clear(uq1_q);
    wire u_sel   = (DUALQ != 0) && !u0_rdy;   // queue the engine takes from
    assign uq_q  = u_sel ? uq1_q : uq0_q;
    // Every waiting head is a sync entry.
    wire uh_sync = !(uq0_e && uq1_e) && (uq0_e || u0_sync) && (uq1_e || u1_sync);
    wire [4:0]        uh_vd   = uq_q[UQW-2 -: 5];
    wire              ue_f32  = (u_e[UQW-7 -: 2] == MD_F32);
    wire              ue_gt4  = (u_e[UQW-7 -: 2] == MD_GT4);
    wire [4:0]        ue_vd   = u_e[UQW-2 -: 5];
    wire [2:0]        ue_n    = u_e[UQW-9 -: 3];
    wire [2:0]        ue_cb   = u_e[UQW-12 -: 3];
    wire [LAW-1:0]    ue_s0   = u_e[SNW+LAW-1:SNW];
    wire [LAW-1:0]    ue_s1   = u_e[SNW+2*LAW-1:SNW+LAW];
    wire [3:0]        ue_nw   = ue_f32 ? {ue_n, 1'b1} : {1'b0, ue_n};   // words - 1

    // Issue one L1 word a cycle while the chunk budget lasts.
    wire u_room  = (u_occ < 4'd8);
    wire u_rd    = u_act && u_room;
    wire u_endw  = u_rd && (u_w == ue_nw);
    wire u_free  = !u_act || u_endw;
    wire u_take  = (u0_rdy || u1_rdy) && u_free;
    wire u_haz   = !u0_rdy && !u1_rdy && !(uq0_e && uq1_e) && !uh_sync;
    assign uq0_pop = (u_take && !u_sel) || u0_go;
    assign uq1_pop = (u_take && u_sel) || u1_go;
    reg  u_eq;                              // the running entry's queue

    // tag: vd chunk queue last half gt4 f32
    localparam integer UTW = 5 + 3 + 1 + 1 + 1 + 1 + 1;
    wire [2:0]  u_chunk = ue_cb + (ue_f32 ? u_w[3:1] : u_w[2:0]);
    wire [UTW-1:0] u_tag0 = {ue_vd, u_chunk, u_eq, (u_w == ue_nw), ue_f32 & u_w[0], ue_gt4, ue_f32};
    reg  [UTW-1:0] u_tag1, u_tag2;
    reg            u_v1, u_v2, u_bk1, u_bk2;

    // DMA's drain read, granted when unpack leaves its bank alone.
    wire           dr_want;
    wire [LAW-1:0] dr_ptr_w;
    wire           dr_gnt = dr_want && !(u_rd && (u_ra[LAW-1] == dr_ptr_w[LAW-1]));

    always @(posedge clk) begin
        if (erst) begin
            u_act <= 1'b0; u_w <= 4'd0; u_v1 <= 1'b0; u_v2 <= 1'b0;
        end else begin
            if (u_take) begin
                u_e  <= uq_q; u_act <= 1'b1; u_w <= 4'd0; u_eq <= u_sel;
                u_ra <= uq_q[SNW+3*LAW-1 -: LAW];
                u_gb <= uq_q[SNW+3*LAW-1 -: LAW];
            end else if (u_endw) begin
                u_act <= 1'b0;
            end else if (u_rd) begin
                u_w  <= u_w + 4'd1;
                if (u_w[1:0] == 2'd3) begin
                    u_gb <= u_gb + ue_s1;
                    u_ra <= u_gb + ue_s1;
                end else begin
                    u_ra <= u_ra + ue_s0;
                end
            end
            u_v1 <= u_rd;  u_v2 <= u_v1;
        end
        u_tag1 <= u_tag0; u_tag2 <= u_tag1;
        u_bk1  <= u_ra[LAW-1]; u_bk2 <= u_bk1;
    end

    // The read ports, registered as vec_core's: a read lands two cycles on.
    reg dr_bk1, dr_bk2;
    always @(posedge clk) begin
        for (ii = 0; ii < 2; ii = ii + 1) begin
            if (u_rd && (u_ra[LAW-1] == ii[0])) begin
                l1_ra[ii] <= u_ra[LAW-2:0];
            end else if (dr_gnt && (dr_ptr_w[LAW-1] == ii[0])) begin
                l1_ra[ii] <= dr_ptr_w[LAW-2:0];
            end
        end
        dr_bk1 <= dr_ptr_w[LAW-1]; dr_bk2 <= dr_bk1;
    end

    // GT4 on the raw words, then the converters.
    wire [255:0]   ug_out;
    wire [UTW-1:0] ug_tag;
    wire           ug_v, ug_busy;
    v2_gt4 #(.G(64), .TW(UTW)) u_ugt (
        .clk(clk), .rst(erst),
        .in_valid(u_v2), .in_gt4(u_tag2[1]), .in_data(l1_q[u_bk2]), .in_tag(u_tag2),
        .out_valid(ug_v), .out_data(ug_out), .out_tag(ug_tag), .busy(ug_busy));

    wire [383:0] ui_f16;
    wire [191:0] ui_f32;
    genvar e;
    generate
    for (e = 0; e < 16; e = e + 1) begin : g_ui16
        vec_cvt_f16_to_e8 u_c (.f16(ug_out[e*16 +: 16]), .e8(ui_f16[e*24 +: 24]));
    end
    for (e = 0; e < 8; e = e + 1) begin : g_ui32
        vec_cvt_f32_to_e8 u_c (.f32(ug_out[e*32 +: 32]), .e8(ui_f32[e*24 +: 24]));
    end
    endgenerate

    // FP32: the lo word's eight lanes wait for the hi word's.
    reg  [191:0] u_hold;
    reg          uf_push;
    reg  [8+2+384-1:0] uf_din;
    wire [8+2+384-1:0] uf_q;
    wire         uf_e, uf_pop;
    always @(posedge clk) begin
        if (erst) begin
            uf_push <= 1'b0;
        end else begin
            uf_push <= ug_v && !(ug_tag[0] && !ug_tag[2]);
        end
        if (ug_v && ug_tag[0] && !ug_tag[2]) begin
            u_hold <= ui_f32;
        end
        uf_din <= {ug_tag[UTW-1 -: 8], ug_tag[4], ug_tag[3],
                   ug_tag[0] ? {ui_f32, u_hold} : ui_f16};
    end

    v2_fifo #(.W(8+2+384), .D(8)) u_uf (.clk(clk), .rst(erst), .push(uf_push), .din(uf_din),
        .pop(uf_pop), .dout(uf_q), .empty(uf_e), .full(), .count());

    wire       uf_last = uf_q[384];
    wire       uf_qb   = uf_q[385];
    assign uw_wa  = uf_q[393:386];
    assign uw_wd  = uf_q[383:0];
    assign uf_pop = !uf_e && !mw_bank_next[uw_wa[0]];
    assign uw_we  = uf_pop;

    // A chunk is budgeted at its first word and released as it is written.
    wire u_chunk_iss = u_rd && (!ue_f32 || !u_w[0]);
    always @(posedge clk) begin
        if (erst) begin
            u_occ <= 4'd0;
        end else begin
            u_occ <= u_occ + (u_chunk_iss ? 4'd1 : 4'd0) - (uf_pop ? 4'd1 : 4'd0);
        end
    end

    // ================================================== pack engine
    // As unpack's: a pack starts once vs's older writes and older pack reads
    // (either queue) are done. Snapshot {Wd, PRd}.
    function p_clear;
        input [UQW-1:0] q;
        reg   [4:0] vs;
        begin
            vs = q[UQW-2 -: 5];
            p_clear = reached(Wc[vs], q[23:18]) && reached(PRc[vs], q[17:12]);
        end
    endfunction
    wire p0_sync = !pq0_e && pq0_q[UQW-1];
    wire p1_sync = !pq1_e && pq1_q[UQW-1];
    wire p0_go   = p0_sync && sync_ok(pq0_q[2:0], pq0_q[18:3]);
    wire p1_go   = p1_sync && sync_ok(pq1_q[2:0], pq1_q[18:3]);
    wire p0_rdy  = !pq0_e && !p0_sync && p_clear(pq0_q);
    wire p1_rdy  = !pq1_e && !p1_sync && p_clear(pq1_q);
    wire p_sel   = (DUALQ != 0) && !p0_rdy;
    assign pq_q  = p_sel ? pq1_q : pq0_q;
    wire ph_sync = !(pq0_e && pq1_e) && (pq0_e || p0_sync) && (pq1_e || p1_sync);
    wire [4:0]        ph_vs   = pq_q[UQW-2 -: 5];
    wire              pe_f32  = (p_e[UQW-7 -: 2] == MD_F32);
    wire              pe_gt4  = (p_e[UQW-7 -: 2] == MD_GT4);
    wire              pe_mx   = (HAS_MX != 0) && (p_e[UQW-7 -: 2] == MD_MX7);
    wire [4:0]        pe_vs   = p_e[UQW-2 -: 5];
    wire [2:0]        pe_n    = p_e[UQW-9 -: 3];
    wire [2:0]        pe_cb   = p_e[UQW-12 -: 3];
    wire [LAW-1:0]    pe_s0   = p_e[SNW+LAW-1:SNW];
    wire [LAW-1:0]    pe_s1   = p_e[SNW+2*LAW-1:SNW+LAW];
    // MX7 reads its 8 chunks twice (v2_mxq) and writes 4 words.
    wire [3:0]        pe_nw   = pe_mx ? 4'd15 : pe_f32 ? {pe_n, 1'b1} : {1'b0, pe_n};

    // MX7's words appear after its reads: they are reserved at its first read,
    // and no other mode starts while v2_mxq holds anything, so its words never
    // meet another entry's in the GT4 stage and retire in order.
    wire mx_busy;
    wire pq_mx  = (HAS_MX != 0) && (pq_q[UQW-7 -: 2] == MD_MX7);
    wire p_room = pe_mx ? ((p_w != 4'd0) || (p_occ <= 4'd4)) : (p_occ < 4'd8);
    wire p_rd   = p_act && p_room;
    wire p_endw = p_rd && (p_w == pe_nw);
    wire p_free = !p_act || p_endw;
    wire p_take = (p0_rdy || p1_rdy) && p_free && !(mx_busy && !pq_mx);
    wire p_haz  = !p0_rdy && !p1_rdy && !(pq0_e && pq1_e) && !ph_sync;
    assign pq0_pop = (p_take && !p_sel) || p0_go;
    assign pq1_pop = (p_take && p_sel) || p1_go;
    reg  p_eq;

    // tag: addr queue last half gt4 f32
    localparam integer PTW = LAW + 5;
    wire [PTW-1:0] p_tag0 = {p_wa, p_eq, (p_w == pe_nw), pe_f32 & p_w[0], pe_gt4, pe_f32};
    reg  [PTW-1:0] p_tag1, p_tag2, p_tag3;
    reg            p_v1, p_v2, p_v3;
    wire [2:0]     p_chunk = pe_mx ? p_w[2:0] : pe_cb + (pe_f32 ? p_w[3:1] : p_w[2:0]);
    // MX7 read tag, beside p_tag: {mx, pass, chunk}.
    reg  [4:0]     m_tag1, m_tag2, m_tag3, m_tag4;
    reg  [LAW-1:0] p_base;

    always @(posedge clk) begin
        if (erst) begin
            p_act <= 1'b0; p_w <= 4'd0; p_v1 <= 1'b0; p_v2 <= 1'b0; p_v3 <= 1'b0;
        end else begin
            if (p_take) begin
                p_e  <= pq_q; p_act <= 1'b1; p_w <= 4'd0; p_eq <= p_sel;
                p_wa <= pq_q[SNW+3*LAW-1 -: LAW];
                p_gb <= pq_q[SNW+3*LAW-1 -: LAW];
                p_base <= pq_q[SNW+3*LAW-1 -: LAW];
            end else if (p_endw) begin
                p_act <= 1'b0;
            end else if (p_rd) begin
                p_w  <= p_w + 4'd1;
                if (p_w[1:0] == 2'd3) begin
                    p_gb <= p_gb + pe_s1;
                    p_wa <= p_gb + pe_s1;
                end else begin
                    p_wa <= p_wa + pe_s0;
                end
            end
            p_v1 <= p_rd; p_v2 <= p_v1; p_v3 <= p_v2;
        end
        if (p_rd) begin
            pk_ra <= {pe_vs, p_chunk};
        end
        p_tag1 <= p_tag0; p_tag2 <= p_tag1; p_tag3 <= p_tag2;
        m_tag1 <= {pe_mx, p_w[3], p_w[2:0]};
        m_tag2 <= m_tag1; m_tag3 <= m_tag2; m_tag4 <= m_tag3;
    end

    // pk_rd answers p_tag3's read; convert, register, then GT4.
    wire [255:0] po_f16, po_f32lo, po_f32hi;
    generate
    for (e = 0; e < 16; e = e + 1) begin : g_po16
        v2_e8_to_f16 u_c (.e8(pk_rd[e*24 +: 24]), .f16(po_f16[e*16 +: 16]));
    end
    for (e = 0; e < 8; e = e + 1) begin : g_po32
        vec_cvt_e8_to_f32 u_l (.e8(pk_rd[e*24 +: 24]),     .f32(po_f32lo[e*32 +: 32]));
        vec_cvt_e8_to_f32 u_h (.e8(pk_rd[(e+8)*24 +: 24]), .f32(po_f32hi[e*32 +: 32]));
    end
    endgenerate

    reg            p_v4;
    reg  [255:0]   p_cv;
    reg  [PTW-1:0] p_tag4;
    always @(posedge clk) begin
        if (erst) begin
            p_v4 <= 1'b0;
        end else begin
            p_v4 <= p_v3;
        end
        p_cv   <= !p_tag3[0] ? po_f16 : (p_tag3[2] ? po_f32hi : po_f32lo);
        p_tag4 <= p_tag3;
    end

    wire           mx_v, mx_eq, mx_last;
    wire [255:0]   mx_word;
    wire [LAW-1:0] mx_addr;
    wire           p_mx4 = p_v4 && m_tag4[4];
    generate
    if (HAS_MX != 0) begin : g_mx
        v2_mxq #(.LAW(LAW)) u_mxq (
            .clk(clk), .rst(erst),
            .in_v(p_mx4), .in_pass(m_tag4[3]), .in_chunk(m_tag4[2:0]), .in_f16(p_cv),
            .in_bl(pe_cb[0]), .in_base(p_base), .in_s0(pe_s0), .in_eq(p_eq),
            .out_v(mx_v), .out_word(mx_word), .out_addr(mx_addr), .out_eq(mx_eq),
            .out_last(mx_last), .busy(mx_busy));
    end else begin : g_nomx
        assign mx_v = 1'b0; assign mx_word = 256'd0; assign mx_addr = {LAW{1'b0}};
        assign mx_eq = 1'b0; assign mx_last = 1'b0; assign mx_busy = 1'b0;
    end
    endgenerate

    wire [255:0]   pg_out;
    wire [PTW-1:0] pg_tag;
    wire           pg_v, pg_busy;
    // An MX7 word is a plain word: {addr, queue, last, f32 half, gt4, f32}.
    wire [PTW-1:0] pg_in_tag = mx_v ? {mx_addr, mx_eq, mx_last, 3'b000} : p_tag4;
    v2_gt4 #(.G(64), .TW(PTW)) u_pgt (
        .clk(clk), .rst(erst),
        .in_valid((p_v4 && !m_tag4[4]) || mx_v), .in_gt4(!mx_v && p_tag4[1]),
        .in_data(mx_v ? mx_word : p_cv), .in_tag(pg_in_tag),
        .out_valid(pg_v), .out_data(pg_out), .out_tag(pg_tag), .busy(pg_busy));

    wire [LAW+2+256-1:0] pf_q;
    wire          pf_e, pf_pop;
    v2_fifo #(.W(LAW+2+256), .D(8)) u_pf (.clk(clk), .rst(erst), .push(pg_v),
        .din({pg_tag[PTW-1 -: LAW], pg_tag[4], pg_tag[3], pg_out}),
        .pop(pf_pop), .dout(pf_q), .empty(pf_e), .full(), .count());
    wire [LAW-1:0] pf_addr = pf_q[LAW+257 -: LAW];
    wire           pf_qb   = pf_q[257];
    wire           pf_last = pf_q[256];

    wire fill_any  = rr_valid || cd_valid;
    wire [8:0] fill_at = rr_valid ? rr_tag : cd_addr;
    assign pf_pop = !pf_e && !(fill_any && (fill_at[LAW-1] == pf_addr[LAW-1]));

    always @(posedge clk) begin
        if (erst) begin
            p_occ <= 4'd0;
        end else begin
            p_occ <= p_occ + (!p_rd ? 4'd0 : !pe_mx ? 4'd1 : (p_w == 4'd0) ? 4'd4 : 4'd0)
                           - (pf_pop ? 4'd1 : 4'd0);
        end
    end

    // L1 writes: a fill (or peer CU_DATA) first, a pack word otherwise.
    always @(posedge clk) begin
        for (ii = 0; ii < 2; ii = ii + 1) begin
            if (rst) begin
                l1_we[ii] <= 1'b0;
            end else if (fill_any && (fill_at[LAW-1] == ii[0])) begin
                l1_we[ii] <= 1'b1;
                l1_wa[ii] <= fill_at[LAW-2:0];
                l1_wd[ii] <= rr_valid ? rr_data : cd_data;
            end else if (pf_pop && (pf_addr[LAW-1] == ii[0])) begin
                l1_we[ii] <= 1'b1;
                l1_wa[ii] <= pf_addr[LAW-2:0];
                l1_wd[ii] <= pf_q[255:0];
            end else begin
                l1_we[ii] <= 1'b0;
            end
        end
    end

    // ================================================== DMA engines
    // vec_core's VFILL / VDRAIN walks, as two engines with a queue and an
    // AGU walker each (walker 0 fills, walker 1 drains): a load and a send
    // run at once, and a sync in one queue does not hold the other.
    reg  [1:0]  ag_start;
    reg  [2:0]  f_sel, d_sel;
    reg  [AW-1:0] f_off, dd_off;
    wire [2*AW-1:0] ag_addr2;
    wire [63:0] ag_total2;
    wire [35:0] ag_stride2;
    wire [31:0] ag_left2;
    wire [AW-1:0] f_addr = ag_addr2[0 +: AW], d_addr = ag_addr2[AW +: AW];
    wire [31:0] f_total = ag_total2[31:0], d_total = ag_total2[63:32];
    wire signed [17:0] f_stride0 = ag_stride2[17:0], d_stride0 = ag_stride2[35:18];
    wire [15:0] f_left0 = ag_left2[15:0], d_left0 = ag_left2[31:16];
    reg  [LAW-1:0] f_l1, dd_l1;
    reg  [8:0]  f_left;
    reg  [8:0]  d_left;
    reg         c_v;
    reg  [AW-1:0] c_addr;
    reg  [7:0]  c_n;
    reg  [LAW-1:0] c_l1;
    reg  [15:0] fill_out;
    reg  [LAW-1:0] dr_ptr;
    reg  [8:0]  dr_left;
    reg  [DR_TAP:0] dr_v;
    reg  [3:0]  wb_left;
    reg         run_v;
    reg  [AW-1:0] run_addr, run_next;
    reg  [8:0]  run_tag;
    reg  [7:0]  run_cnt;

    wire        fh_sync = fq_q[AQW-1];
    wire [AW-1:0] fh_off = fq_q[AW+31:32];
    wire [31:0] fh_ir   = fq_q[31:0];
    wire        dh_sync = dq_q[AQW-1];
    wire [AW-1:0] dh_off = dq_q[AW+31:32];
    wire [31:0] dh_ir   = dq_q[31:0];

    wire dr_land = dr_v[DR_TAP];
    wire dr_room = !wr_req_valid || wr_req_ready;
    wire dr_take = dr_land && dr_room;
    wire dr_blk  = dr_land && !dr_room;
    wire [3:0] dr_al   = 4'd8 - {1'b0, d_addr[7:5]};
    wire [3:0] dr_run  = (d_stride0 != 18'sd32) ? 4'd1
                       : (d_left0 < 16'd8) ? d_left0[3:0] : 4'd8;
    wire [3:0] dr_mem  = (d_left < 9'd8) ? d_left[3:0] : 4'd8;
    wire [3:0] dr_b1   = (dr_al < dr_run) ? dr_al : dr_run;
    wire [3:0] dr_blen = (dr_b1 < dr_mem) ? dr_b1 : dr_mem;
    wire dr_bend = dr_take && !nd_valid
                && ((wb_left == 4'd0) ? (dr_blen == 4'd1) : (wb_left == 4'd1));
    assign dr_want  = (d_st == E_RUN) && (dr_left != 9'd0) && !dr_blk && !dr_bend;
    assign dr_ptr_w = dr_ptr;
    wire dr_iss = dr_gnt;

    // Walker 0 hands one chunk a cycle to c_*: up to 255 words of a contiguous
    // dimension 0 (one word otherwise), never crossing a 256-word L1 boundary.
    // The run (run_*) grows while the next chunk continues it.
    wire [8:0] fl_b   = 9'd256 - {1'b0, f_l1[7:0]};
    wire [8:0] fl_m   = (fl_b < f_left) ? fl_b : f_left;
    wire [7:0] fl_cap = fl_m[8] ? 8'd255 : fl_m[7:0];
    // The cap goes to the walker from a register; a contiguous step waits one
    // cycle after a chunk for it to catch up (a one-word step needs no cap).
    reg  [7:0] fl_cap_q;
    reg        fl_fresh;
    wire [31:0] ag_n;
    wire [1:0]  ag_c32;
    wire [7:0] fl_n   = ag_n[7:0];
    wire [8:0] run_sum = {1'b0, run_cnt} + {1'b0, c_n};
    wire run_ext  = run_v && (c_addr == run_next) && !run_sum[8] && (c_l1[7:0] != 8'd0);
    wire c_take   = c_v && (!run_v || run_ext);
    wire c_load   = (f_st == E_RUN) && (f_left != 9'd0) && (!c_v || c_take)
                 && (fl_fresh || !ag_c32[0]);
    always @(posedge clk) begin
        fl_cap_q <= fl_cap;
        fl_fresh <= !c_load && (f_st == E_RUN);
    end
    wire fill_emit = (f_st == E_RUN) && run_v && !rd_req_valid
                  && (c_v ? !run_ext : (f_left == 9'd0));

    v2_agu #(.AW(AW)) u_agu (
        .clk(clk), .rst(rst),
        .wr_en(ld_en && ld_kind), .wr_ad(ld_addr[5:3]), .wr_fld(ld_addr[2:0]),
        .wr_val(ld_data),
        .start(ag_start), .start_ok(), .sel({d_sel, f_sel}), .off({dd_off, f_off}),
        .step({dr_take, c_load}), .cap({16'd1, 8'd0, fl_cap_q}), .n(ag_n), .contig(ag_c32),
        .addr(ag_addr2), .total(ag_total2), .stride0(ag_stride2), .left0(ag_left2)
    );

    wire f_sync_go = !fq_e && fh_sync && sync_ok(fh_ir[2:0], fh_ir[18:3]);
    wire d_sync_go = !dq_e && dh_sync && sync_ok(dh_ir[2:0], dh_ir[18:3]);
    // nd_* frame the word in wr_req, so a new drain waits for the last to
    // leave. A drain's start wins the shared table read; a fill's waits.
    wire d_take    = (d_st == E_IDLE) && !dq_e && !dh_sync && !wr_req_valid;
    wire f_take    = (f_st == E_IDLE) && !fq_e && !fh_sync && !d_take;
    assign fq_pop  = f_take || ((f_st == E_IDLE) && f_sync_go);
    assign dq_pop  = d_take || ((d_st == E_IDLE) && d_sync_go);
    wire a_done_dr = (d_st == E_RUN) && ((d_left == 9'd0) || (dr_take && (d_left == 9'd1)));
    wire a_done_fl = (f_st == E_RUN) && (f_left == 9'd0) && !c_v && !run_v;
    reg  a_fault;
    reg  [7:0] a_fcode;

    // Fill completion: each VFILL's word count, in order; responses land in
    // request order (vec_cu's run queue already relies on it).
    wire        ft_push = (f_st == E_MEM0) && (f_total <= 32'd256);
    wire [8:0]  ft_q;
    wire        ft_e;
    reg  [15:0] ft_landed;
    wire        ft_pop = !ft_e && (ft_landed >= {7'd0, ft_q});
    v2_fifo #(.W(9), .D(16)) u_ft (.clk(clk), .rst(erst), .push(ft_push), .din(f_total[8:0]),
        .pop(ft_pop), .dout(ft_q), .empty(ft_e), .full(), .count());
    always @(posedge clk) begin
        if (erst) begin
            ft_landed <= 16'd0;
        end else begin
            ft_landed <= ft_landed + (rr_valid ? 16'd1 : 16'd0) - (ft_pop ? {7'd0, ft_q} : 16'd0);
        end
    end

    always @(posedge clk) begin
        if (erst) begin
            f_st <= E_IDLE; d_st <= E_IDLE; ag_start <= 2'b00;
            run_v <= 1'b0; dr_v <= 0; wb_left <= 4'd0;
            rd_req_valid <= 1'b0; wr_req_valid <= 1'b0; wr_req_first <= 1'b0;
            nd_valid <= 1'b0; f_left <= 9'd0; d_left <= 9'd0; dr_left <= 9'd0; c_v <= 1'b0;
            a_fault <= 1'b0; fill_out <= 16'd0;
        end else begin
            ag_start <= {d_take, f_take};
            a_fault  <= 1'b0;
            dr_v <= dr_blk ? {(DR_TAP+1){1'b0}} : {dr_v[DR_TAP-1:0], dr_iss};
            if (rd_req_valid && rd_req_ready) rd_req_valid <= 1'b0;
            if (wr_req_valid && wr_req_ready) wr_req_valid <= 1'b0;
            fill_out <= fill_out + (fill_emit ? {8'd0, run_cnt} : 16'd0)
                                 - (rr_valid ? 16'd1 : 16'd0);
            // ---- fill engine
            case (f_st)
                E_IDLE: if (f_take) begin
                    f_sel <= fh_ir[23:21];
                    f_off <= fh_off;
                    f_l1  <= fh_ir[LAW-1:0];
                    f_st  <= E_AGW;
                end
                E_AGW: f_st <= E_W1;
                E_W1:  f_st <= E_W2;
                E_W2:  f_st <= E_W3;
                E_W3:  f_st <= E_MEM0;
                E_MEM0: if (f_total > 32'd256) begin
                    a_fault <= 1'b1; a_fcode <= F_LEN; f_st <= E_IDLE;
                end else begin
                    f_left <= f_total[8:0];
                    f_st   <= E_RUN;
                end
                E_RUN: begin
                    if (c_load) begin
                        c_v    <= 1'b1;
                        c_addr <= f_addr;
                        c_n    <= fl_n;
                        c_l1   <= f_l1;
                        f_l1   <= f_l1 + {{(LAW-8){1'b0}}, fl_n};
                        f_left <= f_left - {1'b0, fl_n};
                    end else if (c_take) begin
                        c_v    <= 1'b0;
                    end
                    if (c_take) begin
                        if (!run_v) begin
                            run_v <= 1'b1; run_addr <= c_addr; run_tag <= c_l1;
                            run_cnt <= c_n;
                        end else begin
                            run_cnt <= run_sum[7:0];
                        end
                        run_next <= c_addr + {{(AW-13){1'b0}}, c_n, 5'd0};
                    end
                    if (fill_emit) begin
                        rd_req_valid <= 1'b1;
                        rd_req_addr  <= run_addr;
                        rd_req_tag   <= run_tag;
                        rd_req_cnt   <= run_cnt;
                        run_v        <= 1'b0;
                    end
                    if (a_done_fl) f_st <= E_IDLE;
                end
                default: f_st <= E_IDLE;
            endcase
            // ---- drain engine
            case (d_st)
                E_IDLE: if (d_take) begin
                    nd_valid <= dh_ir[24];
                    nd_sig   <= dh_ir[25];
                    nd_x     <= dh_ir[20:17];
                    nd_y     <= dh_ir[16:13];
                    nd_buf   <= dh_ir[12:9];
                    d_sel    <= dh_ir[23:21];
                    dd_off   <= dh_off;
                    dd_l1    <= dh_ir[LAW-1:0];
                    d_st     <= E_AGW;
                end
                E_AGW: d_st <= E_W1;
                E_W1:  d_st <= E_W2;
                E_W2:  d_st <= E_W3;
                E_W3:  d_st <= E_MEM0;
                E_MEM0: if (d_total > 32'd256) begin
                    a_fault <= 1'b1; a_fcode <= F_LEN; d_st <= E_IDLE;
                end else begin
                    d_left   <= d_total[8:0];
                    nd_off   <= d_addr[15:0];
                    nd_ack   <= d_addr[23:16];
                    nd_mesh  <= d_addr[25:24];
                    nd_fin   <= d_addr[33:26];
                    nd_len   <= d_total[7:0] - 8'd1;
                    dr_ptr   <= dd_l1;
                    dr_left  <= d_total[8:0];
                    wb_left  <= 4'd0;
                    d_st     <= E_RUN;
                end
                E_RUN: begin
                    if (dr_iss) begin
                        dr_ptr  <= dr_ptr + 1'b1;
                        dr_left <= dr_left - 9'd1;
                    end
                    if (dr_blk) begin
                        dr_ptr  <= dd_l1;
                        dr_left <= d_left[8:0];
                    end
                    if (dr_take) begin
                        wr_req_valid <= 1'b1;
                        wr_req_addr  <= d_addr;
                        wr_req_data  <= l1_q[dr_bk2];
                        wr_req_first <= (wb_left == 4'd0);
                        wr_req_cnt   <= dr_blen;
                        wb_left      <= (wb_left == 4'd0) ? (dr_blen - 4'd1) : (wb_left - 4'd1);
                        dd_l1        <= dd_l1 + 1'b1;
                        d_left       <= d_left - 9'd1;
                    end
                    if (a_done_dr) d_st <= E_IDLE;
                end
                default: d_st <= E_IDLE;
            endcase
        end
    end

    // ================================================== done counters
    wire m_retire = wb_last;
    wire u_retire = uf_pop && uf_last;
    wire p_retire = pf_pop && pf_last;
    wire d_retire = a_done_dr;
    wire f_retire = ft_pop;

    assign all_idle = mq_e && uq_e && pq_e && fq_e && dq_e && !m_act && !u_act && !p_act
                   && (f_st == E_IDLE) && (d_st == E_IDLE) && pipe_empty && uf_e && pf_e
                   && (u_occ == 4'd0) && (p_occ == 4'd0) && !ug_busy && !pg_busy && !mx_busy
                   && (fill_out == 16'd0) && !wr_req_valid && ft_e && !iss_valid;

    // ================================================== front end: sequencing
    wire disp_m = fe_ok && is_math;
    wire disp_u = fe_ok && (d_op == O_VUNPK);
    wire disp_p = fe_ok && (d_op == O_VPACK);
    // Per kind K_*: dispatched and finished this cycle.
    wire [6:0] k_disp = {disp_p && d_q, disp_u && d_q, fe_ok && (d_op == O_VDRAIN),
                         disp_p && !d_q, disp_m, disp_u && !d_q, fe_ok && (d_op == O_VFILL)};
    wire [6:0] k_done = {p_retire && pf_qb, u_retire && uf_qb, d_retire, p_retire && !pf_qb,
                         m_retire, u_retire && !uf_qb, f_retire};
    wire [7:0] k_done8 = {1'b0, k_done};
    // A new mark is owed what is outstanding of its kind after this cycle,
    // less the slack (n_disp does not move: this cycle dispatches the VSYNC).
    wire [8:0] mk_owed = {1'b0, n_out[d_on]} - {8'd0, k_done8[d_on]} - {3'd0, d_slack};

    // Scoreboard bookkeeping: one update per register per cycle.
    wire m_rel_a = m_end && (e_sa == SRC_V);
    wire m_rel_b = m_end && (e_sb == SRC_V);
    wire m_rel_c = m_end && (e_sc == SRC_V);
    wire p_rel   = p_endw;
    reg  [4:0] p_rel_vs;
    always @(*) p_rel_vs = pe_vs;

    always @(posedge clk) begin
        for (ii = 0; ii < 32; ii = ii + 1) begin
            if (erst) begin
                Wd[ii] <= 6'd0; Wc[ii] <= 6'd0; UWd[ii] <= 6'd0; UWc[ii] <= 6'd0;
                MRd[ii] <= 6'd0; MRc[ii] <= 6'd0; PRd[ii] <= 6'd0; PRc[ii] <= 6'd0;
            end else begin
                if (((disp_m && !is_cmp) || disp_u) && (d_vd == ii[4:0])) Wd[ii] <= Wd[ii] + 6'd1;
                // A math write-back and an unpack retire never finish the same
                // register in one cycle: each waits for the other's older writes.
                if ((wb_last && wb_wr && (wb_vreg == ii[4:0]))
                    || (u_retire && (uw_wa[7:3] == ii[4:0]))) Wc[ii] <= Wc[ii] + 6'd1;
                if (disp_u && (d_vd == ii[4:0])) UWd[ii] <= UWd[ii] + 6'd1;
                if (u_retire && (uw_wa[7:3] == ii[4:0])) UWc[ii] <= UWc[ii] + 6'd1;
                if (disp_m && ((uses_a && d_va == ii[4:0]) || (uses_b && d_vb == ii[4:0])
                               || (uses_c && d_vc == ii[4:0]))) MRd[ii] <= MRd[ii] + 6'd1;
                if ((m_rel_a && e_va == ii[4:0]) || (m_rel_b && e_vb == ii[4:0])
                    || (m_rel_c && e_vc == ii[4:0])) MRc[ii] <= MRc[ii] + 6'd1;
                if (disp_p && (d_vd == ii[4:0])) PRd[ii] <= PRd[ii] + 6'd1;
                if (p_rel && (p_rel_vs == ii[4:0])) PRc[ii] <= PRc[ii] + 6'd1;
            end
        end
        for (ii = 0; ii < 4; ii = ii + 1) begin
            if (erst) begin
                pP[ii] <= 5'd0;
            end else begin
                pP[ii] <= pP[ii]
                        + ((m_take && h_cmp && (h_pr == ii[1:0])) ? 5'd1 : 5'd0)
                        - ((wb_last && wb_cmp && (wb_pr == ii[1:0])) ? 5'd1 : 5'd0);
            end
        end
        for (ii = 0; ii < 8; ii = ii + 1) begin
            if (erst) begin
                mk_on[ii] <= K_F; mk_tgt[ii] <= 16'd0; mk_cnt[ii] <= 8'd0;
            end else if (fe_ok && (d_op == O_VSYNC) && (d_wt == W_MK) && (d_mid == ii[2:0])) begin
                mk_on[ii] <= d_on; mk_tgt[ii] <= d_tgt;
                mk_cnt[ii] <= mk_owed[8] ? 8'd0 : mk_owed[7:0];
            end else if ((mk_cnt[ii] != 8'd0) && k_done8[mk_on[ii]]) begin
                mk_cnt[ii] <= mk_cnt[ii] - 8'd1;
            end
        end
        for (ii = 0; ii < 7; ii = ii + 1) begin
            // Enables, not added bits: dispatch decides late in the cycle.
            if (erst) begin
                n_disp[ii] <= 16'd0; n_done[ii] <= 16'd0; n_out[ii] <= 8'd0;
            end else begin
                if (k_disp[ii]) n_disp[ii] <= n_disp[ii] + 16'd1;
                if (k_done[ii]) n_done[ii] <= n_done[ii] + 16'd1;
                if (k_disp[ii] != k_done[ii])
                    n_out[ii] <= k_disp[ii] ? n_out[ii] + 8'd1 : n_out[ii] - 8'd1;
            end
        end
        n_out[7] <= 8'd0;
    end

    // Queue entries, formed from `ir` and the front end's state.
    wire [15:0] chk_now = d_om ? c_chk : 16'h1111;
    wire [14:0] xb_now  = d_om ? c_xb  : 15'd0;
    wire [LAW-1:0] ua_now = d_rel ? (c_uptr + d_off[LAW-1:0]) : d_off[LAW-1:0];
    wire [LAW-1:0] pa_now = d_rel ? (c_pptr + d_off[LAW-1:0]) : d_off[LAW-1:0];
    // A sync entry: {1, 0..., target[15:0], on[2:0]}; `slack` exempts the
    // youngest instructions of the kind (a double buffer's other half).
    wire [UQW-1:0] sync_q = {1'b1, {(UQW-20){1'b0}}, s_tgt, s_on};

    // The entries depend on the instruction only; fe_ok gates the pushes.
    wire is_sync = (d_op == O_VSYNC);
    always @(*) begin
        mq_push = fe_ok && (is_math || (is_sync && (d_wt == W_M)));
        uq_push = fe_ok && ((d_op == O_VUNPK) || (d_op == O_VBAR) || (is_sync && (d_wt == W_U)));
        pq_push = fe_ok && ((d_op == O_VPACK) || (is_sync && (d_wt == W_P)));
        fq_push = fe_ok && ((d_op == O_VFILL) || (is_sync && (d_wt == W_A)));
        dq_push = fe_ok && ((d_op == O_VDRAIN) || (is_sync && (d_wt == W_D)));
        // aop 31 marks a sync entry; target and kind ride in the kc slot.
        mq_din  = is_sync ? {5'd31, 26'd0, 48'd0, s_tgt, 5'd0, s_on, 42'd0, 30'd0}
                : {alu_op, d_vd, d_va, d_vb, d_vc, e_sa_d, e_sb_d, e_sc_d,
                   pick(e_sa_d, d_va), pick(e_sb_d, d_vb), pick(e_sc_d, d_vc),
                   nb_vl, vl[3:0], chk_now, xb_now, is_cmp,
                   (d_op == O_EXP2D), !is_cmp,
                   Wd[d_va], Wd[d_vb], Wd[d_vc], UWd[d_vd], PRd[d_vd]};
        uq_din  = is_sync ? sync_q
                : (d_op == O_VBAR) ? {1'b1, {(UQW-20){1'b0}}, n_disp[K_F], K_F}
                : {1'b0, d_vd, d_mode, d_n, d_cb, ua_now, c_ustr,
                   Wd[d_vd], UWd[d_vd], MRd[d_vd], PRd[d_vd]};
        pq_din  = is_sync ? sync_q
                : {1'b0, d_vd, d_mode, d_n, d_cb, pa_now, c_pstr, Wd[d_vd], PRd[d_vd], 12'd0};
        aq_din  = is_sync ? {1'b1, {AW{1'b0}}, 13'd0, s_tgt, s_on}
                : {1'b0, ir[26] ? a_off[d_ad] : {AW{1'b0}}, ir};
    end

    // ================================================== front end: registers
    always @(posedge clk) begin
        stk_clr <= 1'b0;
        srst    <= 1'b0;
        d_old   <= !rst && run && (fe_fault == 8'd0) && !a_fault && !adv;
        if (rst) begin
            run <= 1'b0; f_v <= 1'b0; d_v <= 1'b0; busy <= 1'b0; halted <= 1'b0;
            fault <= 1'b0; fault_code <= 8'd0; cycles <= 32'd0; lp_act <= 1'b0;
            vl <= VLMAX[7:0]; cd_err <= 1'b0; f_pc <= {PCW{1'b0}};
            c_chk <= 16'h1111; c_xb <= 15'd0;
            c_uptr <= 0; c_uinc <= 0; c_pptr <= 0; c_pinc <= 0;
            c_ustr <= STR_FLAT; c_pstr <= STR_FLAT;
            for (ii = 0; ii < 8; ii = ii + 1) begin
                a_off[ii] <= {AW{1'b0}}; a_inc[ii] <= 18'sd0;
            end
            kreg[0] <= 24'h000000; kreg[1] <= E8_ONE;
            kreg[2] <= 24'hBF8000; kreg[3] <= 24'h000000;
        end else begin
            if (busy) cycles <= cycles + 32'd1;
            if (!run) begin
                f_pc <= start_pc;
                d_v  <= 1'b0;
                f_v  <= 1'b0;
                if (start) begin
                    run <= 1'b1; f_v <= 1'b1; busy <= 1'b1; halted <= 1'b0; fault <= 1'b0;
                    cycles <= 32'd0; lp_act <= 1'b0; srst <= 1'b1;
                    for (ii = 0; ii < 8; ii = ii + 1) a_off[ii] <= {AW{1'b0}};
                    if (cd_err) begin
                        cd_err <= 1'b0; fault_code <= F_CUDATA;
                        run <= 1'b0; busy <= 1'b0; fault <= 1'b1;
                    end
                end
            end else if ((fe_fault != 8'd0) || a_fault) begin
                fault_code <= a_fault ? a_fcode : fe_fault;
                fault <= 1'b1; busy <= 1'b0; run <= 1'b0; cd_err <= 1'b0;
            end else begin
                f_pc <= im_addr;
                if (redirect) begin
                    d_v <= 1'b0;
                end else if (adv) begin
                    ir   <= im_q;
                    d_pc <= f_pc;
                    // VSETI consumes the word after it as its immediate.
                    d_v  <= f_v && !(fe_ok && d_v && (d_op == O_VSETI));
                    if (lp_back) lp_cnt <= lp_cnt - 24'd1;
                    if (lp_exit) lp_act <= 1'b0;
                end
                if (fe_ok) begin
                    case (d_op)
                        O_VCFG: case (d_csel)
                            4'd0: c_chk  <= d_cpay[15:0];
                            4'd1: c_xb   <= d_cpay[14:0];
                            4'd2: c_uptr <= d_cpay[LAW-1:0];
                            4'd3: c_uinc <= d_cpay[LAW-1:0];
                            4'd4: c_ustr <= d_cpay[2*LAW-1:0];
                            4'd5: c_pptr <= d_cpay[LAW-1:0];
                            4'd6: c_pinc <= d_cpay[LAW-1:0];
                            4'd7: c_pstr <= d_cpay[2*LAW-1:0];
                            4'd9: a_inc[d_cpay[22:20]] <= d_cpay[17:0];
                            4'd10: begin
                                vl    <= {1'b0, d_cpay[22:16]} + 8'd1;
                                c_chk <= d_cpay[15:0];
                            end
                            4'd8: begin
                                sreg[d_cpay[3:0]] <= (|sticky) ? E8_ONE : 24'd0;
                                stk_clr <= 1'b1;
                            end
                            default: ;
                        endcase
                        O_VUNPK: if (d_rel) c_uptr <= c_uptr + c_uinc;
                        O_VPACK: if (d_rel) c_pptr <= c_pptr + c_pinc;
                        O_VFILL, O_VDRAIN: if (ir[26]) begin
                            a_off[d_ad] <= a_off[d_ad] + {{(AW-18){a_inc[d_ad][17]}}, a_inc[d_ad]};
                        end
                        O_VSETVL: vl <= sreg[d_va[3:0]][7:0];
                        O_VSETI: if (ir[0]) kreg[3] <= im_q[23:0];
                                 else sreg[d_vd[3:0]] <= im_q[23:0];
                        O_VLOOP: begin
                            lp_act <= 1'b1;
                            lp_cnt <= sreg[d_va[3:0]];
                            lp_top <= d_pc + 1'b1;
                            lp_end <= d_pc + 1'b1 + d_body;
                        end
                        O_VHALT: begin
                            run <= 1'b0; busy <= 1'b0; halted <= 1'b1;
                        end
                        default: ;
                    endcase
                end
            end
            if (cd_fault) cd_err <= 1'b1;
        end
    end

`ifndef SYNTHESIS
    // ================================================== measurement
    // Every engine's busy and stall cycles by cause, dumped at `final`.
    reg [31:0] pc_busy = 0, pc_beats = 0, pc_lanes = 0, pc_insn = 0;
    reg [31:0] pc_m_act = 0, pc_m_haz = 0, pc_m_empty = 0, pc_m_sync = 0;
    reg [31:0] pc_u_act = 0, pc_u_room = 0, pc_u_sync = 0, pc_u_wbblk = 0;
    reg [31:0] pc_p_act = 0, pc_p_room = 0, pc_p_haz = 0, pc_p_sync = 0, pc_p_l1blk = 0;
    reg [31:0] pc_a_act = 0, pc_a_drblk = 0, pc_a_drarb = 0;
    reg [31:0] pc_a_dact = 0, pc_a_fsync = 0, pc_a_dsync = 0;
    reg [31:0] pc_d_mq = 0, pc_d_uq = 0, pc_d_pq = 0, pc_d_aq = 0, pc_d_sbm = 0, pc_d_sbu = 0;
    reg [31:0] pc_d_fe = 0, pc_d_seti = 0, pc_d_idle = 0, pc_redir = 0;
    reg [31:0] pc_l1r = 0, pc_l1w = 0, pc_fill = 0, pc_drw = 0;
    integer    pi;
    reg [4:0]  lanes_now;
    always @(*) begin
        lanes_now = 5'd0;
        for (pi = 0; pi < 16; pi = pi + 1) lanes_now = lanes_now + {4'd0, iss_tm[pi]};
    end
    always @(posedge clk) begin
        if (!rst && busy) begin
            pc_busy <= pc_busy + 1;
            if (iss_valid) begin
                pc_beats <= pc_beats + 1;
                pc_lanes <= pc_lanes + lanes_now;
            end
            if (fe_ok) pc_insn <= pc_insn + 1;
            if (m_act) pc_m_act <= pc_m_act + 1;
            else if (mq_e) pc_m_empty <= pc_m_empty + 1;
            else if (h_sync) pc_m_sync <= pc_m_sync + 1;
            else pc_m_haz <= pc_m_haz + 1;
            if (u_act) begin
                pc_u_act <= pc_u_act + 1;
                if (!u_room) pc_u_room <= pc_u_room + 1;
            end else if (!uq_e && uh_sync) pc_u_sync <= pc_u_sync + 1;
            if (!uf_e && !uf_pop) pc_u_wbblk <= pc_u_wbblk + 1;
            if (p_act) begin
                pc_p_act <= pc_p_act + 1;
                if (!p_room) pc_p_room <= pc_p_room + 1;
            end else if (!pq_e && ph_sync) pc_p_sync <= pc_p_sync + 1;
            else if (!pq_e && p_haz) pc_p_haz <= pc_p_haz + 1;
            if (!pf_e && !pf_pop) pc_p_l1blk <= pc_p_l1blk + 1;
            if (f_st != E_IDLE) pc_a_act <= pc_a_act + 1;
            if (d_st != E_IDLE) pc_a_dact <= pc_a_dact + 1;
            if (!fq_e && fh_sync && f_st == E_IDLE) pc_a_fsync <= pc_a_fsync + 1;
            if (!dq_e && dh_sync && d_st == E_IDLE) pc_a_dsync <= pc_a_dsync + 1;
            if (dr_blk) pc_a_drblk <= pc_a_drblk + 1;
            if (dr_want && !dr_gnt) pc_a_drarb <= pc_a_drarb + 1;
            if (d_v && !fe_ok) begin
                if (is_math && mq_f) pc_d_mq <= pc_d_mq + 1;
                else if (is_math) pc_d_sbm <= pc_d_sbm + 1;
                else if (d_op == O_VUNPK && uq_f) pc_d_uq <= pc_d_uq + 1;
                else if (d_op == O_VUNPK) pc_d_sbu <= pc_d_sbu + 1;
                else if (d_op == O_VPACK) pc_d_pq <= pc_d_pq + 1;
                else if (d_op == O_VFILL || d_op == O_VDRAIN) pc_d_aq <= pc_d_aq + 1;
                else if (d_op == O_VSETI) pc_d_seti <= pc_d_seti + 1;
                else pc_d_fe <= pc_d_fe + 1;
            end
            if (!d_v) pc_d_idle <= pc_d_idle + 1;
            if (redirect) pc_redir <= pc_redir + 1;
            pc_l1r <= pc_l1r + (u_rd ? 1 : 0) + (dr_iss ? 1 : 0);
            pc_l1w <= pc_l1w + (l1_we[0] ? 1 : 0) + (l1_we[1] ? 1 : 0);
            if (rr_valid) pc_fill <= pc_fill + 1;
            if (dr_take) pc_drw <= pc_drw + 1;
        end
    end
    // The modulo-64 counts need fewer than 32 outstanding of a kind.
    always @(posedge clk) begin
        for (pi = 0; pi < 32; pi = pi + 1) begin
            if (!rst && (((Wd[pi] - Wc[pi]) & 6'h20) || ((MRd[pi] - MRc[pi]) & 6'h20)
                         || ((PRd[pi] - PRc[pi]) & 6'h20)))
                $display("V2_SB_OVERFLOW v%0d t=%0t", pi, $time);
        end
    end
`ifdef V2_TRACE
    // One line a busy cycle: math head and its wait, queue depths, dispatch.
    integer tf;
    initial tf = $fopen("v2trace.txt", "w");
    always @(posedge clk) begin
        if (!rst && busy) begin
            $fwrite(tf, "%0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d %0d\n",
                    pc_busy, m_act, m_take, e_aop, mq_e ? 99 : h_aop, h_haz, u_act, p_act,
                    d_v, d_op, fe_ok, d_pc, uq_e,
                    {d_st, 3'd0} | f_st, fq_e, (!fq_e && fh_sync) | ((!dq_e && dh_sync) << 1),
                    !uq_e && uh_sync, pq_e, !pq_e && ph_sync,
                    rr_valid, dr_take);
        end
    end
`endif
    final begin
        $display("VRES %m busy %0d beats %0d lanes %0d l1_rd %0d l1_wr %0d fill_words %0d drain_words %0d insns %0d",
                 pc_busy, pc_beats, pc_lanes, pc_l1r, pc_l1w, pc_fill, pc_drw, pc_insn);
        $display("V2MATH %m act %0d hazard %0d empty %0d sync %0d",
                 pc_m_act, pc_m_haz, pc_m_empty, pc_m_sync);
        $display("V2UNPACK %m act %0d no_room %0d sync %0d wb_blocked %0d",
                 pc_u_act, pc_u_room, pc_u_sync, pc_u_wbblk);
        $display("V2PACK %m act %0d no_room %0d raw %0d sync %0d l1_blocked %0d",
                 pc_p_act, pc_p_room, pc_p_haz, pc_p_sync, pc_p_l1blk);
        $display("V2DMA %m fill_act %0d drain_act %0d fill_sync %0d drain_sync %0d drain_blocked %0d drain_lost_arb %0d",
                 pc_a_act, pc_a_dact, pc_a_fsync, pc_a_dsync, pc_a_drblk, pc_a_drarb);
        $display("V2DISPATCH %m mq_full %0d sb_math %0d uq_full %0d sb_unpack %0d pq_full %0d aq_full %0d seti %0d fe_wait %0d empty %0d redirects %0d",
                 pc_d_mq, pc_d_sbm, pc_d_uq, pc_d_sbu, pc_d_pq, pc_d_aq, pc_d_seti, pc_d_fe,
                 pc_d_idle, pc_redir);
        // Left over at the end: anything nonzero here is a stuck engine.
        for (pi = 0; pi < 32; pi = pi + 1) begin
            if (Wd[pi] != Wc[pi] || UWd[pi] != UWc[pi] || MRd[pi] != MRc[pi] || PRd[pi] != PRc[pi])
                $display("V2SB_LEFT v%0d W %0d/%0d UW %0d/%0d MR %0d/%0d PR %0d/%0d", pi,
                         Wc[pi], Wd[pi], UWc[pi], UWd[pi], MRc[pi], MRd[pi], PRc[pi], PRd[pi]);
        end
        if (!mq_e) $display("V2HEAD_M aop %0d vd %0d va %0d vb %0d vc %0d s %0d%0d%0d haz %0d",
                            h_aop, h_vd, h_va, h_vb, h_vc, h_sa, h_sb, h_sc, h_haz);
        if (!uq_e) $display("V2HEAD_U sync %0d vd %0d", uh_sync, uh_vd);
        if (!pq_e) $display("V2HEAD_P sync %0d vs %0d haz %0d", ph_sync, ph_vs, p_haz);
        $display("V2FE d_v %0d op %0d pc %0d", d_v, d_op, d_pc);
    end
`endif

endmodule

`default_nettype wire

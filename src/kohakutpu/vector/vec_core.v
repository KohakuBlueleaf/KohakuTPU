// The vector core: sequencer, L1 scratchpad, address generator and lanes.
//
// Executes docs/isa/vector.md against vec_lanes. The memory port is ABSTRACT
// -- a fill request is a contiguous run of 256-bit words tagged with its first
// L1 word, a drain a stream of words grouped into write bursts -- so the core
// carries no NoC knowledge; vec_cu.v does the framing.
//
// Three rules, and the reason for each:
//
//   ALU ops issue back to back, gated by a per-register pending-write count.
//   The lane is 14 deep with no bypass, so without it a program could read a
//   register whose write has not landed and be plausibly wrong.
//
//   VLD, VST, VFILL and VDRAIN walk in a load/store engine, one at a time, while
//   ALU ops keep issuing; every other instruction waits for the engine. A VLD
//   writes only into write-back slots no ALU result owns, a VST reads through
//   whichever of ports a/c the issuing beat leaves free, and an ALU op waits
//   while the walk's register is its operand or destination.
//
//   VSHUF/VBCAST/VCVT wait until no write-back is left in the current mode: they
//   drive the register file's whole-chunk port, which shares write ports with
//   ALU write-back.
//
// VLD/VST/VDRAIN move a word a cycle; a blocked VST/VDRAIN landing REPLAYS from
// that word (no 256-bit skid). The next instruction is fetched while this one runs.

`default_nettype none

`ifndef VEC_VLMAX
  `define VEC_VLMAX 128
`endif

module vec_core #(
    parameter integer MODEL      = 1,
    // Elements per vector register: 16 lanes x VLMAX/16 chunks, a power of two.
    parameter integer VLMAX      = `VEC_VLMAX,
    parameter integer IMEM_DEPTH = 512,
    parameter integer L1_DEPTH   = 512,
    parameter         L1_PRIM    = "block",
    parameter         RF_PRIM    = "block",
    parameter integer RF_PAD     = 24,     // vec_regfile PAD_W (b/c copies)
    parameter integer RF_PACK    = 1,      // vec_regfile LANES per b/c word
    // 34 UNTIL vec_cu's `c_val`/`ld_data` widen IN THE SAME STEP: a narrow
    // driver leaves [39:34] x, and x in `cbase` makes the whole `addr` sum x.
    parameter integer AW         = 40
)(
    input  wire         clk,
    input  wire         rst,

    input  wire         ld_en,
    input  wire         ld_kind,          // 0 = imem, 1 = descriptor
    input  wire [8:0]   ld_addr,          // desc: {ad[2:0], fld[2:0]}
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
    output reg  [8:0]   rd_req_tag,       // the run's first L1 word
    output reg  [7:0]   rd_req_cnt,       // words in the run, 1..255, contiguous
    input  wire         rd_req_ready,
    input  wire         rr_valid,
    input  wire [8:0]   rr_tag,
    input  wire [255:0] rr_data,

    // A peer's CU_DATA landing in L1. SEPARATE FROM THE FILL PORT because every
    // `rr_valid` decrements `fill_out` and a peer's write was never counted
    // into it: reusing it underflows the count and hangs the NEXT barrier,
    // an unrelated instruction far from the flit that caused it. Mutually
    // exclusive with `rr_valid` -- one pop of vec_cu's receive queue feeds both.
    input  wire         cd_valid,
    input  wire [8:0]   cd_addr,          // L1 word, already range-checked
    input  wire [255:0] cd_data,
    input  wire         cd_fault,         // that burst named a place we lack

    // A drain word is TAKEN in any cycle `wr_req_ready` and valid are both high.
    // `first` opens a write burst of `cnt` words, contiguous and inside 256 bytes.
    output reg          wr_req_valid,
    output reg  [AW-1:0] wr_req_addr,
    output reg  [255:0] wr_req_data,
    output reg          wr_req_first,
    output reg  [3:0]   wr_req_cnt,
    input  wire         wr_req_ready,

    // A VDRAIN whose sink is a peer rather than memory. Stable from S_MEM0
    // until the next VFILL/VDRAIN, so vec_cu frames the whole burst from these
    // and this module still carries no NoC knowledge.
    output reg          nd_valid,
    output reg  [3:0]   nd_x,
    output reg  [3:0]   nd_y,
    output reg  [3:0]   nd_buf,
    output reg  [15:0]  nd_off,
    output reg  [7:0]   nd_len,
    output reg          nd_sig,
    output reg  [7:0]   nd_ack,
    // A peer in ANOTHER mesh. Both come from the descriptor base's top ten
    // bits, which a drain has never used; `nd_fin` nonzero is what makes it
    // remote, and (0,0) is a mesh corner that can hold no endpoint, so zero is
    // an unambiguous "local" exactly as it is for `nd_ack`. Every encoding
    // written before the interlink leaves both zero.
    output reg  [1:0]   nd_mesh,
    output reg  [7:0]   nd_fin
);
    localparam [4:0] O_VCVT = 5'h12, O_VRED = 5'h13, O_VLD = 5'h14;
    localparam [4:0] O_VST = 5'h15, O_VBCAST = 5'h16, O_VSHUF = 5'h17;
    localparam [4:0] O_VSETVL = 5'h18, O_VSETMD = 5'h19, O_VSETI = 5'h1A;
    localparam [4:0] O_VLOOP = 5'h1B, O_VBAR = 5'h1C, O_VFILL = 5'h1D;
    localparam [4:0] O_VDRAIN = 5'h1E, O_VHALT = 5'h1F;

    localparam [1:0] SRC_V = 2'd0, SRC_S = 2'd1, SRC_C = 2'd2, SRC_K = 2'd3;
    localparam [1:0] M_FLAT = 2'd0, M_D2 = 2'd1, M_TREE = 2'd3;
    localparam [2:0] DT_FP16 = 3'd1, DT_FP32 = 3'd2;
    localparam [2:0] R_EXPSUM = 3'd5;

    // One element per leaf, so two phases cover a chunk. SUMSQ, DOT, EXPSUM.
    function red_half;
        input [2:0] k;
        red_half = (k == 3'd3) || (k == 3'd4) || (k == R_EXPSUM);
    endfunction
    localparam [23:0] E8_ONE = 24'h3F8000;

    localparam [7:0] F_DTYPE = 8'd1, F_VSRC = 8'd2, F_CHAIN = 8'd3;
    localparam [7:0] F_OPCODE = 8'd4, F_LEN = 8'd5, F_LOOP = 8'd6, F_VL = 8'd7;
    localparam [7:0] F_REDVL = 8'd8, F_CUDATA = 8'd9;

    // URAM CANNOT DO READ_LAT=1 -- xpm_memory rejects it, so the latency is a
    // property of the primitive and the walks' landing taps (LD_TAP/DR_TAP) sit
    // a cycle later.
    localparam integer L1_LAT = (L1_PRIM == "ultra") ? 2 : 1;
    localparam integer LAW    = (L1_DEPTH <= 1) ? 1 : $clog2(L1_DEPTH);
    // Chunk index, vl, register-file word address and beat-count widths.
    localparam integer CW = $clog2(VLMAX / 16);
    localparam integer VW = $clog2(VLMAX) + 1;
    localparam integer RW = 4 + CW;
    localparam integer BW = CW + 3;

    localparam [4:0] S_IDLE = 5'd0, S_F1 = 5'd1, S_F2 = 5'd2, S_DEC = 5'd3;
    localparam [4:0] S_EXEC = 5'd4, S_GA = 5'd5, S_GB = 5'd6, S_GC = 5'd7;
    localparam [4:0] S_GD = 5'd8, S_ALU = 5'd9, S_RED = 5'd10, S_RDRAIN = 5'd11;
    // VLD, VST, VFILL and VDRAIN walks run in the load/store engine (`ls_run`).
    localparam [4:0] S_RWAIT = 5'd13;
    localparam [4:0] S_STR = 5'd17, S_STW = 5'd18;
    localparam [4:0] S_STD = 5'd19;
    localparam [4:0] S_BAR = 5'd24;
    localparam [4:0] S_WAITP = 5'd25, S_SETI = 5'd26, S_SETI2 = 5'd27;
    localparam [4:0] S_HALT = 5'd28, S_FAULT = 5'd29, S_MEM0 = 5'd30;
    localparam [4:0] S_AGW = 5'd31;
    // `ag_total` is pipelined three deep in vec_agu, so a walk waits for it.
    localparam [5:0] S_MEMW1 = 6'd32, S_MEMW2 = 6'd33, S_MEMW3 = 6'd35;
    localparam [5:0] S_PRED = 6'd38;     // VRED ANY/ALL: evaluate P[g_pr]

    // Issue-to-landing distance of each walk, in cycles after the issuing one.
    // VLD's word passes the converter-input register, so it lands one later.
    localparam integer LD_TAP = L1_LAT + 1;
    localparam integer DR_TAP = L1_LAT;

    // ================================================== architectural state
    reg [5:0]  st;
    reg [8:0]  pc;
    reg [31:0] ir;
    reg [1:0]  vmode;
    reg [VW-1:0] vl;
    reg [23:0] sreg [0:15];
    reg [23:0] kreg [0:3];

    reg [8:0]  lp_top, lp_end;
    reg [23:0] lp_cnt;
    reg        lp_act;

    reg [19:0] g_op;
    reg [7:0]  g_sa, g_sb, g_sc;
    reg [95:0] g_ka, g_kb, g_kc;
    reg [2:0]  g_have;
    reg [3:0]  g_ra, g_rb, g_rc, g_wd;
    reg        g_cmp;

    reg [BW-1:0] bcnt, nbeat;
    reg [CW-1:0] cchunk;
    reg [1:0]  cphase;
    reg [3:0]  ls_reg;
    reg [2:0]  ls_dt;
    reg [191:0] ls_hold;
    reg [LAW-1:0] l1_cur;
    reg [15:0] fill_out;
    reg [31:0] mem_left;
    reg [3:0]  shuf_k;
    reg [4:0]  ls_kind;
    reg        bc_to_s;
    reg        cd_err;

    reg [8:0]  im_addr;
    reg [8:0]  pf_addr;               // the imem word `im_q` holds this cycle
    integer    ii;

    // The pipelined walks: words left to issue, words landed, and in-flight valids.
    reg [CW+1:0]    ld_left;
    reg [LD_TAP:0]  ld_v, ld_h;       // VLD in flight, and which FP32 half
    reg [CW+1:0]    sq_nw, sq_iw, sq_lw;
    reg [1:0]       sq_v, sq_h;       // VST in flight, and which FP32 half
    reg [LAW-1:0]   dr_ptr;
    reg [8:0]       dr_left;
    reg [DR_TAP:0]  dr_v;
    reg [3:0]       wb_left;          // words the open write burst still owes

    // ================================================== memories
    // "block", not "distributed": 512 x 32 measured 392 LUT as LUTRAM against
    // 0 LUT and one RAMB18, at the SAME READ_LAT, so no state moves. `im_q` is
    // only ever captured into `ir`/`sreg`/`kreg`, two states after the address
    // is presented, so a block RAM's clock-to-out lands on a plain D pin.
    wire [31:0] im_q;
    kohaku_sdpram #(.WIDTH(32), .DEPTH(IMEM_DEPTH), .MEM_PRIM("block"),
                    .READ_LAT(1)) u_imem (
        .clk(clk), .wr_en(ld_en && !ld_kind), .wr_addr(ld_addr),
        .wr_data(ld_data[31:0]),
        .rd_en(1'b1), .rd_addr(im_addr), .rd_data(im_q)
    );

    reg  [LAW-1:0] l1_waddr, l1_raddr;
    reg  [255:0] l1_wdata;
    reg          l1_we;
    wire [255:0] l1_q;
    kohaku_sdpram #(.WIDTH(256), .DEPTH(L1_DEPTH), .MEM_PRIM(L1_PRIM),
                    .READ_LAT(L1_LAT)) u_l1 (
        .clk(clk), .wr_en(l1_we), .wr_addr(l1_waddr), .wr_data(l1_wdata),
        .rd_en(1'b1), .rd_addr(l1_raddr), .rd_data(l1_q)
    );

    // ================================================== address generator
    reg         ag_start;
    reg  [2:0]  ag_sel;
    reg signed [17:0] ag_off;
    wire [AW-1:0] ag_addr;
    wire [31:0] ag_total;
    wire signed [17:0] ag_stride0;
    wire [15:0] ag_left0;

    // THE LOAD/STORE ENGINE runs VLD and VST beside the sequencer, so ALU ops
    // issue while a walk is in flight. `e_*` hold the walk's own operands.
    localparam [2:0] LS_IDLE = 3'd0, LS_LD = 3'd1, LS_ST = 3'd2, LS_DR = 3'd3;
    localparam [2:0] LS_FL = 3'd4;
    reg  [2:0]    ls_run;
    reg           ls_tail;          // the cycle after a walk ends: its last write lands
    reg  [3:0]    e_reg;
    reg           e_noreg;          // a VDRAIN walk touches no register
    reg  [2:0]    e_dt;
    reg  [CW-1:0] e_chunk;
    wire          ls_busy = (ls_run != LS_IDLE) || ls_tail;
    wire          wb_ahead;

    // VLD: an L1 read a cycle, landing LD_TAP cycles later through `cv_src`. A read
    // is not issued when its write-back slot is an ALU result's (`wb_ahead`).
    wire ld_iss  = (ls_run == LS_LD) && (ld_left != {(CW+2){1'b0}}) && !wb_ahead;
    wire ld_land = ld_v[LD_TAP];
    // vec_lanes writes `ls_wdata` the cycle AFTER `ls_we`, so the enable goes up as
    // the chunk's last word is one cycle from landing; FP32's lo half writes nothing.
    wire ld_wpre = ld_v[LD_TAP-1] && ((e_dt != DT_FP32) || ld_h[LD_TAP-1]);
    // VST: a register-file read a cycle; a landing that meets a fill on L1's one
    // write port replays from itself.
    wire sq_land = sq_v[1];
    wire sq_blk  = sq_land && (rr_valid || cd_valid);
    wire sq_wr   = sq_land && !sq_blk;
    // VDRAIN: a word lands in the request register when it is empty or being taken.
    wire dr_land = dr_v[DR_TAP];
    wire dr_room = !wr_req_valid || wr_req_ready;
    wire dr_take = dr_land && dr_room;
    wire dr_blk  = dr_land && !dr_room;
    // A burst is contiguous (dimension 0 at one word), inside 256 bytes, at most 8.
    wire [3:0] dr_al   = 4'd8 - {1'b0, ag_addr[7:5]};
    wire [3:0] dr_run  = (ag_stride0 != 18'sd32) ? 4'd1
                       : (ag_left0 < 16'd8) ? ag_left0[3:0] : 4'd8;
    wire [3:0] dr_mem  = (mem_left < 32'd8) ? mem_left[3:0] : 4'd8;
    wire [3:0] dr_b1   = (dr_al < dr_run) ? dr_al : dr_run;
    wire [3:0] dr_blen = (dr_b1 < dr_mem) ? dr_b1 : dr_mem;
    // vec_cu spends the cycle after a burst's last word on the next descriptor,
    // so the read that would land then is not issued.
    wire dr_bend = dr_take && !nd_valid
                && ((wb_left == 4'd0) ? (dr_blen == 4'd1) : (wb_left == 4'd1));
    wire dr_iss  = (ls_run == LS_DR) && (dr_left != 9'd0) && !dr_blk && !dr_bend;

    // THE FILL WALK COALESCES a word a cycle into one streamed request while the
    // address follows on and the L1 tag does not wrap; it steps the AGU directly.
    reg          run_v;
    reg [AW-1:0] run_addr, run_next;
    reg [8:0]    run_tag;
    reg [7:0]    run_cnt;
    wire run_ext  = run_v && (ag_addr == run_next) && (run_cnt != 8'd255)
                 && (l1_cur[7:0] != 8'd0);
    wire fill_walk = (ls_run == LS_FL) && (mem_left != 32'd0) && (!run_v || run_ext);
    wire fill_emit = (ls_run == LS_FL) && run_v && !rd_req_valid
                  && ((mem_left == 32'd0) || !run_ext);

    vec_agu #(.AW(AW)) u_agu (
        .clk(clk), .rst(rst),
        .wr_en(ld_en && ld_kind), .wr_ad(ld_addr[5:3]), .wr_fld(ld_addr[2:0]),
        .wr_val(ld_data),
        .start(ag_start), .sel(ag_sel), .off(ag_off),
        .step(fill_walk || ld_iss || sq_wr || dr_take),
        .addr(ag_addr), .last(), .total(ag_total), .busy(),
        .stride0(ag_stride0), .left0(ag_left0)
    );


    // ================================================== lanes
    reg          iss_valid, iss_is_cmp, iss_tail, red_init;
    reg  [1:0]   iss_phase, g_pm, g_pr;
    reg  [RW-1:0] iss_ra, iss_rb, iss_rc, iss_wa;
    reg  [CW-1:0] iss_chunk;
    reg  [2:0]   red_kind;
    reg  [15:0]  iss_tmask;
    reg          lw_we, lw_ract;
    reg          lw_rsel;              // the read uses port c, not a
    // VSHUF's predicate: pm 0 writes every lane, 1 the lanes P[pr] sets, 2 the rest.
    reg  [1:0]   ls_pm, ls_pr;
    reg  [RW-1:0] lw_waddr, lw_raddr;
    reg  [383:0] lw_wdata;
    wire [383:0] lw_rdata;
    wire [23:0]  red_result;
    wire         red_valid, pipe_empty, ls_quiet, wb_fire;
`ifdef VEC_LS_PIPE_EMPTY
    wire         ls_gate = pipe_empty;
`else
    wire         ls_gate = ls_quiet;
`endif
    wire [3:0]   wb_vreg;
    wire [VLMAX-1:0] p_bits;

    vec_lanes #(.MODEL(MODEL), .VLMAX(VLMAX), .LA(LD_TAP + 2), .RF_PRIM(RF_PRIM),
                .RF_PAD(RF_PAD), .RF_PACK(RF_PACK)) u_lanes (
        .clk(clk), .rst(rst), .mode(vmode),
        .ls_we(lw_we), .ls_pm(ls_pm), .ls_pr(ls_pr),
        .ls_waddr(lw_waddr), .ls_wdata(lw_wdata),
        .ls_raddr(lw_raddr), .ls_ractive(lw_ract), .ls_rsel(lw_rsel),
        .ls_rdata(lw_rdata),
        .iss_valid(iss_valid), .iss_phase(iss_phase),
        .iss_ra(iss_ra), .iss_rb(iss_rb), .iss_rc(iss_rc), .iss_wa(iss_wa),
        .iss_pm(g_pm), .iss_pr(g_pr), .iss_chunk(iss_chunk),
        .iss_is_cmp(iss_is_cmp), .iss_tail(iss_tail), .iss_tmask(iss_tmask),
        .st_op(g_op), .st_sa(g_sa), .st_sb(g_sb), .st_sc(g_sc),
        .st_ka(g_ka), .st_kb(g_kb), .st_kc(g_kc),
        .red_init(red_init), .red_kind(red_kind),
        .p_rd_sel(g_pr), .p_rd_bits(p_bits),
        .red_result(red_result), .red_valid(red_valid),
        .pipe_empty(pipe_empty), .ls_quiet(ls_quiet), .wb_ahead(wb_ahead),
        .wb_fire(wb_fire), .wb_vreg(wb_vreg)
    );

    // ================================================== decode
    wire [4:0] d_op = ir[31:27];
    wire [1:0] d_sa = ir[26:25], d_sb = ir[24:23], d_sc = ir[22:21];
    wire [3:0] d_vd = ir[20:17], d_va = ir[16:13];
    wire [3:0] d_vb = ir[12:9],  d_vc = ir[8:5];
    wire [1:0] d_pr = ir[4:3],   d_pm = ir[2:1];
    wire [2:0] d_dt = ir[26:24], d_ad = ir[23:21];
    wire [1:0] d_lpm = ir[15:14];
    wire signed [13:0] d_off = ir[13:0];

    // VDRAIN's peer overlay. VDRAIN reads `d_op`, `d_ad` and `d_off[8:0]` -- the
    // L1 start word -- and nothing else, so these bits are free; every encoding
    // written before this one leaves them zero, which reads as the memory sink.
    wire       d_nd    = ir[24];
    wire       d_ndsig = ir[25];
    wire [3:0] d_ndx   = ir[20:17];
    wire [3:0] d_ndy   = ir[16:13];
    wire [3:0] d_ndbuf = ir[12:9];

    wire is_alu = (d_op <= 5'h11);
    wire is_cmp = (d_op >= 5'h0B) && (d_op <= 5'h0D);
    // vec_alu numbers the four seeds 16..19; the ISA numbers them 14..17.
    wire [4:0] alu_op = (d_op >= 5'h0E && d_op <= 5'h11) ? (d_op + 5'd2) : d_op;
    wire dt_ok = (d_dt == DT_FP16) || (d_dt == DT_FP32);

    wire [2:0] dep = (vmode == M_FLAT) ? 3'd1 : (vmode == M_D2) ? 3'd2 : 3'd4;
    // CW+1 bits and it CANNOT overflow: O_VSETVL faults with F_VL unless vl is
    // 1..VLMAX, so vl[VW-1:4] is at most VLMAX/16 and so is the round-up.
    wire [CW:0] nchunk = vl[VW-1:4] + {{CW{1'b0}}, |vl[3:0]};

    wire [15:0] tail_full = 16'hFFFF;
    wire [15:0] tail_part = (16'd1 << vl[3:0]) - 16'd1;
    wire [15:0] tmask_now = ({1'b0, cchunk} < vl[VW-1:4]) ? tail_full : tail_part;

    // ================================================== prefetch
    // `im_addr` follows the next pc while an instruction runs, so its word is in
    // `im_q` when it completes; the states listed drive `im_addr` themselves.
    wire [8:0] pf_pc   = (st == S_EXEC) ? (pc + 9'd1) : pc;
    wire [8:0] pf_next = (lp_act && (pf_pc == lp_end) && (lp_cnt > 24'd1)) ? lp_top : pf_pc;
    wire pf_on = !((st == S_IDLE) || (st == S_F1) || (st == S_F2) || (st == S_DEC)
                || (st == S_GA) || (st == S_GB) || (st == S_GC) || (st == S_GD)
                || (st == S_SETI) || (st == S_SETI2));
    // A loop end still needs S_F1's bookkeeping, and S_F1 reports a CU_DATA fault.
    wire pf_go = (pf_addr == pc) && !(lp_act && (pc == lp_end)) && !cd_err;

    // Beats in flight per register: at most the D4 line's 4*ALAT, plus one.
    reg [6:0] pend [0:15];
    // The walk's register is off limits to an ALU op until the walk ends: a VLD
    // writes it (RAW, WAW), a VST reads it (WAR).
    wire haz_ls = ls_busy && !e_noreg && ((g_ra == e_reg) || (g_rb == e_reg)
                                          || (g_rc == e_reg) || (g_wd == e_reg));
    wire haz = (|pend[g_ra]) | (|pend[g_rb]) | (|pend[g_rc]) | (|pend[g_wd]) | haz_ls;

    // A FLAT op leaves a VLD no write slot until it ends (measured: ~30% of core
    // time stalled behind it), so while a VLD has words left ALU issue yields 1:1.
    reg  thr;
    wire ld_hungry  = (ls_run == LS_LD) && (ld_left != {(CW+2){1'b0}}) && (vmode == M_FLAT);
    wire alu_issue  = (st == S_ALU) && !((bcnt == 6'd0) && haz) && !(ld_hungry && thr);
    // VST reads through port a, or through port c when the issuing beat sources a
    // vector at a; it waits only for a beat that sources vectors at both.
    wire alu_va  = alu_issue && (g_sa[1:0] == SRC_V);
    wire alu_vc  = alu_issue && (g_sc[1:0] == SRC_V);
    wire sq_iss  = (ls_run == LS_ST) && (sq_iw != sq_nw) && !sq_blk
                && !(alu_va && alu_vc);
    // EXPSUM retires a vector write per beat like an ALU op, so it has to be
    // counted or a later read of vd would not wait for it.
    wire red_issue  = (st == S_RED) && (bcnt != nbeat)
                                    && (red_kind == R_EXPSUM);
    wire pend_inc   = (alu_issue && !g_cmp) || red_issue;

    function [23:0] pick;
        input [1:0] sel;
        input [3:0] rn;
        begin
            pick = (sel == SRC_S) ? sreg[rn]
                 : (sel == SRC_K) ? kreg[rn[1:0]] : 24'd0;
        end
    endfunction

    // ================================================== conversion
    // VCVT.FP16 reuses the store converters feeding the load converters, so a
    // round trip costs a mux rather than a third set. VCVT.FP32 is the
    // identity -- E8M15 -> FP32 -> E8M15 is exact (tests/vector/vec_cvt_tb s4).
    wire [255:0] o_f16, o_f32lo, o_f32hi;

    // VCVT's round trip is TWO conversions, and back to back in one cycle they
    // are the longest path in the vector core. `lw_rdata` is already stable
    // across S_STW and S_STD, so registering the midpoint costs no state.
    // The converter input is ONE REGISTER, selected a cycle early. Taking
    // `l1_q` straight off the BRAM put its clock-to-out in series with a
    // 16-lane FP16 normalise (286.9 MHz in the mesh); muxing the two sources
    // at the converter input then put `ls_kind` in series with that same
    // normalise (310.4 MHz). `ls_kind` is set at decode and stable for the
    // whole access, so choosing a cycle early chooses the same thing.
    reg  [255:0] cv_src;
    wire [383:0] i_f16;
    wire [191:0] i_f32;

    genvar e;
    generate
    for (e = 0; e < 16; e = e + 1) begin : g_c16
        vec_cvt_f16_to_e8 u_i (.f16(cv_src[e*16 +: 16]),
                               .e8(i_f16[e*24 +: 24]));
        vec_cvt_e8_to_f16 u_o (.e8(lw_rdata[e*24 +: 24]),
                               .f16(o_f16[e*16 +: 16]));
    end
    for (e = 0; e < 8; e = e + 1) begin : g_c32
        vec_cvt_f32_to_e8 u_i (.f32(cv_src[e*32 +: 32]),
                               .e8(i_f32[e*24 +: 24]));
        vec_cvt_e8_to_f32 u_ol (.e8(lw_rdata[e*24 +: 24]),
                                .f32(o_f32lo[e*32 +: 32]));
        vec_cvt_e8_to_f32 u_oh (.e8(lw_rdata[(e+8)*24 +: 24]),
                                .f32(o_f32hi[e*32 +: 32]));
    end
    endgenerate

    // lane rotate: vd[i] = va[(i+k) mod 16], as rotate-by-4k then rotate-by-k.
    // Two 4:1 stages are one LUT6 per bit each; the flat 16:1 was four.
    wire [383:0] shuf_hi, shuf_out;
    generate
    for (e = 0; e < 16; e = e + 1) begin : g_shf
        localparam integer H4 = (e+4) % 16, H8 = (e+8) % 16, H12 = (e+12) % 16;
        assign shuf_hi[e*24 +: 24] =
            (shuf_k[3:2] == 2'd0) ? lw_rdata[e*24   +: 24]
          : (shuf_k[3:2] == 2'd1) ? lw_rdata[H4*24  +: 24]
          : (shuf_k[3:2] == 2'd2) ? lw_rdata[H8*24  +: 24]
                                  : lw_rdata[H12*24 +: 24];
    end
    for (e = 0; e < 16; e = e + 1) begin : g_shl
        localparam integer L1 = (e+1) % 16, L2 = (e+2) % 16, L3 = (e+3) % 16;
        assign shuf_out[e*24 +: 24] =
            (shuf_k[1:0] == 2'd0) ? shuf_hi[e*24  +: 24]
          : (shuf_k[1:0] == 2'd1) ? shuf_hi[L1*24 +: 24]
          : (shuf_k[1:0] == 2'd2) ? shuf_hi[L2*24 +: 24]
                                  : shuf_hi[L3*24 +: 24];
    end
    endgenerate

    reg [383:0] bcast_out;
    integer bi;
    always @(*) begin
        bcast_out = 384'd0;
        for (bi = 0; bi < 16; bi = bi + 1) begin
            bcast_out[bi*24 +: 24] = sreg[g_ra];
        end
    end

    // ANY/ALL reduce the PREDICATE file, not the vector file: they never
    // enter the tree, so they cost one cycle and no ALU.
    //
    // vlmask is a REGISTER because as an expression it was the whole machine's
    // critical path: a 128-bit barrel shift and a 128-bit decrement sat in
    // front of the reduce, and `mm_mesh` reported vl_reg[6] -> sreg_reg at
    // 304.2 MHz. `vl` only moves on VSETVL, three states before any consumer.
    reg [VLMAX-1:0] vlmask;
    always @(posedge clk) begin
        // vl resets to VLMAX
        if (rst) begin
            vlmask <= {VLMAX{1'b1}};
        end
        else begin
            vlmask <= (
                (vl >= VLMAX[VW-1:0]) ? {VLMAX{1'b1}}
                : (({{(VLMAX-1){1'b0}}, 1'b1} << vl[VW-2:0]) - 1'b1)
            );
        end
    end
    wire p_any = |(p_bits & vlmask);
    wire p_all = ((p_bits & vlmask) == vlmask);

    // ================================================== sequencer
    // Leave a completed instruction: straight into S_EXEC on a prefetch hit.
    task next_insn;
        begin
            if (pf_go) begin
                ir <= im_q;
                st <= S_EXEC;
            end
            else begin
                st <= S_F1;
            end
        end
    endtask

    always @(posedge clk) begin
        // STATE, VALIDS AND NON-ZERO DEFAULTS ONLY: the payloads their valids
        // qualify came to ~1,700 flops, g_k* and lw_wdata being the worst.
        if (rst) begin
            st <= S_IDLE; pc <= 9'd0; im_addr <= 9'd0;
            vmode <= M_FLAT; vl <= VLMAX[VW-1:0];
            busy <= 1'b0; halted <= 1'b0; fault <= 1'b0; fault_code <= 8'd0;
            cycles <= 32'd0; bcnt <= {BW{1'b0}}; nbeat <= {BW{1'b0}};
            cchunk <= {CW{1'b0}}; cphase <= 2'd0;
            lp_act <= 1'b0; lp_cnt <= 24'd0; lp_top <= 9'd0; lp_end <= 9'd0;
            g_have <= 3'd0; fill_out <= 16'd0; mem_left <= 32'd0;
            iss_valid <= 1'b0; iss_tail <= 1'b0; red_init <= 1'b0;
            iss_is_cmp <= 1'b0; iss_tmask <= 16'hFFFF;
            lw_we <= 1'b0; lw_ract <= 1'b0; lw_rsel <= 1'b0;
            rd_req_valid <= 1'b0; wr_req_valid <= 1'b0; wr_req_first <= 1'b0;
            run_v <= 1'b0;
            pf_addr <= 9'd0;
            ld_left <= {(CW+2){1'b0}}; ld_v <= {(LD_TAP+1){1'b0}};
            ls_run <= LS_IDLE; ls_tail <= 1'b0; e_noreg <= 1'b1; thr <= 1'b0;
            sq_v <= 2'd0; dr_left <= 9'd0; dr_v <= {(DR_TAP+1){1'b0}}; wb_left <= 4'd0;
            nd_valid <= 1'b0;
            ag_start <= 1'b0;
            l1_we <= 1'b0; l1_cur <= {LAW{1'b0}};
            cd_err <= 1'b0;
            // RESET-RISK: sreg unreset like vec_regfile. pend stays -- it is a
            // hazard counter and a stale one stalls issue for good.
            for (ii = 0; ii < 16; ii = ii + 1) begin
                pend[ii] <= 7'd0;
            end
            kreg[0] <= 24'h000000; kreg[1] <= E8_ONE;
            kreg[2] <= 24'hBF8000; kreg[3] <= 24'h000000;
        end else begin
            iss_valid <= 1'b0;
            iss_tail  <= 1'b0;
            red_init  <= 1'b0;
            lw_we     <= 1'b0;
            ag_start  <= 1'b0;
            l1_we     <= 1'b0;
            pf_addr   <= im_addr;
            if (pf_on) begin
                im_addr <= pf_next;
            end
            ld_v <= {ld_v[LD_TAP-1:0], ld_iss};
            // FP32 issues an even count, so the word's half is the parity of what is left.
            ld_h <= {ld_h[LD_TAP-1:0], (e_dt == DT_FP32) && ld_left[0]};
            sq_v <= sq_blk ? 2'd0 : {sq_v[0], sq_iss};
            sq_h <= {sq_h[0], (e_dt == DT_FP32) && sq_iw[0]};
            ls_tail <= 1'b0;
            thr     <= ~thr;
            dr_v <= dr_blk ? {(DR_TAP+1){1'b0}} : {dr_v[DR_TAP-1:0], dr_iss};
            if (rd_req_valid && rd_req_ready) begin
                rd_req_valid <= 1'b0;
            end
            if (wr_req_valid && wr_req_ready) begin
                wr_req_valid <= 1'b0;
            end
            if (busy) begin
                cycles <= cycles + 32'd1;
            end
            cv_src <= (ls_kind == O_VCVT) ? o_f16 : l1_q;

            // A fill response lands where its tag says, whatever the order.
            // `fill_out` is updated ONCE, below: issuing and retiring can land
            // in the same cycle, and two assignments would drop one of them.
            if (rr_valid) begin
                l1_we    <= 1'b1;
                l1_waddr <= rr_tag;
                l1_wdata <= rr_data;
            end else if (cd_valid) begin
                l1_we    <= 1'b1;
                l1_waddr <= cd_addr;
                l1_wdata <= cd_data;
            end
            fill_out <= fill_out + (fill_emit ? {8'd0, run_cnt} : 16'd0)
                                 - (rr_valid  ? 16'd1 : 16'd0);

            // One update per register per cycle: an issue and a retire can
            // land together, and two assignments would drop one of them.
            for (ii = 0; ii < 16; ii = ii + 1) begin
                pend[ii] <= pend[ii]
                          + ((pend_inc && (g_wd == ii[3:0])) ? 5'd1 : 5'd0)
                          - ((wb_fire && (wb_vreg == ii[3:0])
                              && (pend[ii] != 5'd0)) ? 5'd1 : 5'd0);
            end

            case (st)
                // ---------------------------------------------------- fetch
                // A CU_DATA burst this core could not place is reported at the next
                // instruction boundary -- here if nothing was running when it
                // arrived, in S_F1 if a kernel was. Dropping it silently is the one
                // outcome not allowed: the data went nowhere and the kernel would
                // read stale L1 and be plausibly wrong.
                S_IDLE: if (start) begin
                    pc <= start_pc; im_addr <= start_pc;
                    busy <= 1'b1; halted <= 1'b0; fault <= 1'b0;
                    cycles <= 32'd0; g_have <= 3'd0; lp_act <= 1'b0;
                    if (cd_err) begin
                        cd_err <= 1'b0; fault_code <= F_CUDATA; st <= S_FAULT;
                    end
                    else begin
                        st <= S_F2;
                    end
                end

                // The hardware loop closes HERE, not after the case: inside the
                // block `st` still reads its old value, so testing it there would
                // never fire.
                S_F1: if (cd_err) begin
                    cd_err <= 1'b0; fault_code <= F_CUDATA; st <= S_FAULT;
                end else begin
                    if (lp_act && (pc == lp_end)) begin
                        if (lp_cnt > 24'd1) begin
                            lp_cnt  <= lp_cnt - 24'd1;
                            pc      <= lp_top;
                            im_addr <= lp_top;
                        end else begin
                            lp_act  <= 1'b0;
                            im_addr <= pc;
                        end
                    end
                    else begin
                        im_addr <= pc;
                    end
                    if (pf_addr == pf_next) begin
                        ir <= im_q;
                        st <= S_EXEC;
                    end
                    else begin
                        st <= S_F2;
                    end
                end
                S_F2: st <= S_DEC;
                S_DEC: begin ir <= im_q; st <= S_EXEC; end

                // ---------------------------------------------------- issue
                S_EXEC: begin
                    pc <= pc + 9'd1;
                    ls_kind <= d_op;
                    ls_reg  <= d_vd;
                    ls_dt   <= d_dt;
                    cchunk  <= {CW{1'b0}};
                    cphase  <= 2'd0;
                    bcnt    <= {BW{1'b0}};
                    // A walk in the engine still reads `l1_cur` and `ag_off` (vec_agu
                    // adds `off` combinationally), so only an idle engine lets go.
                    if (!ls_busy) begin
                        l1_cur <= d_off[LAW-1:0];
                        ag_sel <= d_ad;
                        ag_off <= 18'd0;
                    end

                    // vec_lanes reads g_op/g_s*/g_k* the cycle AFTER a beat is
                    // visible, so the previous op's last beat holds them one more.
                    if (is_alu && iss_valid) begin
                        pc <= pc;
                    end
                    else if (is_alu) begin
                        g_ra <= d_va; g_rb <= d_vb; g_rc <= d_vc; g_wd <= d_vd;
                        g_pm <= d_pm; g_pr <= d_pr; g_cmp <= is_cmp;
                        g_op[0 +: 5] <= alu_op;
                        g_sa[0 +: 2] <= d_sa; g_sb[0 +: 2] <= d_sb;
                        g_sc[0 +: 2] <= d_sc;
                        g_ka[0 +: 24] <= pick(d_sa, d_va);
                        g_kb[0 +: 24] <= pick(d_sb, d_vb);
                        g_kc[0 +: 24] <= pick(d_sc, d_vc);
                        if ((d_sa == SRC_C) || (d_sb == SRC_C) || (d_sc == SRC_C)) begin
                            fault_code <= F_CHAIN; st <= S_FAULT;
                        end else if (dep == 3'd1) begin
                            nbeat <= {2'd0, nchunk};
                            st <= S_ALU;
                        end else begin
                            g_have <= 3'd1;
                            st <= S_GA;
                        end
                    end else if (ls_busy) begin
                        // Only ALU ops run beside a walk; everything else waits.
                        pc <= pc;
                    end else begin
                        case (d_op)
                            O_VSETVL: begin
                                if ((sreg[d_va] == 24'd0) || (sreg[d_va] > VLMAX)) begin
                                    fault_code <= F_VL; st <= S_FAULT;
                                end else begin
                                    vl <= sreg[d_va][VW-1:0];
                                    st <= S_F1;
                                end
                            end
                            O_VSETMD: if (pipe_empty) begin
                                vmode <= d_va[1:0];
                                st <= S_F1;
                            end else begin
                                pc <= pc;            // hold; re-enter next cycle
                            end
                            O_VSETI: begin im_addr <= pc + 9'd1; st <= S_SETI; end
                            O_VLOOP: begin
                                if (lp_act) begin
                                    fault_code <= F_LOOP; st <= S_FAULT;
                                end else begin
                                    lp_act <= 1'b1;
                                    lp_cnt <= sreg[d_va];
                                    lp_top <= pc + 9'd1;
                                    lp_end <= pc + 9'd1 + {5'd0, d_vb};
                                    st <= S_F1;
                                end
                            end
                            O_VRED: begin
                                g_ra <= d_va; g_rb <= d_vb; g_rc <= d_va;
                                // EXPSUM keeps its elementwise result, and its vector
                                // destination rides in vb -- a unary leaf leaves the
                                // second source free. Every other kind writes vd only.
                                g_wd <= (d_vc[2:0] == R_EXPSUM) ? d_vb : 4'd0;
                                g_pr <= d_pr;
                                red_kind <= d_vc[2:0];
                                if (d_vc[2:0] >= 3'd6) begin
                                    // ANY/ALL complete in S_PRED.
                                    st <= S_PRED;
                                end else if (|vl[3:0]) begin
                                    // a partial chunk would feed stale slots into the
                                    // tree, and the tree has no per-slot mask
                                    fault_code <= F_REDVL; st <= S_FAULT;
                                end else if (vmode != M_TREE) begin
                                    fault_code <= F_OPCODE; st <= S_FAULT;
                                end else begin
                                    ls_reg <= d_vd;
                                    nbeat  <= red_half(d_vc[2:0])
                                            ? {1'd0, nchunk, 1'b0} : {2'd0, nchunk};
                                    red_init <= 1'b1;
                                    st <= S_RED;
                                end
                            end
                            O_VLD, O_VST, O_VCVT, O_VSHUF, O_VBCAST: begin
                                // VST's register field is a SOURCE, and in the
                                // load/store encoding it sits at vd.
                                g_ra <= (d_op == O_VST) ? d_vd : d_va;
                                shuf_k <= sreg[d_vb][3:0];
                                ls_pm  <= (d_op == O_VSHUF) ? d_pm : 2'd0;
                                ls_pr  <= d_pr;
                                bc_to_s <= (d_op == O_VBCAST) && (d_sa != SRC_S);
                                if ((d_op == O_VLD || d_op == O_VST || d_op == O_VCVT)
                                    && !dt_ok) begin
                                    fault_code <= F_DTYPE; st <= S_FAULT;
                                end else if ((d_op == O_VLD) || (d_op == O_VST)
                                             ? (|pend[d_vd]) : !ls_gate) begin
                                    pc <= pc;
                                end else begin
                                    ag_start <= 1'b1;
                                    ag_off   <= {{4{d_off[13]}}, d_off};
                                    st <= S_AGW;
                                end
                            end
                            // `nd_*` frame the word in `wr_req`, so they wait for
                            // the previous drain's last word to leave.
                            O_VFILL, O_VDRAIN: if (wr_req_valid) begin
                                pc <= pc;
                            end
                            else begin
                                nd_valid <= (d_op == O_VDRAIN) && d_nd;
                                nd_x     <= d_ndx;
                                nd_y     <= d_ndy;
                                nd_buf   <= d_ndbuf;
                                nd_sig   <= d_ndsig;
                                // Fills overlap: vec_cu names each response's L1 bank
                                // from its run's queue entry, so 8-bit tags never alias.
                                ag_start <= 1'b1;
                                st <= S_AGW;
                            end
                            O_VBAR:  st <= S_BAR;
                            O_VHALT: st <= S_WAITP;
                            default: begin fault_code <= F_OPCODE; st <= S_FAULT; end
                        endcase
                    end
                end

                // ---- gather the rest of a chain, one instruction per 4 cycles --
                S_GA: begin im_addr <= pc; st <= S_GB; end
                S_GB: st <= S_GC;
                S_GC: begin ir <= im_q; st <= S_GD; end
                S_GD: begin
                    pc <= pc + 9'd1;
                    g_op[g_have*5 +: 5] <= alu_op;
                    g_sa[g_have*2 +: 2] <= d_sa;
                    g_sb[g_have*2 +: 2] <= d_sb;
                    g_sc[g_have*2 +: 2] <= d_sc;
                    g_ka[g_have*24 +: 24] <= pick(d_sa, d_va);
                    g_kb[g_have*24 +: 24] <= pick(d_sb, d_vb);
                    g_kc[g_have*24 +: 24] <= pick(d_sc, d_vc);
                    g_wd  <= d_vd;
                    g_pm  <= d_pm;
                    g_pr  <= d_pr;
                    g_cmp <= is_cmp;
                    if ((d_sa == SRC_V) || (d_sb == SRC_V) || (d_sc == SRC_V)) begin
                        fault_code <= F_VSRC; st <= S_FAULT;
                    end else if (g_have + 3'd1 == dep) begin
                        nbeat <= {2'd0, nchunk} * {3'd0, dep};
                        st <= S_ALU;
                    end else begin
                        g_have <= g_have + 3'd1;
                        st <= S_GA;
                    end
                end

                // ---------------------------------------------------- ALU beats
                S_ALU: begin
                    if (alu_issue) begin
                        iss_valid  <= 1'b1;
                        iss_is_cmp <= g_cmp;
                        iss_chunk  <= cchunk;
                        iss_phase  <= cphase;
                        iss_tmask  <= tmask_now;
                        iss_ra <= {g_ra, cchunk};
                        iss_rb <= {g_rb, cchunk};
                        iss_rc <= {g_rc, cchunk};
                        iss_wa <= {g_wd, cchunk};
                        bcnt <= bcnt + 6'd1;
                        if (cphase + 2'd1 == dep[1:0] || dep == 3'd1) begin
                            cphase <= 2'd0;
                            cchunk <= cchunk + 3'd1;
                        end else begin
                            cphase <= cphase + 2'd1;
                        end
                        if (bcnt + 6'd1 == nbeat) begin
                            g_have <= 3'd0;
                            next_insn;
                        end
                    end
                end

                // ---------------------------------------------------- reduction
                S_RED: begin
                    if (bcnt == nbeat) begin
                        st <= S_RDRAIN;
                    end
                    else begin
                        iss_valid  <= 1'b1;
                        iss_is_cmp <= 1'b0;
                        iss_chunk  <= cchunk;
                        iss_phase  <= cphase;
                        iss_tmask  <= 16'hFFFF;
                        iss_ra <= {g_ra, cchunk};
                        iss_rb <= {g_rb, cchunk};
                        iss_rc <= {g_ra, cchunk};
                        iss_wa <= {g_wd, cchunk};
                        bcnt <= bcnt + 6'd1;
                        if (red_half(red_kind)) begin
                            if (cphase == 2'd1) begin
                                cphase <= 2'd0; cchunk <= cchunk + 3'd1;
                            end
                            else begin
                                cphase <= 2'd1;
                            end
                        end
                        else begin
                            cchunk <= cchunk + 3'd1;
                        end
                    end
                end
                S_RDRAIN: if (pipe_empty) begin
                    iss_valid <= 1'b1;
                    iss_tail  <= 1'b1;
                    iss_phase <= 2'd0;
                    iss_chunk <= {CW{1'b0}};
                    iss_tmask <= 16'hFFFF;
                    iss_ra <= {g_ra, {CW{1'b0}}};
                    iss_rb <= {g_rb, {CW{1'b0}}};
                    iss_rc <= {g_ra, {CW{1'b0}}};
                    st <= S_RWAIT;
                end
                S_RWAIT: if (red_valid) begin
                    sreg[ls_reg] <= red_result;
                    st <= S_F1;
                end

                // ------------------------------------- VCVT / VSHUF / VBCAST
                S_STR: begin
                    lw_ract  <= 1'b1;
                    lw_raddr <= {g_ra, cchunk};
                    st <= S_STW;
                end
                S_STW: st <= S_STD;
                S_STD: begin
                    case (ls_kind)
                        O_VCVT: begin
                            lw_wdata <= (ls_dt == DT_FP16) ? i_f16 : lw_rdata;
                            lw_waddr <= {ls_reg, cchunk};
                            lw_we    <= 1'b1;
                        end
                        O_VSHUF: begin
                            lw_wdata <= shuf_out;
                            lw_waddr <= {ls_reg, cchunk};
                            lw_we    <= 1'b1;
                        end
                        default: begin                      // VBCAST
                            if (bc_to_s) begin
                                sreg[ls_reg] <= lw_rdata[23:0];
                            end
                            else begin
                                lw_wdata <= bcast_out;
                                lw_waddr <= {ls_reg, cchunk};
                                lw_we    <= 1'b1;
                            end
                        end
                    endcase

                    if (bc_to_s || ({1'b0, cchunk} + 1'b1 == nchunk)) begin
                        lw_ract <= 1'b0;
                        st <= S_F1;
                    end else begin
                        cchunk <= cchunk + 3'd1;
                        st <= S_STR;
                    end
                end


                // ---------------------------------------------------- misc
                // ag_start is still high in THIS state and the AGU latches its
                // descriptor at the end of it, so neither ag_addr nor ag_total is
                // trustworthy until the state after. Every walk waits here first.
                S_AGW: begin
                    ld_left <= (ls_dt == DT_FP32) ? {nchunk, 1'b0} : {1'b0, nchunk};
                    sq_nw   <= (ls_dt == DT_FP32) ? {nchunk, 1'b0} : {1'b0, nchunk};
                    sq_iw   <= {(CW+2){1'b0}};
                    sq_lw   <= {(CW+2){1'b0}};
                    e_reg   <= ls_reg;
                    e_noreg <= (ls_kind != O_VLD) && (ls_kind != O_VST);
                    e_dt    <= ls_dt;
                    e_chunk <= {CW{1'b0}};
                    if ((ls_kind == O_VLD) || (ls_kind == O_VST)) begin
                        ls_run <= (ls_kind == O_VLD) ? LS_LD : LS_ST;
                        next_insn;
                    end else begin
                        st <= ((ls_kind == O_VFILL) || (ls_kind == O_VDRAIN)) ? S_MEMW1
                            : S_STR;
                    end
                end

                S_MEMW1: st <= S_MEMW2;
                S_MEMW2: st <= S_MEMW3;
                S_MEMW3: st <= S_MEM0;

                S_MEM0: if (ag_total > 32'd256) begin
                    fault_code <= F_LEN; st <= S_FAULT;
                end else begin
                    mem_left <= ag_total;
                    // For a peer drain the descriptor's BASE is the destination L1
                    // offset and its bounds are the count. One CU_DATA descriptor
                    // covers a contiguous run, so a STRIDED walk is not a peer
                    // drain -- the sink would read it as consecutive words.
                    //
                    // Base [23:16] is {ack_y, ack_x}: the instruction word has one
                    // spare bit, not eight, so the room is here and nowhere else.

                    // PINNED, NOT ADDRESS BITS. A peer drain reinterprets the base
                    // as this packed record, so widening AW must not move them.
                    nd_off  <= ag_addr[15:0];
                    nd_ack  <= ag_addr[23:16];
                    nd_mesh <= ag_addr[25:24];
                    nd_fin  <= ag_addr[33:26];
                    nd_len  <= ag_total[7:0] - 8'd1;
                    dr_ptr  <= l1_cur;
                    dr_left <= ag_total[8:0];
                    wb_left <= 4'd0;
                    ls_run <= (ls_kind == O_VFILL) ? LS_FL : LS_DR;
                    next_insn;
                end

                S_BAR:  if (fill_out == 16'd0) begin
                    next_insn;
                end
                // ANY/ALL of P[d_pr] into S[vd], once the lanes have drained.
                S_PRED: if (pipe_empty) begin
                    sreg[d_vd] <= (d_vc[2:0] == 3'd6)
                                ? (p_any ? E8_ONE : 24'd0)
                                : (p_all ? E8_ONE : 24'd0);
                    st <= S_F1;
                end
                S_SETI: st <= S_SETI2;
                S_SETI2: begin
                    if (d_sa == SRC_K) begin
                        kreg[3] <= im_q[23:0];
                    end
                    else begin
                        sreg[d_vd] <= im_q[23:0];
                    end
                    pc <= pc + 9'd1;
                    st <= S_F1;
                end
                // The last drain word must have left too: completion is reported
                // after the write ACKs, which are counted as the requests go out.
                S_WAITP: if (pipe_empty && (fill_out == 16'd0) && !wr_req_valid) begin
                    st <= S_HALT;
                end
                S_HALT: begin
                    busy <= 1'b0; halted <= 1'b1;
                    st <= S_IDLE;
                end
                S_FAULT: begin
                    busy <= 1'b0; fault <= 1'b1;
                    st <= S_IDLE;
                end
                default: st <= S_FAULT;
            endcase

            // ---------------------------------------- the load/store engine
            case (ls_run)
                LS_LD: begin
                    if (ld_iss) begin
                        l1_raddr <= ag_addr[LAW-1:0];
                        ld_left  <= ld_left - 1'b1;
                    end
                    if (ld_wpre) begin
                        lw_waddr <= {e_reg, e_chunk};
                        lw_we    <= 1'b1;
                        e_chunk  <= e_chunk + 1'b1;
                    end
                    if (ld_land) begin
                        if ((e_dt == DT_FP32) && !ld_h[LD_TAP]) begin
                            ls_hold <= i_f32;
                        end
                        else begin
                            lw_wdata <= (e_dt == DT_FP16) ? i_f16 : {i_f32, ls_hold};
                        end
                        if ((ld_left == {(CW+2){1'b0}})
                            && (ld_v[LD_TAP-1:0] == {LD_TAP{1'b0}})) begin
                            ls_run  <= LS_IDLE;
                            ls_tail <= 1'b1;
                        end
                    end
                end
                LS_ST: begin
                    // A port is VST's only in the cycles it reads.
                    lw_ract <= sq_iss;
                    lw_rsel <= alu_va;
                    if (sq_iss) begin
                        lw_raddr <= {e_reg, (e_dt == DT_FP32) ? sq_iw[CW:1] : sq_iw[CW-1:0]};
                        sq_iw    <= sq_iw + 1'b1;
                    end
                    if (sq_blk) begin
                        sq_iw <= sq_lw;
                    end
                    if (sq_wr) begin
                        l1_waddr <= ag_addr[LAW-1:0];
                        l1_we    <= 1'b1;
                        l1_wdata <= (e_dt == DT_FP16) ? o_f16
                                  : (sq_h[1] ? o_f32hi : o_f32lo);
                        sq_lw    <= sq_lw + 1'b1;
                        if (sq_lw + 1'b1 == sq_nw) begin
                            lw_ract <= 1'b0;
                            lw_rsel <= 1'b0;
                            ls_run  <= LS_IDLE;
                            ls_tail <= 1'b1;
                        end
                    end
                end
                // VFILL's walk: a word address a cycle, coalesced into streamed runs.
                LS_FL: begin
                    if (fill_walk) begin
                        if (!run_v) begin
                            run_v    <= 1'b1;
                            run_addr <= ag_addr;
                            run_tag  <= l1_cur;
                            run_cnt  <= 8'd1;
                        end else begin
                            run_cnt  <= run_cnt + 8'd1;
                        end
                        run_next <= ag_addr + 32;
                        l1_cur   <= l1_cur + 1'b1;
                        mem_left <= mem_left - 32'd1;
                    end
                    if (fill_emit) begin
                        rd_req_valid <= 1'b1;
                        rd_req_addr  <= run_addr;
                        rd_req_tag   <= run_tag;
                        rd_req_cnt   <= run_cnt;
                        run_v        <= 1'b0;
                    end
                    if ((mem_left == 32'd0) && !run_v) begin
                        ls_run  <= LS_IDLE;
                        ls_tail <= 1'b1;
                    end
                end
                // VDRAIN. `l1_cur`/`mem_left` count words TAKEN; `dr_ptr`/`dr_left`
                // words read, and a blocked landing rewinds the latter to the former.
                LS_DR: begin
                    if (dr_iss) begin
                        l1_raddr <= dr_ptr;
                        dr_ptr   <= dr_ptr + 1'b1;
                        dr_left  <= dr_left - 9'd1;
                    end
                    if (dr_blk) begin
                        dr_ptr  <= l1_cur;
                        dr_left <= mem_left[8:0];
                    end
                    if (dr_take) begin
                        wr_req_valid <= 1'b1;
                        wr_req_addr  <= ag_addr;
                        wr_req_data  <= l1_q;
                        wr_req_first <= (wb_left == 4'd0);
                        wr_req_cnt   <= dr_blen;
                        wb_left      <= (wb_left == 4'd0) ? (dr_blen - 4'd1)
                                                          : (wb_left - 4'd1);
                        l1_cur       <= l1_cur + 1'b1;
                        mem_left     <= mem_left - 32'd1;
                    end
                    if ((mem_left == 32'd0) || (dr_take && (mem_left == 32'd1))) begin
                        ls_run  <= LS_IDLE;
                        ls_tail <= 1'b1;
                    end
                end
                default: ;
            endcase

            // LAST, so a fault arriving in the same cycle one is cleared above
            // is not the one that gets dropped.
            if (cd_fault) begin
                cd_err <= 1'b1;
            end
        end
    end

`ifdef VC_STATE_PROF
    // Cycles in each state, dumped at `final`.
    reg [31:0] sprof [0:63];
    integer si;
    initial begin
        for (si = 0; si < 64; si = si + 1) sprof[si] = 32'd0;
    end
    always @(posedge clk) begin
        if (!rst) begin
            sprof[st] <= sprof[st] + 32'd1;
        end
    end
    // Fill runs: how many requests the fills became, and the first few verbatim.
    reg [31:0] pr_runs = 32'd0, pr_words = 32'd0;
    // The fill walk split into walking, waiting on the previous request, the rest;
    // S_EXEC cycles per opcode, so a stall names the instruction it holds.
    reg [31:0] pf_walk = 32'd0, pf_blk = 32'd0, pf_oth = 32'd0;
    reg [31:0] pexec [0:31];
    initial begin
        for (si = 0; si < 32; si = si + 1) pexec[si] = 32'd0;
    end
    always @(posedge clk) begin
        if (!rst && (ls_run == LS_FL)) begin
            if (fill_walk) pf_walk <= pf_walk + 32'd1;
            else if (run_v && rd_req_valid) pf_blk <= pf_blk + 32'd1;
            else pf_oth <= pf_oth + 32'd1;
        end
        if (!rst && (st == S_EXEC)) pexec[d_op] <= pexec[d_op] + 32'd1;
    end
    always @(posedge clk) begin
        if (!rst && fill_emit) begin
            pr_runs  <= pr_runs + 32'd1;
            pr_words <= pr_words + {24'd0, run_cnt};
            if (pr_runs < 32'd12) begin
                $display("VFILLRUN %m addr %h tag %0d cnt %0d stride %0d", run_addr,
                         run_tag, run_cnt, ag_stride0);
            end
        end
    end
    // Every port's busy cycles, for a utilisation table: lane issue beats, the
    // register file's load/store traffic, L1's read and write ports, the walks.
    reg [31:0] pu_busy = 32'd0, pu_beat = 32'd0, pu_rfls = 32'd0, pu_rfrd = 32'd0;
    reg [31:0] pu_l1r = 32'd0, pu_l1w = 32'd0, pu_fill = 32'd0, pu_drw = 32'd0;
    reg [31:0] pu_drblk = 32'd0, pu_sqblk = 32'd0, pu_insn = 32'd0;
    reg [31:0] pu_ls = 32'd0, pu_ovl = 32'd0, pu_ldgap = 32'd0, pu_stgap = 32'd0;
    always @(posedge clk) begin
        if (!rst) begin
            if (ls_run != LS_IDLE) pu_ls <= pu_ls + 32'd1;
            if ((ls_run != LS_IDLE) && iss_valid) pu_ovl <= pu_ovl + 32'd1;
            if ((ls_run == LS_LD) && (ld_left != 0) && wb_ahead) pu_ldgap <= pu_ldgap + 32'd1;
            if ((ls_run == LS_ST) && (sq_iw != sq_nw) && !sq_iss) pu_stgap <= pu_stgap + 32'd1;
        end
    end
    reg [5:0]  pu_pst = 6'd0;
    always @(posedge clk) begin
        if (!rst) begin
            pu_pst <= st;
            if (busy) pu_busy <= pu_busy + 32'd1;
            if (iss_valid) pu_beat <= pu_beat + 32'd1;
            if (lw_we) pu_rfls <= pu_rfls + 32'd1;
            if (sq_iss || (st == S_STR)) pu_rfrd <= pu_rfrd + 32'd1;
            if (ld_iss || dr_iss) pu_l1r <= pu_l1r + 32'd1;
            if (l1_we) pu_l1w <= pu_l1w + 32'd1;
            if (rr_valid) pu_fill <= pu_fill + 32'd1;
            if (dr_take) pu_drw <= pu_drw + 32'd1;
            if (dr_blk) pu_drblk <= pu_drblk + 32'd1;
            if (sq_blk) pu_sqblk <= pu_sqblk + 32'd1;
            if ((st == S_EXEC) && (pu_pst != S_EXEC)) pu_insn <= pu_insn + 32'd1;
        end
    end
    final begin
        $display("VRES %m busy %0d beats %0d rf_ls_wr %0d rf_ls_rd %0d l1_rd %0d l1_wr %0d fill_words %0d drain_words %0d drain_replays %0d vst_replays %0d insns %0d",
                 pu_busy, pu_beat, pu_rfls, pu_rfrd, pu_l1r, pu_l1w, pu_fill, pu_drw,
                 pu_drblk, pu_sqblk, pu_insn);
        $display("VLSENG %m busy %0d overlap %0d ld_wait %0d st_wait %0d",
                 pu_ls, pu_ovl, pu_ldgap, pu_stgap);
        for (si = 0; si < 64; si = si + 1) begin
            if (sprof[si] != 32'd0) $display("VSTATE %m %0d %0d", si, sprof[si]);
        end
        $display("VFILLRUNS %m runs %0d words %0d", pr_runs, pr_words);
        $display("VFILLCYC %m walk %0d blocked %0d other %0d", pf_walk, pf_blk, pf_oth);
        for (si = 0; si < 32; si = si + 1) begin
            if (pexec[si] != 32'd0) $display("VEXEC %m op %0d %0d", si, pexec[si]);
        end
    end
`endif

endmodule

`default_nettype wire

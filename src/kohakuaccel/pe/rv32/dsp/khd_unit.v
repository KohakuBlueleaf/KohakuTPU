// khd_unit -- the DSP extension: the vector register file, the lane array, the
// cross-lane network, the accumulators, and the vector scratchpad.
//
// It attaches to the base core the way the base core attaches to memory: the
// instruction arrives in EX, the operands come out of an array in MEM, and the
// result is written at the end of MEM. Nothing about the scalar pipeline moves.
//
//   EX    DECODE, and the vector read addresses go to the file; rs1+imm goes to
//         the vector scratchpad as a load address
//   MEM   operands out; the lanes compute; the vector file is WRITTEN
//   WB    a scalar result (vextr, vredsum, vredmax) joins rv_mem's writeback
//
// DECODE IS IN EX AND ITS RESULT IS REGISTERED, which is the same structure
// rv_id has and for the same measured reason. Decoding from the MEM-stage
// instruction instead put the whole control cone -- funct7 to the operation
// select, and the element width through a barrel shifter that builds the shift
// masks -- IN SERIES with the lane datapath and the result mux, on the way to
// the register file's write port: 24 logic levels, and the unit closed at
// 172.7 MHz against a 3.333 ns ask. Registering the decode costs ~90 flops and
// takes the control cone out of the cycle entirely.
//
// THE SHIFT MASKS ARE BUILT ONCE, IN EX, FOR EVERY LANE. The amount and the
// element width are the same in every lane, so the three small shifters that
// build them are shared -- and being registered, they are ready at the start of
// MEM rather than half way through it.
//
// THE VECTOR FILE IS WRITTEN IN MEM, NOT IN WB, and that is worth a cycle a
// time. Writing in WB would leave a dependent instruction reading a stale entry
// at BOTH distance 1 and distance 2, because a read_first array returns the
// pre-write value for a read captured at the write's own edge. Writing a stage
// earlier leaves only distance 1, which is the same shape as the base core's
// load-use and costs one stall instead of two -- on `vld; vld; vdot`, the
// innermost loop of every kernel here, that is 4 cycles per 32 MACs instead of 5.
//
// UNIFORM CONTROL, NO MASKS, NO PER-LANE ADDRESSING. One PC, one instruction,
// every lane doing the same thing. Divergence and per-lane gather are the GPU
// PE's territory and nothing here anticipates them.
//
// LATENCIES, and every stall in the unit comes from one of these:
//
//   ALU, logic, shift, permute, moves, vld, vst    complete in MEM
//   vmul                                           one extra cycle in MEM
//   vdot                                           issues at II=1; the
//                                                  accumulate lands two cycles
//                                                  later, in the background
//
// `vdot` not stalling is the point of the accumulator: a stream of them
// accumulates correctly at one per cycle because each one's product reaches the
// accumulate stage in issue order. Only reading or disturbing an accumulator
// (`vaccrd`, `vaccz`, `vaccwr`) has to wait for that pipeline to drain.

`default_nettype none

module khd_unit #(
    parameter integer SIMD          = 8,        // 32-bit lanes; VW = 32*SIMD
    parameter integer VREGS         = 8,
    parameter integer NACC          = 2,
    parameter integer VSPAD_ENTRIES = 1024,
    parameter integer MULS          = 4,        // 4 = int8 dot at II=1
    parameter integer HAS_SHIFT     = 1,
    parameter integer HAS_PERM      = 1,
    // The float tier, on custom-1. 0 elaborates none of it and leaves the
    // opcode major unmapped, so a float instruction faults.
    parameter integer HAS_F16       = 0,
    // Rotating partials per slot, and the lane's latency. NPART must EXCEED
    // ALAT or a partial is re-read before its write returns -- and the count is
    // architectural, because float addition does not associate.
    parameter integer NPART         = 16,
    parameter integer F16_ALAT      = 15,
    // 1 swaps DSP48E2 for vec_dsp's behavioural model. Defaults to the
    // SYNTHESIS value, so a bench that forgets it fails to elaborate rather
    // than quietly measuring a different multiplier.
    parameter integer F16_MODEL     = 0,
    // Where the vector file is written. See the header: 0 keeps the RAW hazard
    // at distance 1 and puts read-compute-write in one cycle; 1 registers the
    // result first, costing a second stall and halving the path. Both are
    // built and measured, as FWD_X is in the base core.
    parameter integer WB_STAGE      = 0,
    // The reduction tree, split across two cycles. It runs once per reduction
    // rather than once per element, so the cycle is free and the depth is not.
    parameter integer RED_PIPE      = 1,
    parameter         USE_DSP       = "yes",
    parameter         MEM_PRIM      = "block",
    parameter         VREG_PRIM     = "distributed"
)(
    input  wire        clk,
    input  wire        resetn,

    // ---- the EX stage, combinational ----
    input  wire        x_valid,     // a KohakuDSP instruction is in EX
    input  wire [31:0] x_instr,
    input  wire [31:0] x_addr,      // rs1 + imm, from the EX adder
    input  wire [31:0] x_xdata,     // rs1's value, for vsplat
    input  wire        x_hold,      // the core is holding EX for its own reason
    output wire        stall,       // this unit needs EX held
    output wire        x_illegal,   // not built in this configuration
    output wire        x_misalign,  // a vld/vst address is not vector-aligned

    // ---- scalar writeback, into rv_mem's W stage ----
    output reg         w_sc_valid,
    output reg  [31:0] w_sc,

    // The vector file's write port, one entry per committing instruction:
    // visible to a bench and to nothing in the machine, and the reason the
    // component test can fail on the instruction that was wrong.
    output wire                 dbg_wr_valid,
    output wire [4:0]           dbg_wr_vd,
    output wire [32*SIMD-1:0]   dbg_wr_data,

    // ---- NoC window writes into the vector scratchpad ----
    input  wire        noc_en,
    input  wire [3:0]  noc_we,
    input  wire [$clog2(VSPAD_ENTRIES*SIMD)-1:0] noc_word,
    input  wire [31:0] noc_wdata,

    // ---- a scalar store into the vector scratchpad ----
    // It uses the port the VECTOR unit owns, not the NoC's, and it can because
    // only one instruction is in MEM at a time. Sharing the NoC's port instead
    // cost the assembled PE 93.6 MHz (284.3 against a 377.9 baseline): the
    // arbitration put the receive FIFO's empty flag into the MEM stall and from
    // there into the fetch address, which is the tie the base core's requestor
    // registers a cycle to avoid.
    input  wire        sc_st_valid,     // a scalar vspad store is in MEM
    input  wire [31:0] sc_st_addr,
    input  wire [3:0]  sc_st_be,
    input  wire [31:0] sc_st_data
);
    localparam integer VW  = 32 * SIMD;
    localparam integer VAW = $clog2(VREGS);
    localparam integer AAW = (NACC > 1) ? $clog2(NACC) : 1;
    localparam integer RAW = $clog2(VSPAD_ENTRIES);
    localparam integer LAW = $clog2(SIMD);
    localparam integer VBYTES = VW / 8;

`include "khd_isa.vh"

    localparam [2:0] OP_ADD = 3'd0, OP_MIN = 3'd1, OP_MAX = 3'd2,
                     OP_AND = 3'd3, OP_OR  = 3'd4, OP_XOR = 3'd5,
                     OP_ANDN = 3'd6, OP_SH = 3'd7;

    // ================= EX: decode ==========================================
    wire [2:0]  f3  = x_instr[14:12];
    wire [6:0]  f7  = x_instr[31:25];
    wire [4:0]  rdf = x_instr[11:7];
    wire [4:0]  r1f = x_instr[19:15];
    wire [4:0]  r2f = x_instr[24:20];
    wire [1:0]  et  = f7[1:0];
    wire [4:0]  op5 = f7[6:2];
    wire [3:0]  op4 = f7[6:3];
    wire [2:0]  ix3 = f7[2:0];

    // Which opcode major this instruction belongs to. Custom-0 and custom-1
    // both number their funct3 groups from zero, so every test below has to be
    // qualified or a `vfmacc` reads as a `vld`.
    wire is_f_maj = (HAS_F16 != 0) && (x_instr[6:0] == KHF_OPCODE);
    wire is_i_maj = !is_f_maj;

    wire is_vld = is_i_maj && (f3 == KHD_F3_VLD);
    wire is_vst = is_i_maj && (f3 == KHD_F3_VST);
    wire is_int = is_i_maj && (f3 == KHD_F3_VINT);
    wire is_bit = is_i_maj && (f3 == KHD_F3_VBIT);
    wire is_shi = is_i_maj && (f3 == KHD_F3_VSHI);
    wire is_mac = is_i_maj && (f3 == KHD_F3_VMAC);
    wire is_mov = is_i_maj && (f3 == KHD_F3_VMOV);
    wire is_prm = is_i_maj && (f3 == KHD_F3_VPRM);

    // ---- the float tier, on custom-1 ----
    wire is_fmac  = is_f_maj && (f3 == KHF_F3_FMAC);
    wire is_fred  = is_f_maj && (f3 == KHF_F3_FRED);
    wire is_fma   = is_fmac && ((op5 == KHF_FMAC_FMACC) || (op5 == KHF_FMAC_FMSAC));
    wire is_fmsub = is_fmac && (op5 == KHF_FMAC_FMSAC);
    wire is_facz  = is_fmac && (op5 == KHF_FMAC_FACCZ);
    wire is_facrd = is_fmac && (op5 == KHF_FMAC_FACCRD);
    wire is_facwr = is_fmac && (op5 == KHF_FMAC_FACCWR);
    wire is_frsum = is_fred && (op5 == KHF_FRED_FREDSUM);
    // Reading an accumulator means folding it first, and that runs for
    // NPART*ALAT cycles -- so both forms stall until the fold reports done.
    wire is_ffold = is_facrd;

    wire is_dot   = is_mac && ((op5 == KHD_MAC_DOT) || (op5 == KHD_MAC_DOTN));
    wire is_dotn  = is_mac && (op5 == KHD_MAC_DOTN);
    wire is_accz  = is_mac && (op5 == KHD_MAC_ACCZ);
    wire is_accrd = is_mac && (op5 == KHD_MAC_ACCRD);
    wire is_accwr = is_mac && (op5 == KHD_MAC_ACCWR);
    wire is_mul   = is_int && (op5 == KHD_INT_MUL);
    wire is_splat = is_mov && (f7 == KHD_MOV_SPLAT);
    wire is_extr  = is_mov && (f7 == KHD_MOV_EXTR);
    wire is_rsum  = is_mov && (f7 == KHD_MOV_REDSUM);
    wire is_rmax  = is_mov && (f7 == KHD_MOV_REDMAX);

    // A store's DATA register rides in the rd field, so read port 1 serves it.
    wire [4:0] p1f = is_vst ? rdf : r1f;

    wire use_p1 = is_vst || is_int || is_bit || is_shi || is_prm
                || (is_mac && (is_dot || is_accwr))
                || (is_mov && !is_splat)
                || is_fma || is_facwr;
    wire use_p2 = is_int || is_bit || is_dot
                || (is_prm && (op4 <= KHD_PRM_PACK_S32))
                || is_fma;

    wire wr_vreg = is_vld || is_int || is_bit || is_shi || is_prm
                 || is_accrd || is_splat || is_facrd;
    wire wr_acc  = is_dot || is_accz || is_accwr;
    wire wr_sc   = is_extr || is_rsum || is_rmax;
    // What has to wait for an accumulate in flight, and `vdot` is NOT on the
    // list: see the hazard section below.
    wire wait_acc = is_accz || is_accwr || is_accrd;

    reg [2:0] d_alu_op;
    always @(*) begin
        if (is_bit)
            case (f7)
                KHD_BIT_AND:  d_alu_op = OP_AND;
                KHD_BIT_OR:   d_alu_op = OP_OR;
                KHD_BIT_XOR:  d_alu_op = OP_XOR;
                default:      d_alu_op = OP_ANDN;
            endcase
        else if (is_shi) d_alu_op = OP_SH;
        else if (is_int && (op5 == KHD_INT_MIN)) d_alu_op = OP_MIN;
        else if (is_int && (op5 == KHD_INT_MAX)) d_alu_op = OP_MAX;
        else d_alu_op = OP_ADD;
    end

    wire d_alu_sub = is_int && ((op5 == KHD_INT_SUB) || (op5 == KHD_INT_SSUB));
    wire d_alu_sat = is_int && ((op5 == KHD_INT_SADD) || (op5 == KHD_INT_SSUB));
    // Decided HERE, not in the lane: min and max need a - b for their compare,
    // and deriving that per lane put a LUT between the decode register and the
    // carry chain -- the first two levels of the binding path.
    wire d_cmp_sub = d_alu_sub || (d_alu_op == OP_MIN) || (d_alu_op == OP_MAX);

    // ---- the shift masks, built once here rather than SIMD times in MEM ----
    wire [4:0] d_sh_amt = (et == KHD_ET_S8)  ? {2'd0, r2f[2:0]}
                        : (et == KHD_ET_S16) ? {1'd0, r2f[3:0]} : r2f;
    wire d_sh_left  = is_shi && (op5 == KHD_SH_SLLI);
    wire d_sh_arith = is_shi && ((op5 == KHD_SH_SRAI) || (op5 == KHD_SH_SRARI));
    wire d_sh_round = is_shi && (op5 == KHD_SH_SRARI);

    wire [7:0]  kr8  = 8'hFF  >> d_sh_amt;
    wire [7:0]  kl8  = 8'hFF  << d_sh_amt;
    wire [15:0] kr16 = 16'hFFFF >> d_sh_amt;
    wire [15:0] kl16 = 16'hFFFF << d_sh_amt;
    wire [31:0] kr32 = 32'hFFFFFFFF >> d_sh_amt;
    wire [31:0] kl32 = 32'hFFFFFFFF << d_sh_amt;

    wire [31:0] keep_r = (et == KHD_ET_S8)  ? {4{kr8}}
                       : (et == KHD_ET_S16) ? {2{kr16}} : kr32;
    wire [31:0] keep_l = (et == KHD_ET_S8)  ? {4{kl8}}
                       : (et == KHD_ET_S16) ? {2{kl16}} : kl32;
    wire [31:0] d_sh_keep = d_sh_left ? keep_l : keep_r;

    wire [31:0] one_at = (d_sh_amt == 5'd0) ? 32'd0 : (32'd1 << (d_sh_amt - 5'd1));
    wire [31:0] d_sh_rmask = (et == KHD_ET_S8)  ? {4{one_at[7:0]}}
                           : (et == KHD_ET_S16) ? {2{one_at[15:0]}} : one_at;

    wire [4:0] d_sh_rot = d_sh_left ? (5'd0 - d_sh_amt) : d_sh_amt;

    // Each element's MSB: the SWAR adder's mask. Built here and registered so
    // the lanes see it at the start of MEM rather than through a mux.
    wire [31:0] d_el_mask = (et == KHD_ET_S8)  ? 32'h8080_8080
                          : (et == KHD_ET_S16) ? 32'h8000_8000
                                               : 32'h8000_0000;

    // ---- what this configuration refuses ---------------------------------
    // An encoding a build does not carry must FAULT, not compute something
    // plausible: that is what makes "a variant without a tier lacks those
    // encodings" a checkable statement rather than a description.
    wire bad_reg = (use_p1 && (p1f >= VREGS)) || (use_p2 && (r2f >= VREGS))
                 || (wr_vreg && (rdf >= VREGS))
                 || (wr_acc && (rdf >= NACC)) || (is_accrd && (r1f >= NACC))
                 || ((is_fma || is_facz || is_facwr) && (rdf >= NACC))
                 || (is_ffold && (r1f >= NACC));
    // Custom-1 without the float tier is an unmapped opcode major, and any
    // float group this build does not implement is unmapped within it.
    wire bad_flt = (HAS_F16 == 0) && (x_instr[6:0] == KHF_OPCODE);
    // `vfredsum` is NOT BUILT YET and therefore faults. The fold combines the
    // partials WITHIN each slot; crossing the slots is a second pass that does
    // not exist, and returning slot 0 alone would be a plausible wrong answer
    // -- which is the one thing a refusal is for.
    wire bad_fgrp = is_f_maj && !(is_fma || is_facz || is_facrd || is_facwr);
    wire bad_fet  = is_f_maj && (et != KHF_FT_F16) && !is_facz;
    wire bad_et  = (is_int || is_shi) && (et == 2'd3);
    // int8 needs FOUR products per 32-bit lane, for `vmul` exactly as much as
    // for `vdot`: a two-multiplier lane returns zero for the top two elements.
    wire bad_cfg = (is_shi && (HAS_SHIFT == 0))
                 || (is_prm && (HAS_PERM == 0))
                 || ((is_dot || is_mul) && (et == KHD_ET_S8) && (MULS < 4))
                 || (is_dot && (et >= KHD_ET_S32))
                 || (is_mul && (et >= KHD_ET_S32));
    wire bad_grp = !(is_vld || is_vst || is_int || is_bit || is_shi
                   || is_mac || is_mov || is_prm
                   || is_fma || is_facz || is_facrd || is_facwr);
    assign x_illegal = x_valid && (bad_reg || bad_et || bad_cfg || bad_grp
                                   || bad_flt || bad_fgrp || bad_fet);

    assign x_misalign = x_valid && (is_vld || is_vst)
                      && (|x_addr[$clog2(VBYTES)-1:0]);

    // ================= the MEM-stage register ==============================
    // The DECODE is registered, not the instruction: see the header.
    reg         m_valid, m_left;
    reg  [31:0] m_addr, m_xdata;
    reg  [4:0]  m_vd, m_rs1;
    reg  [1:0]  m_et;
    reg  [2:0]  m_alu_op;
    reg         m_cmp_sub, m_alu_sat;
    reg  [4:0]  m_sh_rot;
    reg  [31:0] m_sh_keep, m_sh_rmask, m_el_mask;
    reg         m_sh_arith, m_sh_left, m_sh_round;
    reg         m_is_vld, m_is_vst, m_is_dot, m_is_dotn, m_is_accz;
    reg         m_is_accrd, m_is_accwr, m_is_mul, m_is_splat;
    reg         m_is_extr, m_is_rsum, m_is_rmax, m_is_prm;
    reg         m_is_fma, m_is_fmsub, m_is_facz, m_is_facrd, m_is_facwr;
    reg         m_is_frsum, m_is_ffold;
    reg         m_wr_vreg_d, m_wr_sc_d;
    reg  [3:0]  m_prm_op4;
    reg  [2:0]  m_prm_idx;
    reg  [LAW-1:0] m_lane;

    // A folding instruction is not complete until the fold reports done, so it
    // writes back once -- on that cycle -- rather than every cycle it waits.
    wire hz_fold;
    wire m_wr_vreg  = m_valid && !m_left && !hz_fold && m_wr_vreg_d;
    wire m_wr_sc    = m_valid && !m_left && !hz_fold && m_wr_sc_d;
    wire m_complete = m_valid && !m_left && !hz_fold;

    // The float tier's signals, DECLARED BEFORE THE HAZARDS THAT READ THEM:
    // xvlog rejects a use-before-declare that synthesis accepts silently, and
    // rv_id.v carries the same note for the same reason.
    localparam integer FSLOTS = 2 * SIMD;
    localparam integer FPW    = (NPART > 1) ? $clog2(NPART) : 1;

    wire [VW-1:0]        facc_rd_v;
    wire                 f_busy, f_done, f_iss, f_raw, f_sweep;
    wire                 f_sw_hold, f_rd_hold, f_inflight;
    wire [FPW-1:0]       f_idx;
    wire [24*FSLOTS-1:0] fold_part_v;

    // ---- hazards ----------------------------------------------------------
    // Distance 1 only: the file is written at the end of MEM, so an instruction
    // two behind reads the new value. A VW-wide forwarding mux is 256 LUT on
    // the widest path in the unit, against one cycle on a dependency software
    // can usually unroll away -- so this stalls, and rv_regfile's opposite
    // verdict at 32 bits is the reason it is worth saying which is which.
    wire hz_raw = x_valid && m_wr_vreg
                && ((use_p1 && (p1f == m_vd)) || (use_p2 && (r2f == m_vd)));
    // At WB_STAGE the write lands a cycle later, so distance 2 is stale too:
    // a read_first array returns the pre-write value for a read captured at
    // the write's own edge.
    wire hz_wb;

    // A store owns the scratchpad's address port in MEM; a load wants it in EX.
    // A SCALAR store is the same conflict and is NOT handled here: this stall
    // holds the core's MEM stage, so waiting on something in that stage never
    // ends. rv_core bubbles that pair in decode instead.
    wire hz_spad = x_valid && is_vld && m_valid && m_is_vst;

    // A dot's products register at the end of its MEM cycle and their sum one
    // cycle after that, so the accumulate fires on the SECOND stage of this
    // shift register. `m_is_dot` covers the MEM cycle itself.
    //
    // A DOT DOES NOT WAIT FOR A DOT: each sum reaches the accumulate stage in
    // issue order beside its own destination index, and the accumulate is a
    // one-cycle recurrence, so one may arrive every cycle. Making `vdot` wait
    // like the other three cost 3 cycles per dot on `dot2_i8_v` -- 80 cycles
    // for 58 instructions, where every other hazard accounts for 9.
    // The three that DO wait need the accumulator settled: `vaccrd` reads it
    // combinationally, `vaccz` / `vaccwr` write it in MEM, and an older dot's
    // add would land on top of that write.
    reg [1:0] acc_pipe;
    wire acc_busy = (|acc_pipe) || (m_valid && m_is_dot);
    wire hz_acc = x_valid && wait_acc && acc_busy;

    wire hz_stretch = m_valid && m_left;
    // A fold runs for NPART*ALAT cycles and the instruction that asked for it
    // waits: it is once per reduction, against a kernel of thousands of cycles.
    // A zero or a seed sweeps NPART entries of a one-write-port memory, so its
    // instruction waits for the sweep the way a fold waits for the fold.
    assign hz_fold = f_rd_hold || f_sw_hold;

    // A FLOAT ACCUMULATE IS STILL IN FLIGHT FIFTEEN CYCLES AFTER IT RETIRES.
    // Folding before it lands drops it AND captures it as a fold step, because
    // the fold gates the partial writes off. `vfmacc` is deliberately not on
    // the list: rotation is what lets one issue every cycle.
    wire wait_facc = is_facz || is_facwr || is_facrd;
    wire hz_facc = x_valid && wait_facc && (f_inflight || (m_valid && m_is_fma));
    assign stall = hz_raw | hz_wb | hz_spad | hz_acc | hz_stretch | hz_fold
                 | hz_facc;

    wire x_go = x_valid && !x_hold && !stall && !x_illegal && !x_misalign;

    always @(posedge clk) begin
        if (!resetn) begin
            m_valid <= 1'b0;
            m_left  <= 1'b0;
        end else if (hz_stretch) begin
            m_left <= 1'b0;
        end else if (hz_fold) begin
            // Hold the whole MEM stage: the folding instruction has not
            // finished and nothing may take its place.
            m_valid <= m_valid;
        end else begin
            m_valid     <= x_go;
            // A pipelined reduction needs the second cycle for the same reason
            // vmul does: its result is not there until the register has taken it.
            m_left      <= x_go && (is_mul
                                    || ((RED_PIPE != 0) && (SIMD > 2)
                                        && (is_rsum || is_rmax)));
            m_addr      <= x_addr;
            m_xdata     <= x_xdata;
            m_vd        <= rdf;
            m_rs1       <= r1f;
            m_et        <= et;
            m_alu_op    <= d_alu_op;
            m_cmp_sub   <= d_cmp_sub;
            m_alu_sat   <= d_alu_sat;
            m_sh_rot    <= d_sh_rot;
            m_sh_keep   <= d_sh_keep;
            m_sh_rmask  <= d_sh_rmask;
            m_el_mask   <= d_el_mask;
            m_sh_arith  <= d_sh_arith;
            m_sh_left   <= d_sh_left;
            m_sh_round  <= d_sh_round;
            m_is_vld    <= is_vld;
            m_is_vst    <= is_vst;
            m_is_dot    <= is_dot;
            m_is_dotn   <= is_dotn;
            m_is_accz   <= is_accz;
            m_is_accrd  <= is_accrd;
            m_is_accwr  <= is_accwr;
            m_is_mul    <= is_mul;
            m_is_splat  <= is_splat;
            m_is_extr   <= is_extr;
            m_is_rsum   <= is_rsum;
            m_is_rmax   <= is_rmax;
            m_is_prm    <= is_prm;
            m_is_fma    <= is_fma;
            m_is_fmsub  <= is_fmsub;
            m_is_facz   <= is_facz;
            m_is_facrd  <= is_facrd;
            m_is_facwr  <= is_facwr;
            m_is_frsum  <= is_frsum;
            m_is_ffold  <= is_ffold;
            m_wr_vreg_d <= wr_vreg;
            m_wr_sc_d   <= wr_sc;
            m_prm_op4   <= op4;
            m_prm_idx   <= ix3;
            m_lane      <= r2f[LAW-1:0];
        end
    end

    // ---- the vector register file -----------------------------------------
    wire [VW-1:0] v1, v2;
    wire [VW-1:0] vwdata;

    wire            vrf_we;
    wire [4:0]      vrf_wa;
    wire [VW-1:0]   vrf_wd;

    khd_vregfile #(.VREGS(VREGS), .VW(VW), .PRIM(VREG_PRIM)) u_vrf (
        .clk(clk),
        .ra_en(!stall && !x_hold),
        .ra1(p1f[VAW-1:0]), .ra2(r2f[VAW-1:0]),
        .rd1(v1), .rd2(v2),
        .we(vrf_we), .wa(vrf_wa[VAW-1:0]), .wd(vrf_wd)
    );

    generate
    if (WB_STAGE == 0) begin : g_wr_mem
        assign vrf_we = m_wr_vreg;
        assign vrf_wa = m_vd;
        assign vrf_wd = vwdata;
        assign hz_wb  = 1'b0;
    end else begin : g_wr_wb
        reg           w_v;
        reg [4:0]     w_vd;
        reg [VW-1:0]  w_d;
        always @(posedge clk) begin
            if (!resetn) w_v <= 1'b0;
            else begin
                w_v  <= m_wr_vreg;
                w_vd <= m_vd;
                w_d  <= vwdata;
            end
        end
        assign vrf_we = w_v;
        assign vrf_wa = w_vd;
        assign vrf_wd = w_d;
        assign hz_wb  = x_valid && w_v
                      && ((use_p1 && (p1f == w_vd)) || (use_p2 && (r2f == w_vd)));
    end
    endgenerate

    // ---- the vector scratchpad --------------------------------------------
    // A load presents its row in EX so the data is out in MEM; a store presents
    // it in MEM, because that is when its data leaves the register file. The
    // read enable is a real signal, not a constant: left at 1 the port reads
    // whatever row the EX adder produced for a scalar instruction, and every
    // NoC write would look like a cross-port collision.
    wire [RAW-1:0] ld_row = x_addr[RAW+$clog2(VBYTES)-1 -: RAW];
    wire [RAW-1:0] st_row = m_addr[RAW+$clog2(VBYTES)-1 -: RAW];
    wire           sp_we  = m_complete && m_is_vst;
    wire           sp_en  = (x_valid && is_vld) || sp_we || sc_st_valid;
    wire [VW-1:0]  sp_rd;

    // The scalar store's row and its bank: the same split khd_vspad makes, at
    // byte granularity because this address came from the EX adder.
    wire [RAW-1:0] sc_row  = sc_st_addr[RAW+$clog2(VBYTES)-1 -: RAW];
    wire [LAW-1:0] sc_bank = sc_st_addr[2 +: LAW];

    // Per-bank write enables: every bank for a `vst`, one bank's bytes for a
    // scalar store. The two cannot coincide -- they are different instructions
    // and only one is in MEM.
    wire [4*SIMD-1:0] sp_we_b;
    genvar B;
    generate
    for (B = 0; B < SIMD; B = B + 1) begin : g_bwe
        assign sp_we_b[4*B +: 4] =
            sp_we ? 4'hF
          : (sc_st_valid && (sc_bank == B[LAW-1:0])) ? sc_st_be : 4'd0;
    end
    endgenerate

    khd_vspad #(.SIMD(SIMD), .ENTRIES(VSPAD_ENTRIES), .MEM_PRIM(MEM_PRIM))
    u_vspad (
        .clk(clk),
        .a_en(noc_en),
        .a_we(noc_we),
        .a_word(noc_word),
        .a_wdata(noc_wdata),
        .b_en(sp_en),
        .b_row(sp_we ? st_row : sc_st_valid ? sc_row : ld_row),
        .b_we(sp_we_b),
        .b_wdata(sp_we ? v1 : {SIMD{sc_st_data}}),
        .b_rdata(sp_rd)
    );

    // ---- the lane array ---------------------------------------------------
    // Gated, and a vmul fires it only on its FIRST cycle: the second cycle is
    // there to read the registered products, and re-asserting would recompute
    // them from operands the stall is holding anyway.
    wire mul_en = m_valid && (m_is_dot || (m_is_mul && m_left));

    wire [VW-1:0]      lane_y;
    wire [VW-1:0]      lane_mul_lo;
    wire [34*SIMD-1:0] lane_dot;

    genvar L;
    generate
    for (L = 0; L < SIMD; L = L + 1) begin : g_lane
        khd_lane #(.MULS(MULS), .HAS_SHIFT(HAS_SHIFT), .USE_DSP(USE_DSP)) u_lane (
            .clk(clk),
            .a(v1[32*L +: 32]), .b(v2[32*L +: 32]), .et(m_et),
            .alu_op(m_alu_op), .cmp_sub(m_cmp_sub), .alu_sat(m_alu_sat),
            .sh_rot(m_sh_rot), .sh_keep(m_sh_keep), .sh_rmask(m_sh_rmask),
            .sh_arith(m_sh_arith), .sh_left(m_sh_left), .sh_round(m_sh_round),
            .el_mask(m_el_mask),
            .y(lane_y[32*L +: 32]),
            .mul_en(mul_en),
            .dot_sum(lane_dot[34*L +: 34]),
            .mul_lo(lane_mul_lo[32*L +: 32])
        );
    end
    endgenerate

    // ---- the accumulators --------------------------------------------------
    // A flat array rather than the DSP's own P register, so NACC is a parameter
    // and `vaccwr` (seeding an accumulation with a bias vector) is a plain
    // write. The recurrence is one cycle, which is what lets vdot issue at II=1.
    reg  [31:0] acc [0:NACC*SIMD-1];
    reg  [AAW-1:0] acc_idx [0:1];
    reg  [1:0]  acc_neg;

    wire [AAW-1:0] a_now = acc_idx[1];
    wire           a_neg = acc_neg[1];

    integer ai;
    always @(posedge clk) begin
        if (!resetn) begin
            acc_pipe <= 2'd0;
        end else begin
            acc_pipe   <= {acc_pipe[0], (m_complete && m_is_dot)};
            acc_idx[0] <= m_vd[AAW-1:0];
            acc_idx[1] <= acc_idx[0];
            acc_neg    <= {acc_neg[0], m_is_dotn};

            if (m_complete && m_is_accz)
                for (ai = 0; ai < SIMD; ai = ai + 1)
                    acc[m_vd[AAW-1:0] * SIMD + ai] <= 32'd0;
            else if (m_complete && m_is_accwr)
                for (ai = 0; ai < SIMD; ai = ai + 1)
                    acc[m_vd[AAW-1:0] * SIMD + ai] <= v1[32*ai +: 32];
            else if (acc_pipe[1])
                for (ai = 0; ai < SIMD; ai = ai + 1)
                    acc[a_now * SIMD + ai] <=
                        a_neg ? (acc[a_now * SIMD + ai] - lane_dot[34*ai +: 32])
                              : (acc[a_now * SIMD + ai] + lane_dot[34*ai +: 32]);
        end
    end

    wire [VW-1:0] acc_rd;
    generate
    for (L = 0; L < SIMD; L = L + 1) begin : g_accrd
        assign acc_rd[32*L +: 32] = acc[m_rs1[AAW-1:0] * SIMD + L];
    end
    endgenerate

    // ---- the float tier ----------------------------------------------------
    // 2*SIMD FP16 elements, one lane each, accumulating into NPART rotating
    // partials. The rotation is what makes II = 1 possible over a 15-deep lane
    // and it is architectural: NPART changes the answers.
    generate
    if (HAS_F16 != 0) begin : g_f16
        localparam integer SW = (FSLOTS > 1) ? $clog2(FSLOTS) : 1;
        localparam [1:0] ST_IDLE = 2'd0, ST_FILL = 2'd1,
                         ST_RUN  = 2'd2, ST_DONE = 2'd3;

        wire [24*FSLOTS-1:0] part_rd, lane_out;
        // Every lane is the same depth, so one valid speaks for all of them.
        wire [FSLOTS-1:0]    lane_ov;
        wire                 lane_ovld = lane_ov[0];
        reg  [24*FSLOTS-1:0] total, seed_r;
        reg  [16*FSLOTS-1:0] packed_f16;

        wire acc_fire = m_complete && m_is_fma;

        // ---- vfaccz / vfaccwr : build the seed word, then sweep -------------
        // ONE CONVERTER, WALKED, NOT FSLOTS OF THEM: sixteen parallel ones
        // measured 720 LUT for an instruction that runs once per kernel. The
        // word is registered because the sweep writes all slots at once.
        reg [1:0]    sw_st;
        reg [SW-1:0] sd_k;
        reg          do_z, do_s;

        wire [23:0] sd_e8;
        vec_cvt_f16_to_e8 u_sd (.f16(v1[16*sd_k +: 16]), .e8(sd_e8));

        assign f_sw_hold = m_valid && (m_is_facz || m_is_facwr)
                        && (sw_st != ST_DONE);

        always @(posedge clk) begin
            if (!resetn) begin
                sw_st <= ST_IDLE; do_z <= 1'b0; do_s <= 1'b0;
            end else begin
                do_z <= 1'b0;
                do_s <= 1'b0;
                case (sw_st)
                    ST_IDLE: if (m_valid && m_is_facz) begin
                                 do_z  <= 1'b1;
                                 sw_st <= ST_RUN;
                             end else if (m_valid && m_is_facwr) begin
                                 sd_k  <= {SW{1'b0}};
                                 sw_st <= ST_FILL;
                             end
                    ST_FILL: begin
                                 seed_r[24*sd_k +: 24] <= sd_e8;
                                 if (sd_k == (FSLOTS-1)) begin
                                     do_s  <= 1'b1;
                                     sw_st <= ST_RUN;
                                 end else sd_k <= sd_k + 1'b1;
                             end
                    // `busy_sweep` is low on the cycle the pulse is issued, so
                    // the pulse itself has to hold this state.
                    ST_RUN:  if (!f_sweep && !do_z && !do_s) sw_st <= ST_DONE;
                    default: sw_st <= ST_IDLE;
                endcase
            end
        end

        // ---- vfaccrd : fold the partials, then pack to FP16 -----------------
        // E8M15 -> FP16 carries a 48-bit subnormal shifter: 161 LUT each, 2,576
        // hung on the end of a 256-cycle instruction.
        reg [1:0]    rd_st;
        reg [SW-1:0] pk_k;
        wire         folding = (rd_st == ST_RUN);
        wire         fold_go = (rd_st == ST_IDLE) && m_valid && m_is_ffold;

        wire [15:0] pk_f16;
        vec_cvt_e8_to_f16 u_pk (.e8(total[24*pk_k +: 24]), .f16(pk_f16));

        assign f_rd_hold  = m_valid && m_is_ffold && (rd_st != ST_DONE);
        assign facc_rd_v  = packed_f16;

        always @(posedge clk) begin
            if (!resetn) rd_st <= ST_IDLE;
            else case (rd_st)
                ST_IDLE: if (fold_go) rd_st <= ST_RUN;
                ST_RUN:  if (f_done) begin
                             rd_st <= ST_FILL;
                             pk_k  <= {SW{1'b0}};
                         end
                ST_FILL: begin
                             packed_f16[16*pk_k +: 16] <= pk_f16;
                             if (pk_k == (FSLOTS-1)) rd_st <= ST_DONE;
                             else pk_k <= pk_k + 1'b1;
                         end
                default: rd_st <= ST_IDLE;
            endcase
        end

        // The lane's depth, as a shadow: `wait_facc` reads it in EX.
        reg [F16_ALAT-1:0] fpipe;
        always @(posedge clk) begin
            if (!resetn) fpipe <= {F16_ALAT{1'b0}};
            else         fpipe <= {fpipe[F16_ALAT-2:0], acc_fire};
        end
        assign f_inflight = |fpipe;

        khd_ffold #(.NPART(NPART), .ALAT(F16_ALAT)) u_ffold (
            .clk(clk), .resetn(resetn),
            .start(fold_go), .busy(f_busy), .done(f_done),
            .part_idx(f_idx), .iss_valid(f_iss), .iss_raw(f_raw)
        );

        genvar S;
        for (S = 0; S < FSLOTS; S = S + 1) begin : g_flane
            // A fold's addend is the running total; an accumulate's is the
            // partial the counter selected.
            wire [23:0] c_sel = folding ? total[24*S +: 24]
                                        : part_rd[24*S +: 24];
            khd_f16_lane #(.PIPE_MUX(1), .MODEL(F16_MODEL)) u_fl (
                .clk(clk), .rst(!resetn),
                .in_valid(acc_fire | f_iss),
                .a(v1[16*S +: 16]),
                .b(m_is_fmsub ? (v2[16*S +: 16] ^ 16'h8000) : v2[16*S +: 16]),
                .c(c_sel),
                .raw_e8(f_raw), .a_e8(fold_part_v[24*S +: 24]),
                .out_valid(lane_ov[S]),
                .out(lane_out[24*S +: 24])
            );
        end

        always @(posedge clk)
            if (fold_go)                   total <= {(24*FSLOTS){1'b0}};
            else if (folding && lane_ovld) total <= lane_out;

        khd_facc #(.SLOTS(FSLOTS), .NACC(NACC), .NPART(NPART), .ALAT(F16_ALAT))
        u_facc (
            .clk(clk), .resetn(resetn),
            .acc_valid(acc_fire), .acc_sel(m_vd[AAW-1:0]),
            .rd_part(part_rd), .rd_idx(),
            // A fold's results are NOT accumulates and must not land in the
            // partials being read.
            .wb_valid(lane_ovld && !folding), .wb_data(lane_out),
            .do_zero(do_z), .do_seed(do_s),
            .ctl_sel(m_vd[AAW-1:0]), .seed_data(seed_r),
            .fold_sel(m_rs1[AAW-1:0]), .fold_idx(f_idx),
            .fold_part(fold_part_v), .busy_sweep(f_sweep)
        );
    end else begin : g_no_f16
        assign facc_rd_v   = {VW{1'b0}};
        assign f_busy = 1'b0; assign f_done = 1'b0; assign f_sweep = 1'b0;
        assign f_iss  = 1'b0; assign f_raw  = 1'b0;
        assign f_sw_hold = 1'b0; assign f_rd_hold = 1'b0;
        assign f_inflight = 1'b0;
        assign f_idx  = {(NPART>1 ? $clog2(NPART) : 1){1'b0}};
        assign fold_part_v = {(24*FSLOTS){1'b0}};
    end
    endgenerate

    // ---- the cross-lane network -------------------------------------------
    wire [VW-1:0] perm_y;
    khd_perm #(.SIMD(SIMD), .HAS_PERM(HAS_PERM)) u_perm (
        .v1(v1), .v2(v2), .op4(m_prm_op4), .idx(m_prm_idx), .y(perm_y)
    );

    // Reductions: a tree, not a loop -- a chain that carries a value between
    // iterations synthesises as exactly that chain, which cost the accumulator
    // ~68 MHz once (mx_fpacc.v).
    wire [31:0] red_sum, red_max;
    khd_reduce #(.SIMD(SIMD), .PIPE(RED_PIPE)) u_red (
        .clk(clk), .v(v1), .sum(red_sum), .max(red_max)
    );

    wire [31:0] extr = v1[32 * m_lane +: 32];

    // ---- the result muxes --------------------------------------------------
    // Every select is a registered decode bit, so this is a mux and not a mux
    // with a decoder in front of it.
    reg [VW-1:0] vres;
    always @(*) begin
        if (m_is_vld)        vres = sp_rd;
        else if (m_is_facrd) vres = facc_rd_v;
        else if (m_is_accrd) vres = acc_rd;
        else if (m_is_splat) vres = {SIMD{m_xdata}};
        else if (m_is_prm)   vres = perm_y;
        else if (m_is_mul)   vres = lane_mul_lo;
        else                 vres = lane_y;
    end
    assign vwdata = vres;

    // The probe is the FILE'S write port, not the MEM stage's intent, so it
    // reports the same stream at either WB_STAGE -- one cycle later at 1, and
    // still in program order, which is what the bench compares.
    assign dbg_wr_valid = vrf_we;
    assign dbg_wr_vd    = vrf_wa;
    assign dbg_wr_data  = vrf_wd;

    always @(posedge clk) begin
        if (!resetn) w_sc_valid <= 1'b0;
        else begin
            w_sc_valid <= m_wr_sc;
            w_sc <= m_is_extr ? extr : m_is_rsum ? red_sum : red_max;
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) if (resetn) begin
        if (x_valid && x_illegal)
            $display("%0t khd_unit: instruction %08h is not built in this configuration (SIMD %0d, VREGS %0d, NACC %0d, MULS %0d, shift %0d, perm %0d)",
                     $time, x_instr, SIMD, VREGS, NACC, MULS, HAS_SHIFT, HAS_PERM);
        if (x_valid && x_misalign)
            $display("%0t khd_unit: vector address %08h is not a multiple of %0d bytes",
                     $time, x_addr, VBYTES);
    end
`endif

endmodule

`default_nettype wire

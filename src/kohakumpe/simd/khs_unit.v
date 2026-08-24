// khs_unit -- the SIMD extension: the vector register file, the lane array, the
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
//                                                  accumulate lands DOT_LAT
//                                                  later, in the background --
//                                                  2 in fabric, 4 down the DSP48
//
// `vdot` not stalling is the point of the accumulator: a stream of them
// accumulates correctly at one per cycle because each one's product reaches the
// accumulate stage in issue order. Only reading or disturbing an accumulator
// (`vaccrd`, `vaccz`, `vaccwr`) has to wait for that pipeline to drain.

`default_nettype none

module khs_unit #(
    parameter integer SIMD          = 8,        // 32-bit lanes; VW = 32*SIMD
    parameter integer VREGS         = 8,
    parameter integer NACC          = 2,
    parameter integer VSPAD_ENTRIES = 1024,
    parameter integer MULS          = 4,        // 4 = int8 dot at II=1
    parameter integer HAS_SHIFT     = 1,
    // Packed-shift units, against SIMD 32-bit lanes. 0 = one per lane, which is
    // the shifter inside every khs_lane and every build before this existed.
    parameter integer SHIFT_UNITS   = 0,
    parameter integer HAS_PERM      = 1,
    // Cross-lane units: how many 32-bit OUTPUT words the permute produces per
    // pass, against SIMD of them. 0 = one per word, which is every build before
    // the width was separable. Legal: 0,1,2,4,8,16 dividing SIMD.
    parameter integer PERM_UNITS    = 0,
    parameter integer DOT_DSP       = 0,
    // Float, on custom-1. 0 elaborates none of it and leaves the opcode major
    // unmapped, so a float instruction faults.
    //
    // THE GROUPS ARE SEPARATE PARAMETERS because a SIMD PE is a CPU and the
    // groups are not equally fundamental. FALU -- mul, add, sub, fma, min, max,
    // compare -- is what every CPU SIMD ISA ships as its base, so it is on
    // wherever float is. The rest are additions, priced and enabled one at a
    // time, which is also what lets the eight SIMD PEs of a mesh carry
    // different feature sets instead of one global choice.
    parameter integer HAS_FLOAT       = 0,
    parameter integer HAS_FALU        = 1,   // the base; 0 only to measure it
    // float <-> int32, f16 <-> f32. NOT BUILT: the decode accepts it, sets
    // `wr_vreg`, and the result mux has no branch for it -- so `vfcvt` writes
    // the INTEGER lane's output. There is no converter instantiated anywhere in
    // this file. Default 0, and the guard below refuses 1, so the group faults
    // rather than returning a plausible wrong answer.
    parameter integer HAS_FCVT        = 0,
    // WHICH MEMORY FORMATS THE FLOAT TIER CARRIES; the compute format is E8M15
    // either way, so this removes CONVERTERS AND WIDTH SELECTS and no arithmetic.
    parameter integer HAS_F16         = 1,
    parameter integer HAS_F32         = 1,
    // exp2, log2, rcp, rsqrt -- A UNIT COUNT, NOT A BOOLEAN. 0 builds none and
    // the opcode faults; N builds N seed-capable units out of FLOAT_LANES and a
    // seed walks 2*SIMD/N passes where an FMA walks 2*SIMD/FLOAT_LANES. Every
    // other unit is a plain FMA unit, one DSP48E2 and 1.5 BRAM cheaper.
    // A ROW MEASURED AT THE OLD BOOLEAN IS NOT COMPARABLE: HAS_FSFU=1 meant
    // "every lane seed-capable", which is FSFU_UNITS = FLOAT_LANES here.
    parameter integer FSFU_UNITS      = 0,
    // The rotating accumulator and its fold. OFF by default: it is the SIMD
    // PE's extra, justified by vertex transform, float dot and long reductions,
    // and a shader doing elementwise colour work pays nothing for it.
    parameter integer HAS_FACC        = 0,
    // Rotating partials per slot, and the lane's latency. NPART must EXCEED
    // ALAT or a partial is re-read before its write returns -- and the count is
    // architectural, because float addition does not associate.
    parameter integer NPART         = 16,
    parameter integer FLOAT_ALAT      = 15,
    // HOW MANY FLOAT LANES ARE BUILT, against 2*SIMD elements per register. 0
    // means one lane per element, which is what every build did before this
    // parameter existed and is what keeps them bit-identical. Must divide
    // 2*SIMD, and 2*SIMD/FLOAT_LANES must divide NPART.
    //
    // **0 IS "NOT BUILT"**, the same spelling the SIMT PE's `FLANES` uses. It
    // used to mean "one unit per element"; a build wanting that now says
    // FLOAT_LANES = 2*SIMD, and asking for a float group with no units is
    // refused at elaboration rather than silently given the widest tier.
    parameter integer FLOAT_LANES     = 0,
    // 1 swaps DSP48E2 for vec_dsp's behavioural model. Defaults to the
    // SYNTHESIS value, so a bench that forgets it fails to elaborate rather
    // than quietly measuring a different multiplier.
    parameter integer FLOAT_MODEL     = 0,
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
    input  wire        x_valid,     // a KohakuSIMD instruction is in EX
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

    // Declared here because the DECODE reads FLANES: xvlog rejects the
    // use-before-declare that synthesis accepts silently. NARROW_SLOTS is FP16
    // elements per vector; FP32 is half of them over FLANES/2 lanes, which is
    // what keeps PASSES one number in both formats. See the float tier below.
    localparam integer NARROW_SLOTS = 2 * SIMD;
    // 0 IS "NOT BUILT", exactly as the SIMT PE spells it. It used to mean "one
    // unit per element", so the same 0 meant the WIDEST possible float tier
    // here and NO float tier next door -- a caller that forgot the parameter
    // got opposite machines from the two cores.
    localparam integer FLANES   = FLOAT_LANES;
    localparam integer FL_ON    = (FLANES != 0) ? 1 : 0;
    // Stands in for FLANES wherever a width or a division needs a nonzero, in
    // the branch that is then not elaborated: a zero-width wire and a divide by
    // zero are elaboration errors, not trimmed ones.
    localparam integer FLANES_W = FL_ON ? FLANES : 1;
    localparam integer PASSES = NARROW_SLOTS / FLANES_W;
    // The seed units, and 0 means "no seeds" rather than "all of them": with
    // none built the walk is one number and the decode refuses the opcode.
    localparam integer SEED_U = (FSFU_UNITS != 0) ? FSFU_UNITS : FLANES_W;
    // The permute's own width and its walk.
    localparam integer PU     = (PERM_UNITS == 0) ? SIMD : PERM_UNITS;
    localparam integer PPASS  = SIMD / PU;
    localparam integer PPW    = (PPASS > 1) ? $clog2(PPASS) : 1;
    // The shifter's own width and walk. SHU == SIMD keeps it inside the lane.
    localparam integer SHU    = (SHIFT_UNITS == 0) ? SIMD : SHIFT_UNITS;
    localparam integer SHPASS = SIMD / SHU;
    localparam integer SHPW   = (SHPASS > 1) ? $clog2(SHPASS) : 1;
    localparam integer SH_IN_LANE = (SHPASS == 1) ? HAS_SHIFT : 0;
    localparam integer FPW    = (NPART > 1) ? $clog2(NPART) : 1;

`include "khs_isa.vh"

    localparam [2:0] OP_ADD = 3'd0, OP_MIN = 3'd1, OP_MAX = 3'd2, OP_AND = 3'd3;
    localparam [2:0] OP_OR = 3'd4, OP_XOR = 3'd5, OP_ANDN = 3'd6, OP_SH = 3'd7;

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
    wire is_f_maj = (HAS_FLOAT != 0) && (x_instr[6:0] == KHF_OPCODE);
    wire is_i_maj = !is_f_maj;

    wire is_vld = is_i_maj && (f3 == KHS_F3_VLD);
    wire is_vst = is_i_maj && (f3 == KHS_F3_VST);
    wire is_int = is_i_maj && (f3 == KHS_F3_VINT);
    wire is_bit = is_i_maj && (f3 == KHS_F3_VBIT);
    wire is_shi = is_i_maj && (f3 == KHS_F3_VSHI);
    wire is_mac = is_i_maj && (f3 == KHS_F3_VMAC);
    wire is_mov = is_i_maj && (f3 == KHS_F3_VMOV);
    wire is_prm = is_i_maj && (f3 == KHS_F3_VPRM);

    // ---- the accumulator group, on custom-1. OFF unless HAS_FACC. ----
    wire is_fmac  = is_f_maj && (HAS_FACC != 0) && FL_ON && (f3 == KHF_F3_FMAC);
    wire is_fma   = is_fmac && ((op5 == KHF_FMAC_FMACC) || (op5 == KHF_FMAC_FMSAC));
    wire is_fmsub = is_fmac && (op5 == KHF_FMAC_FMSAC);
    wire is_facz  = is_fmac && (op5 == KHF_FMAC_FACCZ);
    wire is_facrd = is_fmac && (op5 == KHF_FMAC_FACCRD);
    wire is_facwr = is_fmac && (op5 == KHF_FMAC_FACCWR);
    // Reading an accumulator means folding it first, and that runs for
    // NPART*ALAT cycles -- so both forms stall until the fold reports done.
    wire is_ffold = is_facrd;
    // WHICH INPUT FORMAT, and nothing else: the compute format is E8M15 either
    // way, so this reaches the conversion at the edge and no arithmetic below it.
    // AT ONE FORMAT IT IS A CONSTANT, not a registered decode bit that constant
    // propagation might or might not chase through the MEM register.
    localparam integer FMT_DUAL = ((HAS_F16 != 0) && (HAS_F32 != 0)) ? 1 : 0;
    wire is_f32   = FMT_DUAL ? (is_f_maj && (et == KHF_FT_F32))
                             : (HAS_F32 != 0);

    // ---- the elementwise groups ------------------------------------------
    // NO UNITS MEANS NO GROUP. `FL_ON` is what makes `FLOAT_LANES = 0` fault a
    // float instruction rather than build a tier the caller did not ask for.
    wire is_falu = is_f_maj && (HAS_FALU != 0) && FL_ON && (f3 == KHF_F3_FALU);
    wire is_fsfu = is_f_maj && (FSFU_UNITS != 0) && FL_ON && (f3 == KHF_F3_FSFU);
    wire is_fcvt = is_f_maj && (HAS_FCVT != 0) && (f3 == KHF_F3_FCVT);
    // Everything that walks the elementwise lanes. FCVT is not on the list: it
    // is a conversion at the edge, with no lane behind it.
    wire is_fel  = is_falu || is_fsfu;
    wire is_fcmp = is_falu && ((op5 == KHF_FALU_FCMPLT)
                           || (op5 == KHF_FALU_FCMPGT)
                           || (op5 == KHF_FALU_FCMPEQ));
    // `vfma` is the one form that READS its destination, which is what the
    // third register-file port exists for.
    wire is_ffma = is_falu && (op5 == KHF_FALU_FMA);

    // The lane's operation. vec_alu routes the operands itself once it knows
    // this, so the decode ends here rather than in an operand network.
    reg [4:0] d_fop;
    always @(*) begin
        if (is_fsfu) begin
            case (op5)
                KHF_FSFU_FEXP2: d_fop = KHS_FOP_EXP2;
                KHF_FSFU_FLOG2: d_fop = KHS_FOP_LOG2;
                KHF_FSFU_FRCP:  d_fop = KHS_FOP_INV;
                default:        d_fop = KHS_FOP_RSQRT;
            endcase
        end
        else begin
            case (op5)
                KHF_FALU_FMUL:   d_fop = KHS_FOP_MUL;
                KHF_FALU_FADD:   d_fop = KHS_FOP_ADD;
                KHF_FALU_FSUB:   d_fop = KHS_FOP_SUB;
                KHF_FALU_FMIN:   d_fop = KHS_FOP_MIN;
                KHF_FALU_FMAX:   d_fop = KHS_FOP_MAX;
                KHF_FALU_FCMPLT: d_fop = KHS_FOP_CMPLT;
                KHF_FALU_FCMPGT: d_fop = KHS_FOP_CMPGT;
                KHF_FALU_FCMPEQ: d_fop = KHS_FOP_CMPEQ;
                default:         d_fop = KHS_FOP_FMA;
            endcase
        end
    end

    wire [1:0] d_fcvt_op = op5[1:0];

    wire is_dot   = is_mac && ((op5 == KHS_MAC_DOT) || (op5 == KHS_MAC_DOTN));
    wire is_dotn  = is_mac && (op5 == KHS_MAC_DOTN);
    wire is_accz  = is_mac && (op5 == KHS_MAC_ACCZ);
    wire is_accrd = is_mac && (op5 == KHS_MAC_ACCRD);
    wire is_accwr = is_mac && (op5 == KHS_MAC_ACCWR);
    wire is_mul   = is_int && (op5 == KHS_INT_MUL);
    wire is_splat = is_mov && (f7 == KHS_MOV_SPLAT);
    wire is_extr  = is_mov && (f7 == KHS_MOV_EXTR);
    wire is_rsum  = is_mov && (f7 == KHS_MOV_REDSUM);
    wire is_rmax  = is_mov && (f7 == KHS_MOV_REDMAX);

    // A store's DATA register rides in the rd field, so read port 1 serves it.
    wire [4:0] p1f = is_vst ? rdf : r1f;

    wire use_p1 = is_vst || is_int || is_bit || is_shi || is_prm
                || (is_mac && (is_dot || is_accwr))
                || (is_mov && !is_splat)
                || is_fma || is_facwr
                || is_fel || is_fcvt;
    wire use_p2 = is_int || is_bit || is_dot
                || (is_prm && (op4 <= KHS_PRM_PACK_S32))
                || is_fma
                || is_falu;
    // vfma reads its destination as the addend. One more port, and it is a
    // parameter on the file rather than a fixture: an integer-only build must
    // not pay a third mirror for an instruction it does not have.
    wire use_p3 = is_ffma;

    // FCVT completes in MEM as an ALU op does -- vec_cvt is combinational, so
    // there is no lane behind it and nothing to wait for. FALU and FSFU do NOT
    // appear here: they retire through the shadow below, fifteen cycles later.
    wire wr_vreg = is_vld || is_int || is_bit || is_shi || is_prm
                 || is_accrd || is_splat || is_facrd || is_fcvt;
    wire wr_acc  = is_dot || is_accz || is_accwr;
    wire wr_sc   = is_extr || is_rsum || is_rmax;
    // What has to wait for an accumulate in flight, and `vdot` is NOT on the
    // list: see the hazard section below.
    wire wait_acc = is_accz || is_accwr || is_accrd;

    reg [2:0] d_alu_op;
    always @(*) begin
        if (is_bit) begin
            case (f7)
                KHS_BIT_AND:  d_alu_op = OP_AND;
                KHS_BIT_OR:   d_alu_op = OP_OR;
                KHS_BIT_XOR:  d_alu_op = OP_XOR;
                default:      d_alu_op = OP_ANDN;
            endcase
        end
        else if (is_shi) begin
            d_alu_op = OP_SH;
        end
        else if (is_int && (op5 == KHS_INT_MIN)) begin
            d_alu_op = OP_MIN;
        end
        else if (is_int && (op5 == KHS_INT_MAX)) begin
            d_alu_op = OP_MAX;
        end
        else begin
            d_alu_op = OP_ADD;
        end
    end

    wire d_alu_sub = is_int && ((op5 == KHS_INT_SUB) || (op5 == KHS_INT_SSUB));
    wire d_alu_sat = is_int && ((op5 == KHS_INT_SADD) || (op5 == KHS_INT_SSUB));
    // Decided HERE, not in the lane: min and max need a - b for their compare,
    // and deriving that per lane put a LUT between the decode register and the
    // carry chain -- the first two levels of the binding path.
    wire d_cmp_sub = d_alu_sub || (d_alu_op == OP_MIN) || (d_alu_op == OP_MAX);

    // ---- the shift masks, built once here rather than SIMD times in MEM ----
    wire [4:0] d_sh_amt = (et == KHS_ET_S8)  ? {2'd0, r2f[2:0]}
                        : (et == KHS_ET_S16) ? {1'd0, r2f[3:0]} : r2f;
    wire d_sh_left  = is_shi && (op5 == KHS_SH_SLLI);
    wire d_sh_arith = is_shi && ((op5 == KHS_SH_SRAI) || (op5 == KHS_SH_SRARI));
    wire d_sh_round = is_shi && (op5 == KHS_SH_SRARI);

    wire [7:0]  kr8  = 8'hFF  >> d_sh_amt;
    wire [7:0]  kl8  = 8'hFF  << d_sh_amt;
    wire [15:0] kr16 = 16'hFFFF >> d_sh_amt;
    wire [15:0] kl16 = 16'hFFFF << d_sh_amt;
    wire [31:0] kr32 = 32'hFFFFFFFF >> d_sh_amt;
    wire [31:0] kl32 = 32'hFFFFFFFF << d_sh_amt;

    wire [31:0] keep_r = (et == KHS_ET_S8)  ? {4{kr8}}
                       : (et == KHS_ET_S16) ? {2{kr16}} : kr32;
    wire [31:0] keep_l = (et == KHS_ET_S8)  ? {4{kl8}}
                       : (et == KHS_ET_S16) ? {2{kl16}} : kl32;
    wire [31:0] d_sh_keep = d_sh_left ? keep_l : keep_r;

    wire [31:0] one_at = (d_sh_amt == 5'd0) ? 32'd0 : (32'd1 << (d_sh_amt - 5'd1));
    wire [31:0] d_sh_rmask = (et == KHS_ET_S8)  ? {4{one_at[7:0]}}
                           : (et == KHS_ET_S16) ? {2{one_at[15:0]}} : one_at;

    wire [4:0] d_sh_rot = d_sh_left ? (5'd0 - d_sh_amt) : d_sh_amt;

    // Each element's MSB: the SWAR adder's mask. Built here and registered so
    // the lanes see it at the start of MEM rather than through a mux.
    wire [31:0] d_el_mask = (et == KHS_ET_S8)  ? 32'h8080_8080
                          : (et == KHS_ET_S16) ? 32'h8000_8000
                                               : 32'h8000_0000;

    // ---- what this configuration refuses ---------------------------------
    // An encoding a build does not carry must FAULT, not compute something
    // plausible: that is what makes "a variant without a tier lacks those
    // encodings" a checkable statement rather than a description.
    wire bad_reg = (use_p1 && (p1f >= VREGS)) || (use_p2 && (r2f >= VREGS))
                 || (use_p3 && (rdf >= VREGS))
                 || (wr_vreg && (rdf >= VREGS))
                 || (is_fel && (rdf >= VREGS))
                 || (wr_acc && (rdf >= NACC)) || (is_accrd && (r1f >= NACC))
                 || ((is_fma || is_facz || is_facwr) && (rdf >= NACC))
                 || (is_ffold && (r1f >= NACC));
    // Custom-1 without the float tier is an unmapped opcode major, and any
    // float group this build does not implement is unmapped within it.
    wire bad_flt = (HAS_FLOAT == 0) && (x_instr[6:0] == KHF_OPCODE);
    // `vfredsum` is NOT BUILT YET and therefore faults. The fold combines the
    // partials WITHIN each slot; crossing the slots is a second pass that does
    // not exist, and returning slot 0 alone would be a plausible wrong answer
    // -- which is the one thing a refusal is for.
    // A float group this build does not carry is unmapped WITHIN custom-1, so a
    // program using it faults instead of landing on a neighbouring decode.
    // `vfredsum` is still not built: the fold combines the partials within each
    // slot, and crossing the slots is a second pass that does not exist.
    wire bad_fgrp = is_f_maj && !(is_fma || is_facz || is_facrd || is_facwr
                                  || is_falu || is_fsfu || is_fcvt);
    // BOTH WIDTHS, IN EVERY GROUP. The only remaining shape constraint is the
    // ACCUMULATOR's: it packs FP16 two per 32-bit slot and gives FP32 the even
    // slot alone, so one float lane has nowhere to put a wide element.
    // Elementwise carries no such rule -- a lane takes a whole element either
    // way -- which is why FALU has no FLANES condition here.
    // A FORMAT THE BUILD DOES NOT CARRY IS AN UNMAPPED ENCODING, not a silent
    // reinterpretation of the other one -- the same rule the absent groups obey.
    wire bad_fet  = is_f_maj && !is_facz
                  && (((et != KHF_FT_F16) && (et != KHF_FT_F32))
                   || ((et == KHF_FT_F16) && (HAS_F16 == 0))
                   || ((et == KHF_FT_F32) && (HAS_F32 == 0)));
    wire bad_facc = is_fmac && !is_facz
                  && (et == KHF_FT_F32) && (FLANES < 2);
    // A LANE INDEX WIDER THAN THE BUILD MUST FAULT, NOT ALIAS. `m_lane` is
    // r2f[LAW-1:0], so `vextr x, v, 5` on a 4-lane build silently read lane 1 --
    // the same binary answering differently on a narrower machine, which is
    // exactly what "the ISA knows no width" has to rule out. The ENCODING still
    // allows 0..31; what a build carries is what this refuses.
    wire bad_lane = is_extr && (r2f >= SIMD);
    wire bad_et  = (is_int || is_shi) && (et == 2'd3);
    // int8 needs FOUR products per 32-bit lane, for `vmul` exactly as much as
    // for `vdot`: a two-multiplier lane returns zero for the top two elements.
    wire bad_cfg = (is_shi && (HAS_SHIFT == 0))
                 || (is_prm && (HAS_PERM == 0))
                 // int8 at MULS < 4 is TWO PASSES now, not a fault: the operand
                 // width is the same and only the cycle count moves.
                 || (is_dot && (et >= KHS_ET_S32))
                 || (is_mul && (et >= KHS_ET_S32));
    wire bad_grp = !(is_vld || is_vst || is_int || is_bit || is_shi
                   || is_mac || is_mov || is_prm
                   || is_fma || is_facz || is_facrd || is_facwr
                   || is_falu || is_fsfu || is_fcvt);
    assign x_illegal = x_valid && (bad_reg || bad_et || bad_cfg || bad_grp
                                   || bad_flt || bad_fgrp || bad_fet
                                   || bad_facc || bad_lane);

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
    reg         m_is_ffold, m_is_f32_r;
    // A flop whose D is constant is not reliably folded away; this is.
    wire        m_is_f32 = FMT_DUAL ? m_is_f32_r : (HAS_F32 != 0);
    reg         m_is_falu, m_is_fsfu, m_is_fcvt, m_is_fcmp, m_is_fel;
    reg  [4:0]  m_fop;
    reg  [1:0]  m_fcvt_op;
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
    // rv_id.v carries the same note for the same reason. The SHAPE localparams
    // this tier turns on are at the top of the module, where the decode reads
    // them; separating FLANES from NARROW_SLOTS is what makes "8 int + 4 float"
    // expressible at all.
    wire [VW-1:0]        facc_rd_v;
    wire                 f_busy, f_done, f_iss, f_raw, f_sweep;
    wire                 f_sw_hold, f_rd_hold, f_inflight, f_pass_hold;
    wire [FPW-1:0]       f_idx;
    wire [24*NARROW_SLOTS-1:0] fold_part_v;

    // The elementwise group's retire path. A FALU result lands FLOAT_ALAT
    // cycles after its pass issued, so the instruction cannot write in MEM the
    // way an ALU op does. Three signals carry that:
    //   el_soon   one cycle of warning, so the MEM stage is empty when the
    //             result arrives and the write port is taken by MUX rather
    //             than by arbitration -- kht_core's `f_soon`, same trick
    //   el_wr     the retiring write itself
    //   el_busy   one bit per vector register, set at issue and cleared at the
    //             last pass's write; anything touching a busy register stalls
    // The permute's walk holds MEM the same way the float fold does, and it is
    // declared here because `hz_fold` reads it long before the block below.
    wire                 prm_hold, shf_hold, mul_hold;
    // TWO PASSES FOR int8 WHEN THE LANE HAS TWO MULTIPLIERS, one otherwise. A
    // RUNTIME condition, not a build one: the same build does int16 in one pass.
    wire                 mul_two = (MULS < 4) && (m_et == KHS_ET_S8);
    wire                 el_soon, el_wr, el_hold, el_inflight;
    wire [4:0]           el_wa;
    wire [VW-1:0]        el_wdata;
    wire [VREGS-1:0]     el_busy;

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
    // THE DOT'S LATENCY IS A CONTRACT WITH khs_lane, which derives the same
    // number from the same two parameters. A PCIN cascade costs a cycle per
    // hop, so four terms retire in four; the fabric adder tree retires in two.
    localparam integer DOT_LAT = ((DOT_DSP != 0) && (MULS >= 4)) ? 4 : 2;

    reg [DOT_LAT-1:0] acc_pipe;
    wire acc_busy = (|acc_pipe) || (m_valid && m_is_dot);
    wire hz_acc = x_valid && wait_acc && acc_busy;

    wire hz_stretch = m_valid && m_left;
    // A fold runs for NPART*ALAT cycles and the instruction that asked for it
    // waits: it is once per reduction, against a kernel of thousands of cycles.
    // A zero or a seed sweeps NPART entries of a one-write-port memory, so its
    // instruction waits for the sweep the way a fold waits for the fold.
    // A multi-pass `vfmacc` waits the same way: the MEM stage holds until every
    // pass has issued, so the instruction retires once rather than per pass.
    // `el_hold` BELONGS HERE AND NOT ONLY IN `stall`. This is what holds the MEM
    // STAGE; `stall` only holds EX, so a multi-pass elementwise instruction left
    // MEM after its first pass, the walk never reached `iss_last`, the result
    // never retired and the bench waited forever for a write that was not coming.
    assign hz_fold = f_rd_hold || f_sw_hold || f_pass_hold || el_hold
                  || prm_hold || shf_hold || mul_hold;

    // A FLOAT ACCUMULATE IS STILL IN FLIGHT FIFTEEN CYCLES AFTER IT RETIRES.
    // Folding before it lands drops it AND captures it as a fold step, because
    // the fold gates the partial writes off. `vfmacc` is deliberately not on
    // the list: rotation is what lets one issue every cycle.
    wire wait_facc = is_facz || is_facwr || is_facrd;
    wire hz_facc = x_valid && wait_facc && (f_inflight || (m_valid && m_is_fma));

    // A REGISTER WITH A FLOAT IN FLIGHT IS NOT READABLE AND NOT WRITABLE. The
    // unit has one PC and no waves, so there is nothing to switch to while a
    // 15-deep result is outstanding -- the scoreboard is what replaces the SIMT
    // side's `fpend`. WAW is on the list as well as RAW: two writes to one
    // register would retire out of order, because a later short instruction
    // reaches MEM long before an earlier float leaves the lane.
    // The MEM-stage float is included EXPLICITLY: it sets its busy bit at the
    // end of this cycle, so a dependent instruction one behind it would read a
    // bit that is not set yet and sail past the hazard.
    wire [VREGS-1:0] el_busy_eff =
        el_busy | ((m_valid && m_is_fel)
                   ? ({{(VREGS-1){1'b0}}, 1'b1} << m_vd[VAW-1:0])
                   : {VREGS{1'b0}});
    // ONE ELEMENTWISE INSTRUCTION IN FLIGHT. `stage_r` below is a single
    // staging register: each pass places its FLANES results into it and the
    // whole register is written when the last pass lands. Two instructions in
    // flight share it, so the second one's passes overwrite the first's before
    // it retires -- measured as X in the upper elements and a write carrying
    // another instruction's value. The scoreboard alone does not catch this,
    // because it only blocks DEPENDENT instructions.
    //
    // The cost is real and is the next thing to fix: II is ALAT + passes, about
    // 19 cycles for FP16 at four lanes, so elementwise float is latency-bound
    // rather than throughput-bound until the writeback becomes per-pass with a
    // per-element write enable on the register file.
    wire hz_el = x_valid
               && ((use_p1 && el_busy_eff[p1f[VAW-1:0]])
                || (use_p2 && el_busy_eff[r2f[VAW-1:0]])
                || ((use_p3 || wr_vreg || is_fel)
                    && el_busy_eff[rdf[VAW-1:0]])
                || (is_fel && el_inflight));
    // `el_soon` empties MEM so the retiring float owns the write port.
    assign stall = hz_raw | hz_wb | hz_spad | hz_acc | hz_stretch | hz_fold
                 | hz_facc | hz_el | el_soon | el_hold;

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
            m_is_ffold  <= is_ffold;
            m_is_f32_r  <= is_f32;
            m_is_falu   <= is_falu;
            m_is_fsfu   <= is_fsfu;
            m_is_fcvt   <= is_fcvt;
            m_is_fcmp   <= is_fcmp;
            m_is_fel    <= is_fel;
            m_fop       <= d_fop;
            m_fcvt_op   <= d_fcvt_op;
            m_wr_vreg_d <= wr_vreg;
            m_wr_sc_d   <= wr_sc;
            m_prm_op4   <= op4;
            m_prm_idx   <= ix3;
            m_lane      <= r2f[LAW-1:0];
        end
    end

    // ---- the vector register file -----------------------------------------
    wire [VW-1:0] v1, v2, v3;
    wire [VW-1:0] vwdata;

    wire            vrf_we;
    wire [4:0]      vrf_wa;
    wire [VW-1:0]   vrf_wd;

    // The third port exists only for `vfma`, so it follows the group that
    // carries it rather than being built unconditionally.
    localparam integer VRF_RD3 = ((HAS_FLOAT != 0) && (HAS_FALU != 0) && FL_ON) ? 1 : 0;

    khs_vregfile #(.VREGS(VREGS), .VW(VW), .PRIM(VREG_PRIM), .RD3(VRF_RD3))
    u_vrf (
        .clk(clk),
        .ra_en(!stall && !x_hold),
        .ra1(p1f[VAW-1:0]), .ra2(r2f[VAW-1:0]), .ra3(rdf[VAW-1:0]),
        .rd1(v1), .rd2(v2), .rd3(v3),
        .we(vrf_we), .wa(vrf_wa[VAW-1:0]), .wd(vrf_wd)
    );

    generate
    if (WB_STAGE == 0) begin : g_wr_mem
        // THE RETIRING FLOAT WINS THE PORT, and it can because `el_soon` told
        // the core a cycle ago to empty MEM. `m_wr_vreg` is false on that cycle
        // by construction, so this is a mux and never an arbitration.
        assign vrf_we = el_wr ? 1'b1     : m_wr_vreg;
        assign vrf_wa = el_wr ? el_wa    : m_vd;
        assign vrf_wd = el_wr ? el_wdata : vwdata;
        assign hz_wb  = 1'b0;
    end else begin : g_wr_wb
        reg           w_v;
        reg [4:0]     w_vd;
        reg [VW-1:0]  w_d;
        always @(posedge clk) begin
            if (!resetn) begin
                w_v <= 1'b0;
            end
            else begin
                w_v  <= m_wr_vreg;
                w_vd <= m_vd;
                w_d  <= vwdata;
            end
        end
        // THE RETIRING FLOAT WINS THE PORT HERE TOO. `el_soon` gave two cycles
        // of warning at this WB_STAGE, so `w_v` is low on the cycle `el_wr`
        // fires and this stays a mux. Folding `el_wdata` into `vres` to reuse
        // that select chain measured -16 LUT and cost the write a cycle.
        assign vrf_we = el_wr ? 1'b1     : w_v;
        assign vrf_wa = el_wr ? el_wa    : w_vd;
        assign vrf_wd = el_wr ? el_wdata : w_d;
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

    // The scalar store's row and its bank: the same split khs_vspad makes, at
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

    khs_vspad #(.SIMD(SIMD), .ENTRIES(VSPAD_ENTRIES), .MEM_PRIM(MEM_PRIM))
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
    // THE MULTIPLY WALK. `np` passes issue, and `vmul` needs one cycle beyond
    // them to read the registered products -- which at MULS >= 4 is the stretch
    // `m_left` already provides, so nothing about that build changes.
    // A GENERATE, NOT A RUNTIME CONSTANT: folded to 0 at MULS >= 4 the counter
    // still reached `hz_fold`, and through it `stall`, for +346 LUT (15,741 ->
    // 16,087) on a walk that build can never take.
    wire mul_en, mul_mp, mul_cap;
    generate
    if (MULS >= 4) begin : g_mul_one
        // Byte-for-byte the expression this file had before the walk existed.
        assign mul_en   = m_valid && (m_is_dot || (m_is_mul && m_left));
        assign mul_hold = 1'b0;
        assign mul_mp   = 1'b0;
        assign mul_cap  = 1'b0;
    end else begin : g_mul_walk
        wire       mul_in = m_valid && (m_is_dot || m_is_mul);
        wire [1:0] mul_np = mul_two ? 2'd2 : 2'd1;
        wire [1:0] mul_nc = m_is_mul ? (mul_np + 2'd1) : mul_np;
        reg  [1:0] mc_q;
        assign mul_en   = mul_in && (mc_q < mul_np);
        assign mul_mp   = mc_q[0];
        assign mul_hold = mul_in && (mc_q < (mul_nc - 2'd1));
        assign mul_cap  = mul_in && m_is_mul && mul_two && (mc_q == 2'd1);

        // RESET ON COMPLETION: back-to-back multiplies keep `mul_in` high across
        // the boundary, so a `vmul` left mc_q at 1 and the `vdot` behind it never
        // fired its multipliers.
        always @(posedge clk) begin
            if (!resetn || !mul_in || m_complete) begin
                mc_q <= 2'd0;
            end
            else if (mc_q < (mul_nc - 2'd1)) begin
                mc_q <= mc_q + 2'd1;
            end
        end
    end
    endgenerate


    wire [VW-1:0]      lane_y;
    wire [VW-1:0]      lane_mul_lo;
    wire [34*SIMD-1:0] lane_dot;

    genvar L;
    generate
    for (L = 0; L < SIMD; L = L + 1) begin : g_lane
        khs_lane #(.MULS(MULS), .HAS_SHIFT(SH_IN_LANE), .DOT_DSP(DOT_DSP),
                   .USE_DSP(USE_DSP)) u_lane (
            .clk(clk),
            .a(v1[32*L +: 32]), .b(v2[32*L +: 32]), .et(m_et),
            .alu_op(m_alu_op), .cmp_sub(m_cmp_sub), .alu_sat(m_alu_sat),
            .sh_rot(m_sh_rot), .sh_keep(m_sh_keep), .sh_rmask(m_sh_rmask),
            .sh_arith(m_sh_arith), .sh_left(m_sh_left), .sh_round(m_sh_round),
            .el_mask(m_el_mask),
            .y(lane_y[32*L +: 32]),
            .mul_en(mul_en), .mpass(mul_mp),
            .dot_sum(lane_dot[34*L +: 34]),
            .mul_lo(lane_mul_lo[32*L +: 32])
        );
    end
    endgenerate

    // Pass 1's bytes are live, pass 0's are staged. int8 at MULS < 4 only.
    wire [VW-1:0] mul_res;
    genvar ML;
    generate
    if (MULS >= 4) begin : g_mres_one
        assign mul_res = lane_mul_lo;
    end else begin : g_mres_two
        reg [16*SIMD-1:0] mul_lo_st;
        integer ms;
        always @(posedge clk) begin
            if (mul_cap) begin
                for (ms = 0; ms < SIMD; ms = ms + 1) begin
                    mul_lo_st[16*ms +: 16] <= lane_mul_lo[32*ms +: 16];
                end
            end
        end
        for (ML = 0; ML < SIMD; ML = ML + 1) begin : g_mres
            assign mul_res[32*ML +: 32] =
                mul_two ? {lane_mul_lo[32*ML +: 16], mul_lo_st[16*ML +: 16]}
                        : lane_mul_lo[32*ML +: 32];
        end
    end
    endgenerate

    // ---- the accumulators --------------------------------------------------
    // Fabric registers rather than the DSP's own P register, so NACC is a
    // parameter and `vaccwr` (seeding an accumulation with a bias vector) is a
    // plain write. The recurrence is one cycle, which lets vdot issue at II=1.
    reg  [AAW-1:0] acc_idx [0:DOT_LAT-1];
    reg  [DOT_LAT-1:0] acc_neg;

    wire [AAW-1:0] a_now = acc_idx[DOT_LAT-1];
    wire           a_neg = acc_neg[DOT_LAT-1];

    integer aq;
    always @(posedge clk) begin
        if (!resetn) begin
            acc_pipe <= {DOT_LAT{1'b0}};
        end else begin
            acc_pipe   <= {acc_pipe[DOT_LAT-2:0], (m_complete && m_is_dot)};
            acc_neg    <= {acc_neg[DOT_LAT-2:0], m_is_dotn};
            acc_idx[0] <= m_vd[AAW-1:0];
            for (aq = 1; aq < DOT_LAT; aq = aq + 1) begin
                acc_idx[aq] <= acc_idx[aq-1];
            end
        end
    end

    // PER-BANK REGISTERS, not one array indexed by a runtime bank. As an array
    // `vaccz` is a THIRD input on every flop's write mux; as its own condition
    // on a per-bank register it is the flop's own synchronous clear, which is
    // free. `acc_idx`/`acc_neg` stay above so the bank select is registered.
    // LEAVE THE ACCUMULATE AS A TERNARY -- rewriting it as one adder with a
    // conditionally inverted operand measured byte-identical at 17,792 LUT.
    wire [VW-1:0] acc_bank [0:NACC-1];
    genvar BK, AL;
    generate
    for (BK = 0; BK < NACC; BK = BK + 1) begin : g_accbank
        wire hit  = m_complete && (m_vd[AAW-1:0] == BK[AAW-1:0]);
        wire e_z  = hit && m_is_accz;
        wire e_w  = hit && m_is_accwr;
        wire e_a  = acc_pipe[DOT_LAT-1] && (a_now == BK[AAW-1:0]);
        for (AL = 0; AL < SIMD; AL = AL + 1) begin : g_accel
            reg [31:0] r;
            always @(posedge clk) begin
                if (e_z) begin
                    r <= 32'd0;
                end
                else if (e_w) begin
                    r <= v1[32*AL +: 32];
                end
                else if (e_a) begin
                    r <= a_neg ? (r - lane_dot[34*AL +: 32])
                               : (r + lane_dot[34*AL +: 32]);
                end
            end
            assign acc_bank[BK][32*AL +: 32] = r;
        end
    end
    endgenerate

    // LEAVE THE BANK SELECT AS ITS OWN MUX. Making the NACC banks parallel
    // sources of `vres` -- to absorb it into slots an 8:1 already pays for --
    // measured 16,457 -> 16,764. A merge pays only when the COMBINED select
    // fits ONE LUT6; `vres` has seven sources and cannot, so it collapses
    // nothing and lengthens the chain.
    wire [VW-1:0] acc_rd = acc_bank[m_rs1[AAW-1:0]];

    // ---- the shape constraints, enforced at ELABORATION --------------------
    // FLANES MUST DIVIDE 2*SIMD. Three does not: `PASSES` truncates to 5, the
    // walk covers 15 of 16 elements, and it still elaborates, synthesises and
    // reports 330.4 MHz while failing the bench 10 of 66. Instantiating a
    // module that does not exist kills the build at elaboration with a name
    // that says which rule broke -- sb_nsu does the same for SDW > FW.
    generate
    if ((HAS_FLOAT != 0) && FL_ON
        && ((FLANES * PASSES) != NARROW_SLOTS)) begin : g_bad_fl
        khs_unit_requires_FLOAT_LANES_to_divide_2x_SIMD u_bad ();
    end
    // A float GROUP asked for with no units is a caller who forgot the count --
    // it used to be the widest tier there is, so it must not now be silence.
    if ((HAS_FLOAT != 0) && !FL_ON
        && ((HAS_FALU != 0) || (FSFU_UNITS != 0) || (HAS_FACC != 0)))
    begin : g_bad_fl0
        khs_unit_float_groups_need_FLOAT_LANES_nonzero u_bad ();
    end
    if ((HAS_FLOAT != 0) && (HAS_FACC != 0) && FL_ON
        && (((NPART / PASSES) * PASSES) != NPART)) begin : g_bad_np
        khs_unit_requires_PASSES_to_divide_NPART u_bad ();
    end
    if ((SHIFT_UNITS != 0)
        && ((((SIMD / SHIFT_UNITS) * SHIFT_UNITS) != SIMD)
            || (SHIFT_UNITS > SIMD))) begin : g_bad_shu
        khs_unit_requires_SHIFT_UNITS_to_divide_SIMD u_bad ();
    end
    if (HAS_FCVT != 0) begin : g_bad_cvt
        khs_unit_FCVT_decodes_but_has_no_datapath u_bad ();
    end
    if ((HAS_FLOAT != 0) && (FSFU_UNITS != 0)
        && ((((NARROW_SLOTS / FSFU_UNITS) * FSFU_UNITS) != NARROW_SLOTS)
            || (FSFU_UNITS > FLANES))) begin : g_bad_sf
        khs_unit_requires_FSFU_UNITS_to_divide_2xSIMD_and_not_exceed_FLANES u_bad ();
    end
    endgenerate

    // ================= the elementwise float unit ==========================
    // FALU and FSFU. The lanes are the accumulator's lanes and the arithmetic
    // is vec_alu's; what this adds is a PASS WALK over packed elements and a
    // RETIRE PATH, because a 15-deep result cannot be written in MEM.
    //
    // ONE WRITE PER INSTRUCTION, NOT ONE PER PASS. Each pass's FLANES results
    // are placed into a staging register as they return, and the register is
    // written to the file when the last pass lands -- so the file needs no
    // per-lane write enable and the port is claimed once.
    generate
    if ((HAS_FLOAT != 0) && FL_ON
        && ((HAS_FALU != 0) || (FSFU_UNITS != 0))) begin : g_fel
        localparam integer WIDE_ELEMS = VW / 32;
        // At FLANES > the wide element count the extra lanes simply idle, which
        // is why this floors at one pass rather than dividing to zero.
        localparam integer PW_N = NARROW_SLOTS / FLANES;
        localparam integer PW_W = (WIDE_ELEMS >= FLANES) ? (WIDE_ELEMS / FLANES) : 1;
        // The seed walk, which is a DIFFERENT number of passes over the same
        // elements whenever the seed units are fewer than the FMA units.
        localparam integer PS_N = NARROW_SLOTS / SEED_U;
        localparam integer PS_W = (WIDE_ELEMS >= SEED_U) ? (WIDE_ELEMS / SEED_U) : 1;
        localparam integer PMAX = (PS_N > PW_N) ? PS_N : PW_N;
        localparam integer EPSW = (PMAX > 1) ? $clog2(PMAX) : 1;
        // The FMA walk's own width. Placing an FMA pass with the SEED walk's
        // index builds a 16-way slot decode where four are reachable.
        localparam integer EPSW_F = (PW_N > 1) ? $clog2(PW_N) : 1;

        reg  [EPSW-1:0] iss_pass;
        // NOT `PW_W[EPSW-1:0]`. A parameter sliced to the pass index's own width
        // is 4 truncated to two bits, which is ZERO; the constants stay full
        // width and only the difference is truncated.
        wire [EPSW-1:0] last_pass = m_is_fsfu ? ((m_is_f32 ? PS_W : PS_N) - 1)
                                              : ((m_is_f32 ? PW_W : PW_N) - 1);
        // NOT `m_complete`: that is false while the walk holds, which is
        // exactly when the middle passes have to issue.
        wire el_live = (
            m_valid
            && m_is_fel
            && !m_left
            && !f_rd_hold
            && !f_sw_hold
        );
        wire iss_last = (iss_pass == last_pass);
        assign el_hold = el_live && !iss_last;

        always @(posedge clk) begin
            if (!resetn) begin
                iss_pass <= {EPSW{1'b0}};
            end
            else if (el_live) begin
                iss_pass <= iss_last ? {EPSW{1'b0}} : (iss_pass + 1'b1);
            end
        end

        wire [32*FLANES-1:0] falu_out;
        khs_falu #(.VW(VW), .FLANES(FLANES), .SEED_UNITS(FSFU_UNITS),
                   .PSW(EPSW), .PIPE_MUX(1), .MODEL(FLOAT_MODEL),
                   .HAS_F16(HAS_F16), .HAS_F32(HAS_F32))
        u_falu (
            .clk(clk), .rst(!resetn),
            .in_valid(el_live), .op(m_fop), .is_cmp(m_is_fcmp),
            .wide(m_is_f32), .pass(iss_pass),
            .v1(v1), .v2(v2), .vd(v3),
            .out_valid(), .out(falu_out)
        );

        // The shadow. It carries what the lane array does not -- destination,
        // pass, format, and whether this is the pass that finishes the
        // instruction -- and it MUST be the lane's exact depth or a result
        // lands on the wrong register with no witness.
        reg [FLOAT_ALAT:1]  sh_v, sh_last, sh_wide;
        // WHICH WALK THIS RESULT CAME OFF. A seed's units are a different count
        // from an FMA's, so the placement below cannot read the live decode: by
        // then it belongs to whatever issued fifteen cycles later.
        reg [FLOAT_ALAT:1]  sh_sfu;
        reg [4:0]           sh_wa   [1:FLOAT_ALAT];
        reg [EPSW-1:0]      sh_pass [1:FLOAT_ALAT];

        integer si;
        always @(posedge clk) begin
            if (!resetn) begin
                sh_v <= {FLOAT_ALAT{1'b0}};
            end else begin
                sh_v[1]    <= el_live;
                sh_last[1] <= iss_last;
                sh_wide[1] <= m_is_f32;
                sh_sfu[1]  <= m_is_fsfu;
                sh_wa[1]   <= m_vd;
                sh_pass[1] <= iss_pass;
                for (si = 2; si <= FLOAT_ALAT; si = si + 1) begin
                    sh_v[si]    <= sh_v[si-1];
                    sh_last[si] <= sh_last[si-1];
                    sh_wide[si] <= sh_wide[si-1];
                    sh_sfu[si]  <= sh_sfu[si-1];
                    sh_wa[si]   <= sh_wa[si-1];
                    sh_pass[si] <= sh_pass[si-1];
                end
            end
        end

        wire ret_v = sh_v[FLOAT_ALAT];

        // PLACED BY THE WALK THAT PRODUCED IT. A seed pass carries SEED_U
        // results and an FMA pass FLANES of them, so the stride is the retiring
        // instruction's own count and a unit above SEED_U contributes nothing on
        // a seed pass. `pl < SEED_U` is a constant per unrolled iteration.
        reg [VW-1:0] stage_r;
        integer pl;
        always @(posedge clk) if (ret_v) begin
            for (pl = 0; pl < FLANES; pl = pl + 1) begin
                if (!sh_sfu[FLOAT_ALAT]) begin
                    if (sh_wide[FLOAT_ALAT]) begin
                        stage_r[32*(sh_pass[FLOAT_ALAT][EPSW_F-1:0]*FLANES + pl)
                                +: 32] <= falu_out[32*pl +: 32];
                    end
                    else begin
                        stage_r[16*(sh_pass[FLOAT_ALAT][EPSW_F-1:0]*FLANES + pl)
                                +: 16] <= falu_out[32*pl +: 16];
                    end
                end else if (pl < SEED_U) begin
                    if (sh_wide[FLOAT_ALAT]) begin
                        stage_r[32*(sh_pass[FLOAT_ALAT]*SEED_U + pl) +: 32]
                            <= falu_out[32*pl +: 32];
                    end
                    else begin
                        stage_r[16*(sh_pass[FLOAT_ALAT]*SEED_U + pl) +: 16]
                            <= falu_out[32*pl +: 16];
                    end
                end
            end
        end

        // The write is one cycle BEHIND the last pass landing, because that
        // pass's own data reaches `stage_r` on the same edge.
        reg       wr_r;
        reg [4:0] wa_r;
        always @(posedge clk) begin
            if (!resetn) begin
                wr_r <= 1'b0;
            end
            else begin
                wr_r <= ret_v && sh_last[FLOAT_ALAT];
                wa_r <= sh_wa[FLOAT_ALAT];
            end
        end

        assign el_wr    = wr_r;
        assign el_wa    = wa_r;
        assign el_wdata = stage_r;
        // ONE CYCLE OF WARNING AT WB_STAGE 0, TWO AT 1. Raising `stall` clears
        // m_valid next cycle, which is when `el_wr` claims the port -- but at
        // WB_STAGE 1 the ordinary write is a stage later still, so a single
        // cycle leaves it live on exactly the cycle the float needs the port.
        assign el_soon  = (ret_v && sh_last[FLOAT_ALAT])
                       || ((WB_STAGE != 0) && sh_v[FLOAT_ALAT-1]
                           && sh_last[FLOAT_ALAT-1]);

        reg [VREGS-1:0] busy_r;
        always @(posedge clk) begin
            if (!resetn) begin
                busy_r <= {VREGS{1'b0}};
            end
            else begin
                if (el_live && (iss_pass == {EPSW{1'b0}})) begin
                    busy_r[m_vd[VAW-1:0]] <= 1'b1;
                end
                if (wr_r) begin
                    busy_r[wa_r[VAW-1:0]] <= 1'b0;
                end
            end
        end
        assign el_busy = busy_r;
        // The shadow, plus the retiring cycle behind it: `stage_r` is not free
        // until the write that reads it has happened.
        assign el_inflight = (|sh_v) || wr_r;
    end else begin : g_no_fel
        assign el_inflight = 1'b0;
        assign el_hold  = 1'b0;
        assign el_soon  = 1'b0;
        assign el_wr    = 1'b0;
        assign el_wa    = 5'd0;
        assign el_wdata = {VW{1'b0}};
        assign el_busy  = {VREGS{1'b0}};
    end
    endgenerate

    // ---- the float tier ----------------------------------------------------
    // NARROW_SLOTS FP16 -- or WIDE_SLOTS FP32 over lane pairs -- across FLANES
    // lanes, PASSES of them to a lane, accumulating into NPART rotating
    // partials. The rotation is what makes II = 1 possible over a 15-deep lane;
    // NPART and PASSES are architectural, because both change the answers.
    // The INPUT FORMAT is not: it is converted at the lane's edge and everything
    // from the multiplier on is E8M15 and identical either way.
    generate
    if ((HAS_FLOAT != 0) && (HAS_FACC != 0) && FL_ON) begin : g_float
        localparam integer SW  = (NARROW_SLOTS > 1) ? $clog2(NARROW_SLOTS) : 1;
        localparam integer FIW = (FLANES > 1) ? $clog2(FLANES) : 1;
        localparam [1:0] ST_IDLE = 2'd0, ST_FILL = 2'd1, ST_RUN = 2'd2;
        localparam [1:0] ST_DONE = 2'd3;

        localparam integer PSW = (PASSES > 1) ? $clog2(PASSES) : 1;
        localparam integer NP_EFF = NPART / PASSES;

        wire [24*FLANES-1:0] part_rd, lane_out;
        // Every lane is the same depth, so one valid speaks for all of them.
        wire [FLANES-1:0]    lane_ov;
        wire                 lane_ovld = lane_ov[0];
        reg  [24*FLANES-1:0] total;
        reg  [24*NARROW_SLOTS-1:0] seed_r;
        reg  [16*NARROW_SLOTS-1:0] packed_narrow;
        wire [FPW-1:0]       sweep_idx;

        // THE WALK. `vfmacc` issues one operation per pass; the instruction is
        // not complete until the last of them has gone. At PASSES = 1 `iss_pass`
        // is constant 0 and `f_pass_hold` is constant low, so nothing changes.
        reg  [PSW-1:0] iss_pass;
        wire           iss_last = (PASSES == 1)
                                || (iss_pass == (PASSES[PSW-1:0] - 1'b1));
        // NOT `!hz_fold`: that contains `f_pass_hold` itself, and reading it
        // here closes a combinational loop. The other two hold terms are the
        // ones that must gate an issue.
        wire fma_live = (
            m_valid
            && m_is_fma
            && !m_left
            && !f_rd_hold
            && !f_sw_hold
        );
        assign f_pass_hold = fma_live && !iss_last;

        always @(posedge clk) begin
            if (!resetn) begin
                iss_pass <= {PSW{1'b0}};
            end
            else if (fma_live) begin
                iss_pass <= iss_last ? {PSW{1'b0}} : (iss_pass + 1'b1);
            end
        end

        // Fires on EVERY pass, not only the retiring one -- `m_complete` is
        // false while the walk holds, which is exactly when the middle passes
        // have to issue.
        wire acc_fire = fma_live;

        wire [16*FLANES-1:0] a_sl = v1[16*FLANES*iss_pass +: 16*FLANES];
        wire [16*FLANES-1:0] b_sl = v2[16*FLANES*iss_pass +: 16*FLANES];

        // ---- vfaccz / vfaccwr : build the seed word, then sweep -------------
        // ONE CONVERTER, WALKED, NOT NARROW_SLOTS OF THEM: sixteen parallel ones
        // measured 720 LUT for an instruction that runs once per kernel. The
        // word is registered because the sweep writes all slots at once.
        reg [1:0]    sw_st;
        reg [SW-1:0] sd_k;
        reg          do_z, do_s;

        // ONE WALK, BOTH FORMATS, AT THE SAME INDEX: FP32 element j lands in
        // seed slot 2j, so the wide converter reads the pair at `sd_k` and
        // writes where the narrow one would have, needing no second index. The
        // zero-extension keeps that pair read in range on the last slot.
        wire [16*NARROW_SLOTS+15:0] v1_ext = {16'd0, v1};
        wire [23:0] sd_e8, sd_e8_w;
        vec_cvt_f16_to_e8 u_sd (.f16(v1[16*sd_k +: 16]), .e8(sd_e8));
        vec_cvt_f32_to_e8 u_sd32 (.f32(v1_ext[16*sd_k +: 32]), .e8(sd_e8_w));
        wire [23:0] sd_word = !m_is_f32 ? sd_e8 : (sd_k[0] ? 24'd0 : sd_e8_w);

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
                                 seed_r[24*sd_k +: 24] <= sd_word;
                                 if (sd_k == (NARROW_SLOTS-1)) begin
                                     do_s  <= 1'b1;
                                     sw_st <= ST_RUN;
                                 end
                                 else begin
                                     sd_k <= sd_k + 1'b1;
                                 end
                             end
                    // `busy_sweep` is low on the cycle the pulse is issued, so
                    // the pulse itself has to hold this state.
                    ST_RUN:  if (!f_sweep && !do_z && !do_s) begin
                        sw_st <= ST_DONE;
                    end
                    default: sw_st <= ST_IDLE;
                endcase
            end
        end

        // ---- vfaccrd : fold the partials, then pack to FP16 -----------------
        // E8M15 -> FP16 carries a 48-bit subnormal shifter: 161 LUT each, 2,576
        // hung on the end of a 256-cycle instruction.
        reg [1:0]     rd_st;
        reg [SW-1:0]  pk_k;
        reg [PSW-1:0] fold_pass;
        wire          folding = (rd_st == ST_RUN);
        wire          fold_go = (rd_st == ST_IDLE) && m_valid && m_is_ffold;
        // The last pass has been packed, so the whole reduction is done.
        wire          fold_last = (PASSES == 1)
                                || (fold_pass == (PASSES[PSW-1:0] - 1'b1));

        // ONE FOLD PER PASS. `total` is FLANES wide and each pass produces the
        // results for a different FLANES-sized slice of the register, so the
        // run/pack pair repeats and the pack offset moves with the pass.
        wire fold_next = (rd_st == ST_FILL) && (pk_k == (FLANES-1)) && !fold_last;

        // THE PACK WALKS SLOTS, NOT ELEMENTS, and an FP32 element occupies the
        // two 16-bit slots its own lane pair would have -- so the same walk, the
        // same offset, and only the WORD differs: the low half on the even step,
        // the high half on the odd one, both from the even lane's total.
        wire [FIW-1:0] pk_mask = m_is_f32 ? {{(FIW-1){1'b1}}, 1'b0}
                                          : {FIW{1'b1}};
        wire [FIW-1:0] pk_sel  = pk_k[FIW-1:0] & pk_mask;
        wire [23:0]    pk_e8   = total[24*pk_sel +: 24];
        wire [15:0] pk_narrow;
        wire [31:0] pk_wide;
        vec_cvt_e8_to_f16 u_pk   (.e8(pk_e8), .f16(pk_narrow));
        vec_cvt_e8_to_f32 u_pk32 (.e8(pk_e8), .f32(pk_wide));
        wire [15:0] pk_word = !m_is_f32 ? pk_narrow
                            : (pk_k[0] ? pk_wide[31:16] : pk_wide[15:0]);

        assign f_rd_hold  = m_valid && m_is_ffold && (rd_st != ST_DONE);
        assign facc_rd_v  = packed_narrow;

        always @(posedge clk) begin
            if (!resetn) begin
                rd_st     <= ST_IDLE;
                fold_pass <= {PSW{1'b0}};
            end else begin
                case (rd_st)
                    ST_IDLE: if (fold_go) begin
                        rd_st     <= ST_RUN;
                        fold_pass <= {PSW{1'b0}};
                    end
                    ST_RUN: if (f_done) begin
                        rd_st <= ST_FILL;
                        pk_k  <= {SW{1'b0}};
                    end
                    ST_FILL: begin
                        // A CONCATENATION, NOT A MULTIPLY-ADD: FLANES is a
                        // power of two, so the element index is just the pass
                        // above the lane and the tool gets a plain decoder
                        // instead of an adder feeding one.
                        packed_narrow[16*{fold_pass, pk_k[FIW-1:0]} +: 16]
                            <= pk_word;
                        if (pk_k == (FLANES-1)) begin
                            if (fold_last) begin
                                rd_st <= ST_DONE;
                            end
                            else begin
                                rd_st     <= ST_RUN;
                                fold_pass <= fold_pass + 1'b1;
                            end
                        end
                        else begin
                            pk_k <= pk_k + 1'b1;
                        end
                    end
                    default: rd_st <= ST_IDLE;
                endcase
            end
        end

        // The lane's depth, as a shadow: `wait_facc` reads it in EX.
        reg [FLOAT_ALAT-1:0] fpipe;
        always @(posedge clk) begin
            if (!resetn) begin
                fpipe <= {FLOAT_ALAT{1'b0}};
            end
            else begin
                fpipe <= {fpipe[FLOAT_ALAT-2:0], acc_fire};
            end
        end
        assign f_inflight = |fpipe;

        // NP_EFF, not NPART: one element's chain is NPART/PASSES partials, and
        // `fold_addr` maps this walk onto the strided subset that belongs to it.
        // ITS index is NARROWER than `f_idx`, so it drives a local wire and is
        // zero-extended -- connected directly, the top bits stay undriven and
        // every folded element comes back X.
        localparam integer NEW = (NP_EFF > 1) ? $clog2(NP_EFF) : 1;
        wire [NEW-1:0] f_idx_e;
        assign f_idx = {{(FPW-NEW){1'b0}}, f_idx_e};

        khs_ffold #(.NPART(NP_EFF), .ALAT(FLOAT_ALAT)) u_ffold (
            .clk(clk), .resetn(resetn),
            .start(fold_go || fold_next), .busy(f_busy), .done(f_done),
            .part_idx(f_idx_e), .iss_valid(f_iss), .iss_raw(f_raw)
        );

        genvar S;
        for (S = 0; S < FLANES; S = S + 1) begin : g_flane
            // A fold's addend is the running total; an accumulate's is the
            // partial the counter selected.
            wire [23:0] c_sel = folding ? total[24*S +: 24]
                                        : part_rd[24*S +: 24];
            // FP32 IS PAIRED LANES, NOT A WIDER PASS: an even lane takes its own
            // slot and its neighbour's as one FP32 while the odd lane idles, so
            // PASSES, the partial count and the fold order are the SAME NUMBERS
            // in both formats. Widening the pass instead halves PASSES and
            // doubles the chain, and float addition does not associate.
            localparam integer PAIRED = ((S % 2) == 0) && (FLANES > 1);
            wire [31:0] a_w, b_w;
            if (PAIRED) begin : g_pair
                assign a_w = {a_sl[16*(S+1) +: 16], a_sl[16*S +: 16]};
                assign b_w = {b_sl[16*(S+1) +: 16], b_sl[16*S +: 16]};
            end else begin : g_solo
                assign a_w = {16'd0, a_sl[16*S +: 16]};
                assign b_w = {16'd0, b_sl[16*S +: 16]};
            end
            wire lane_wide = PAIRED ? m_is_f32 : 1'b0;
            // The subtract flips the operand's SIGN BIT, which moves with the
            // format: bit 31 wide, bit 15 narrow.
            wire [31:0] b_neg = b_w ^ (m_is_fmsub
                                       ? (lane_wide ? 32'h8000_0000
                                                    : 32'h0000_8000)
                                       : 32'd0);

            // AN IDLE LANE STILL WRITES, because the partials are one memory
            // word per turn -- so it must write back what it read. A ZERO
            // FACTOR IS HOW: vec_alu forces both exponents to their minimum on
            // a zero product, the addend dominates, and it passes through with
            // its sign. Leaving the misaligned operands connected instead put
            // an odd lane's garbage into the FP16 slots and failed every odd
            // element of a mixed-width accumulator.
            wire idle = !PAIRED && m_is_f32;

            // `.op` WAS NOT CONNECTED. An unconnected input is `z`, so every
            // accumulated element read back as X in simulation and, in
            // synthesis, tied to 0 = OP_MOV -- which vec_alu routes as
            // a * 1.0 + 0. The accumulator was a PASS-THROUGH, not an FMA, and
            // `vfaccrd` failed 13 checks of khs_unit's own float stream.
            // Both paths here are FMA: an accumulate is a*b + partial and a
            // fold is partial*1.0 + total, the latter through `raw_e8`.
            khs_float_lane #(.PIPE_MUX(1), .MODEL(FLOAT_MODEL),
                             .HAS_F16(HAS_F16), .HAS_F32(HAS_F32)) u_fl (
                .clk(clk), .rst(!resetn),
                .in_valid(acc_fire | f_iss), .op(KHS_FOP_FMA),
                .wide(lane_wide),
                .a(idle ? 32'd0 : a_w),
                .b(idle ? 32'd0 : b_neg),
                .c(c_sel),
                .raw_e8(f_raw), .a_e8(fold_part_v[24*S +: 24]),
                .out_valid(lane_ov[S]),
                .out(lane_out[24*S +: 24])
            );
        end

        always @(posedge clk) begin
            if (fold_go || fold_next) begin
                total <= {(24*FLANES){1'b0}};
            end
            else if (folding && lane_ovld) begin
                total <= lane_out;
            end
        end

        // THE SEED SLICE FOR THE PASS THE SWEEP IS ON. All NARROW_SLOTS elements are
        // converted into `seed_r` by the walked converter; the accumulator only
        // ever writes FLANES of them at a time.
        wire [24*FLANES-1:0] seed_sl =
            seed_r[24*FLANES*(sweep_idx[PSW-1:0]) +: 24*FLANES];

        // THE FOLD WALKS ONE PASS'S PARTIALS, not all of them. Element e's chain
        // is the turns congruent to its pass modulo PASSES, so a flat fold over
        // NPART would sum ACROSS elements -- which is what the measurement probe
        // in khs_float_tier.v gets wrong and why this is not a straight lift.
        wire [FPW-1:0] fold_addr = (PASSES == 1)
                        ? f_idx
                        : {f_idx_e, fold_pass};

        khs_facc #(.SLOTS(FLANES), .NACC(NACC), .NPART(NPART),
                   .ALAT(FLOAT_ALAT), .PASSES(PASSES))
        u_facc (
            .clk(clk), .resetn(resetn),
            .acc_valid(acc_fire), .acc_sel(m_vd[AAW-1:0]),
            .rd_part(part_rd), .rd_idx(),
            // A fold's results are NOT accumulates and must not land in the
            // partials being read.
            .wb_valid(lane_ovld && !folding), .wb_data(lane_out),
            .do_zero(do_z), .do_seed(do_s),
            .ctl_sel(m_vd[AAW-1:0]), .seed_data(seed_sl),
            .fold_sel(m_rs1[AAW-1:0]), .fold_idx(fold_addr),
            .fold_part(fold_part_v), .busy_sweep(f_sweep),
            .sweep_idx(sweep_idx)
        );
    end else begin : g_no_float
        assign facc_rd_v   = {VW{1'b0}};
        assign f_busy = 1'b0; assign f_done = 1'b0; assign f_sweep = 1'b0;
        assign f_iss  = 1'b0; assign f_raw  = 1'b0;
        assign f_sw_hold = 1'b0; assign f_rd_hold = 1'b0;
        assign f_pass_hold = 1'b0;
        assign f_inflight = 1'b0;
        assign f_idx  = {(NPART>1 ? $clog2(NPART) : 1){1'b0}};
        assign fold_part_v = {(24*NARROW_SLOTS){1'b0}};
    end
    endgenerate

    // ---- the packed shifter, as a width ------------------------------------
    // AT SHU < SIMD THE SHIFTER LEAVES THE LANE. Unit u on pass p serves lane
    // p*SHU + u, so it needs a SIMD:1 32-bit operand mux -- and THAT is what the
    // earlier refusal in the docs got wrong: it charged one mux per shifter
    // REMOVED, where the walk pays one mux per unit KEPT. At one unit that is a
    // single mux against seven shifters, not one against one.
    //
    // The shift masks are already built once in EX for every lane, so a unit
    // takes them unchanged; only the OPERAND is per lane.
    wire [VW-1:0] shf_res;
    generate
    if (SHPASS == 1) begin : g_shf_lane
        assign shf_hold = 1'b0;
        assign shf_res  = {VW{1'b0}};
    end else begin : g_shf_walk
        reg [SHPW:0] sh_q;
        // OP_SH is what `is_shi` becomes in the MEM register; there is no
        // separate decode bit and adding one would be two names for it.
        wire         in_mem  = m_valid && (m_alu_op == OP_SH) && !m_left;
        wire         issuing = (sh_q != SHPASS[SHPW:0]);
        // NOT `!hz_fold`: that contains this signal and reading it closes a
        // combinational loop -- the trap `f_pass_hold` records.
        assign shf_hold = in_mem && issuing;

        always @(posedge clk) begin
            if (!resetn) begin
                sh_q <= {(SHPW+1){1'b0}};
            end
            else if (in_mem) begin
                sh_q <= issuing ? (sh_q + 1'b1) : {(SHPW+1){1'b0}};
            end
            else begin
                sh_q <= {(SHPW+1){1'b0}};
            end
        end

        wire [32*SHU-1:0] shy;
        genvar SU2;
        for (SU2 = 0; SU2 < SHU; SU2 = SU2 + 1) begin : g_shunit
            wire [LAW-1:0] slane = sh_q[SHPW-1:0] * SHU + SU2;
            wire [31:0]    sop   = v1[32*slane +: 32];
            wire [31:0]    sy, srb;
            khs_pshift32 u_sh (
                .x(sop), .et(m_et), .rot(m_sh_rot), .keep(m_sh_keep),
                .rmask(m_sh_rmask), .arith(m_sh_arith), .left(m_sh_left),
                .y(sy), .round_bit(srb)
            );
            // The round bit is GATED, not the result: masking it is four LUTs
            // where choosing after the adder is a 32-bit mux. khs_lane's own
            // note, and it holds here.
            wire [31:0] rin = m_sh_round ? srb : 32'd0;
            khs_padd32 u_rnd (
                .a(sy), .b(rin), .sub(1'b0), .et(m_et), .mask(m_el_mask),
                .sat(1'b0), .y(shy[32*SU2 +: 32]), .lt(), .lt_s(), .top()
            );
        end

        // ONE STAGING REGISTER AND ONE DRAIN STEP, exactly as the permute walk
        // does -- khs_vregfile has no per-element write enable, which is why
        // SIMD stages where the SIMT side writes straight through.
        reg [VW-1:0] shst;
        integer sk;
        always @(posedge clk) begin
            if (in_mem && issuing) begin
                for (sk = 0; sk < SHU; sk = sk + 1) begin
                    shst[32*(sh_q[SHPW-1:0]*SHU + sk) +: 32] <= shy[32*sk +: 32];
                end
            end
        end
        assign shf_res = shst;
    end
    endgenerate

    // ---- the cross-lane network -------------------------------------------
    // PU output words a pass, SIMD/PU passes, into a staging register. The
    // walk holds MEM through `hz_fold`, so the instruction retires once.
    wire [32*PU-1:0] perm_y;
    wire [PPW-1:0]   prm_pass;
    wire [VW-1:0]    prm_res;

    khs_perm #(.SIMD(SIMD), .HAS_PERM(HAS_PERM), .UNITS(PERM_UNITS),
               .PSW(PPW)) u_perm (
        .v1(v1), .v2(v2), .op4(m_prm_op4), .idx(m_prm_idx),
        .pass(prm_pass), .y(perm_y)
    );

    generate
    if (PPASS == 1) begin : g_prm_one
        assign prm_pass = {PPW{1'b0}};
        assign prm_hold = 1'b0;
        assign prm_res  = perm_y;
    end else begin : g_prm_walk
        // ONE STEP PAST THE LAST PASS. The final pass's words reach `st` on the
        // edge that ends its cycle, so reading `st` on that same cycle would
        // need a per-word mux against `perm_y` -- SIMD 32-bit muxes, which is
        // most of what the narrower units just saved. A drain step costs one
        // cycle on an instruction that already takes PPASS of them.
        reg [PPW:0] pp_q;
        wire        in_mem  = m_valid && m_is_prm && !m_left;
        wire        issuing = (pp_q != PPASS[PPW:0]);
        // NOT `!hz_fold`: that contains this signal, and reading it here closes
        // a combinational loop -- the trap `f_pass_hold` records.
        assign prm_hold = in_mem && issuing;
        assign prm_pass = pp_q[PPW-1:0];

        always @(posedge clk) begin
            if (!resetn) begin
                pp_q <= {(PPW+1){1'b0}};
            end
            else if (in_mem) begin
                pp_q <= issuing ? (pp_q + 1'b1) : {(PPW+1){1'b0}};
            end
            else begin
                pp_q <= {(PPW+1){1'b0}};
            end
        end

        reg [VW-1:0] st;
        integer pk;
        always @(posedge clk) begin
            if (in_mem && issuing) begin
                for (pk = 0; pk < PU; pk = pk + 1) begin
                    st[32*(pp_q[PPW-1:0]*PU + pk) +: 32] <= perm_y[32*pk +: 32];
                end
            end
        end
        assign prm_res = st;
    end
    endgenerate

    // Reductions: a tree, not a loop -- a chain that carries a value between
    // iterations synthesises as exactly that chain, which cost the accumulator
    // ~68 MHz once (mx_fpacc.v).
    wire [31:0] red_sum, red_max;
    khs_reduce #(.SIMD(SIMD), .PIPE(RED_PIPE)) u_red (
        .clk(clk), .v(v1), .sum(red_sum), .max(red_max)
    );

    wire [31:0] extr = v1[32 * m_lane +: 32];

    // ---- the result muxes --------------------------------------------------
    // LEAVE THIS A PRIORITY CHAIN. Every select is a registered decode bit and
    // the tool already balances it; encoding the select into 3 bits and casing
    // on it measured 10,343 -> 10,397 LUT and 357.1 -> 355.2 MHz.
    reg [VW-1:0] vres;
    always @(*) begin
        if (m_is_vld) begin
            vres = sp_rd;
        end
        else if (m_is_facrd) begin
            vres = facc_rd_v;
        end
        else if (m_is_accrd) begin
            vres = acc_rd;
        end
        else if (m_is_splat) begin
            vres = {SIMD{m_xdata}};
        end
        else if (m_is_prm) begin
            vres = prm_res;
        end
        else if (m_is_mul) begin
            vres = mul_res;
        end
        // ONLY WHEN THE SHIFTER LEFT THE LANE. At SHPASS == 1 this is a constant
        // false and the arm is trimmed, so the chain is the seven-source one
        // every earlier row measured.
        else if ((SHPASS != 1) && (m_alu_op == OP_SH)) begin
            vres = shf_res;
        end
        else begin
            vres = lane_y;
        end
    end
    assign vwdata = vres;

    // The probe is the FILE'S write port, not the MEM stage's intent, so it
    // reports the same stream at either WB_STAGE -- one cycle later at 1, and
    // still in program order, which is what the bench compares.
    assign dbg_wr_valid = vrf_we;
    assign dbg_wr_vd    = vrf_wa;
    assign dbg_wr_data  = vrf_wd;

    always @(posedge clk) begin
        if (!resetn) begin
            w_sc_valid <= 1'b0;
        end
        else begin
            w_sc_valid <= m_wr_sc;
            w_sc <= m_is_extr ? extr : m_is_rsum ? red_sum : red_max;
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) if (resetn) begin
        if (x_valid && x_illegal) begin
            $display("%0t khs_unit: instruction %08h is not built in this configuration (SIMD %0d, VREGS %0d, NACC %0d, MULS %0d, shift %0d, perm %0d)",
                     $time, x_instr, SIMD, VREGS, NACC, MULS, HAS_SHIFT, HAS_PERM);
        end
        if (x_valid && x_misalign) begin
            $display("%0t khs_unit: vector address %08h is not a multiple of %0d bytes",
                     $time, x_addr, VBYTES);
        end
    end
`endif

endmodule

`default_nettype wire

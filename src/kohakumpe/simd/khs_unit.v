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
//   float elementwise                              retires FLOAT_ALAT later
//
// EVERY COMPUTE FEATURE IS A WIDTH, and a width below full walks: SIMD/W passes
// through one array, holding MEM until the last one issues. A width of 0 builds
// nothing and its encodings fault in decode. See the widths specification.
//
// THERE IS NO INTEGER DOT AND NO INTEGER ACCUMULATOR. A dot product is `vmul`
// then `vredsum`, or a multiply whose partials the scalar core sums; a part
// needing high-rate dot carries matrix units.

`default_nettype none

module khs_unit #(
    parameter integer SIMD          = 8,        // 32-bit lanes; VW = 32*SIMD
    // INTEGER IM LANES: the packed ALU and its multipliers, which are one unit.
    // 0 IS NOT BUILT and every integer vector encoding faults; N builds N and an
    // operation takes SIMD/N passes.
    parameter integer ILANES        = 8,
    parameter integer VREGS         = 8,
    // Banks in the FLOAT accumulator; it is the only accumulator this unit has.
    parameter integer NACC          = 2,
    parameter integer VSPAD_ENTRIES = 1024,
    // Packed-shift units. 0 IS NOT BUILT and the shift encodings fault.
    parameter integer SHIFT_UNITS   = 8,
    // Cross-lane units: 32-bit OUTPUT words the permute produces per pass.
    // 0 IS NOT BUILT and the permute encodings fault.
    parameter integer PERM_UNITS    = 8,
    // THE FLOAT GROUPS ARE SEPARATE PARAMETERS because they are not equally
    // fundamental. FALU -- mul, add, sub, fma, min, max, compare -- is what a
    // SIMD ISA ships as its base, so it is on wherever float lanes are. The
    // rest are additions, enabled one at a time, which is what lets the SIMD
    // PEs of a mesh carry different feature sets instead of one global choice.
    // `FLOAT_LANES = 0` is what says "no float tier"; there is no second gate.
    parameter integer HAS_FALU        = 1,   // the base; 0 only to measure it
    // int32 <-> binary32 converters per pass. 0 = not built and the whole FCVT
    // group faults; -1 = one per element. There is no `f2f`: FP32 is the only
    // float type, so there is no second format to convert between.
    parameter integer FCVT_UNITS      = 0,
    // exp2, log2, rcp, rsqrt -- A UNIT COUNT, NOT A BOOLEAN. 0 builds none and
    // the opcode faults; N builds N seed-capable units out of FLOAT_LANES and a
    // seed walks SIMD/N passes where an FMA walks SIMD/FLOAT_LANES. Every other
    // unit is a plain FMA unit, without khs_fp32_sfu's table and two DSPs.
    // A NONZERO COUNT DEEPENS THE WHOLE TIER: the seed is 10 cycles and the FMA
    // is 6, so FLOAT_ALAT below moves with it.
    parameter integer FSFU_UNITS      = 0,
    // The rotating accumulator and its fold. OFF by default: it is the SIMD
    // PE's extra, justified by vertex transform, float dot and long reductions,
    // and a shader doing elementwise colour work pays nothing for it.
    parameter integer HAS_FACC        = 0,
    // Rotating partials per slot, and the lane's latency. NPART must EXCEED
    // ALAT or a partial is re-read before its write returns -- and the count is
    // architectural, because float addition does not associate.
    parameter integer NPART         = 16,
    // binary32 FMA units. 0 IS NOT BUILT and every float encoding faults.
    // Legal: 0,1,2,4,8 -- must divide SIMD, and the pass count it implies must
    // divide NPART.
    parameter integer FLOAT_LANES     = 0,
    // Where the vector file is written. See the header: 0 keeps the RAW hazard
    // at distance 1 and puts read-compute-write in one cycle; 1 registers the
    // result first, costing a second stall and halving the path. Both are
    // built and measured, as FWD_X is in the base core.
    parameter integer WB_STAGE      = 0,
    // The reduction tree, split across two cycles. It runs once per reduction
    // rather than once per element, so the cycle is free and the depth is not.
    parameter integer RED_PIPE      = 1,
    // `vredsum` / `vredmax` trees. A COUNT whose only values are 0 and 1: a
    // reduction consumes the whole register in one instruction, so there is no
    // per-element walk -- but the vocabulary is counts, not booleans.
    parameter integer RED_UNITS     = 1,
    // `vsrari`'s round adder, one SWAR add per lane inside khs_lane. Separable
    // from the shifter it rides on; 0 makes vsrari round-to-zero.
    parameter integer HAS_SHROUND   = 1,
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
    // use-before-declare that synthesis accepts silently. FP32 IS THE ONLY
    // COMPUTE TYPE, so a 32-bit word holds exactly one element and the float
    // tier's slot count IS the lane count.
    localparam integer FSLOTS = SIMD;
    // 0 IS "NOT BUILT", exactly as the SIMT PE spells it. It used to mean "one
    // unit per element", so the same 0 meant the WIDEST possible float tier
    // here and NO float tier next door -- a caller that forgot the parameter
    // got opposite machines from the two cores.
    localparam integer FLANES   = (FLOAT_LANES < 0) ? SIMD : FLOAT_LANES;
    localparam integer FL_ON    = (FLOAT_LANES != 0) ? 1 : 0;
    // `HAS_FLOAT` is gone: the width says whether the tier exists.
    localparam integer HAS_FLOAT = FL_ON;
    // Stands in for FLANES wherever a width or a division needs a nonzero, in
    // the branch that is then not elaborated: a zero-width wire and a divide by
    // zero are elaboration errors, not trimmed ones.
    localparam integer FLANES_W = FL_ON ? FLANES : 1;
    localparam integer PASSES = FSLOTS / FLANES_W;
    // The seed units, and 0 means "no seeds" rather than "all of them": with
    // none built the walk is one number and the decode refuses the opcode.
    // -1 IS FULL RATE and is resolved HERE, once: passed down unresolved it is
    // a negative unit count, so `L < FSFU_UNITS` builds no seed unit while the
    // decode still accepts the opcode.
    localparam integer SEED_N = (FSFU_UNITS < 0) ? FLANES_W : FSFU_UNITS;
    localparam integer SEED_U = (SEED_N > 0) ? SEED_N : FLANES_W;
    // TWO DEPTHS, AND THEY ARE NOT THE SAME NUMBER. rv_fpu is 6 and
    // khs_fp32_sfu is 10, so the ELEMENTWISE tier pads its FMA path to 10 when
    // seeds are built. The ACCUMULATOR has no seeds and stays at 6 -- given the
    // padded number its write index would trail its own results by three and
    // every partial would land on the wrong turn.
    localparam integer FMA_ALAT   = 6;
    localparam integer FLOAT_ALAT = (FSFU_UNITS != 0) ? 10 : FMA_ALAT;
    // EVERY WIDTH HAS AN "ON" BIT AND A NONZERO STAND-IN. A zero-width wire and
    // a divide by zero are elaboration errors even in a branch that is never
    // taken, so `*_W` stands in wherever a width or a division needs a nonzero.
    // -1 IS FULL RATE, the house rule: a caller says "one per element" without
    // having to know SIMD, and 0 stays the single spelling of "not built".
    localparam integer PERM_ON = (PERM_UNITS != 0) ? 1 : 0;
    localparam integer PU      = (PERM_UNITS < 0) ? SIMD : (PERM_ON ? PERM_UNITS : 1);
    localparam integer PPASS   = SIMD / PU;
    localparam integer PPW     = (PPASS > 1) ? $clog2(PPASS) : 1;

    localparam integer INT_ON  = (ILANES != 0) ? 1 : 0;
    localparam integer IU      = (ILANES < 0) ? SIMD : (INT_ON ? ILANES : 1);
    localparam integer IPASS   = SIMD / IU;
    localparam integer IPW     = (IPASS > 1) ? $clog2(IPASS) : 1;

    localparam integer CVT_ON  = (FCVT_UNITS != 0) ? 1 : 0;
    localparam integer CU      = (FCVT_UNITS < 0) ? SIMD : (CVT_ON ? FCVT_UNITS : 1);
    localparam integer CPASS   = SIMD / CU;
    localparam integer CPW     = (CPASS > 1) ? $clog2(CPASS) : 1;

    localparam integer SH_ON   = (SHIFT_UNITS != 0) ? 1 : 0;
    localparam integer SHU     = (SHIFT_UNITS < 0) ? SIMD : (SH_ON ? SHIFT_UNITS : 1);
    localparam integer SHPASS  = SIMD / SHU;
    localparam integer SHPW    = (SHPASS > 1) ? $clog2(SHPASS) : 1;
    // A LANE-RESIDENT SHIFTER'S COUNT *IS* THE LANE COUNT, so it may only live
    // inside the lane when the two widths are equal -- at ILANES 4 with
    // SHIFT_UNITS 8 it would otherwise shift four elements of eight and the
    // upper half would keep its operand. Unequal, the shifter is its own array.
    // THE EFFECTIVE WIDTHS, not the raw parameters: -1 and SIMD both mean full
    // rate, and comparing the spellings would split the shifter out of the lane
    // for a build that is the same width.
    localparam integer SH_EQ_IL   = (SHU == IU) ? 1 : 0;
    localparam integer SH_IN_LANE = (SH_ON && INT_ON && SH_EQ_IL) ? 1 : 0;
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

    // ---- the elementwise groups ------------------------------------------
    // NO UNITS MEANS NO GROUP. `FL_ON` is what makes `FLOAT_LANES = 0` fault a
    // float instruction rather than build a tier the caller did not ask for.
    wire is_falu = is_f_maj && (HAS_FALU != 0) && FL_ON && (f3 == KHF_F3_FALU);
    wire is_fsfu = is_f_maj && (FSFU_UNITS != 0) && FL_ON && (f3 == KHF_F3_FSFU);
    // FL_ON like every other float group: a machine with no float lanes has no
    // float ISA and no converters either.
    wire is_cvt_maj = is_f_maj && (f3 == KHF_F3_FCVT);
    wire is_fcvt = is_cvt_maj && FL_ON && (FCVT_UNITS != 0);
    // Everything that walks the elementwise lanes. FCVT is not on the list: it
    // is a conversion at the edge, with no lane behind it.
    wire is_fel  = is_falu || is_fsfu;
    wire is_fcmp = is_falu && ((op5 == KHF_FALU_FCMPLT)
                           || (op5 == KHF_FALU_FCMPGT)
                           || (op5 == KHF_FALU_FCMPEQ));
    // `vfma` is the one form that READS its destination, which is what the
    // third register-file port exists for.
    wire is_ffma = is_falu && (op5 == KHF_FALU_FMA);

    // The lane's operation. `khs_fp32_alu` routes the operands from it -- `vfadd`
    // and `vfsub` take their second operand on the ADDEND port -- so the decode
    // ends here rather than in an operand network.
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

    // THE INTEGER DOT AND ITS ACCUMULATOR ARE NOT BUILT. A dot product is
    // `vmul` then `vredsum`, or a multiply whose partials the scalar core sums;
    // a part needing high-rate dot carries matrix units. The whole custom-0
    // VMAC group is therefore unmapped and faults through `bad_grp`.
    wire is_mul   = is_int && (op5 == KHS_INT_MUL);
    // The ops whose vreg write comes from `lane_y` -- the result mux's DEFAULT
    // arm, so there was no bit for it until the ALU became a width of its own.
    wire is_alu   = (is_int && !is_mul) || is_bit
                 || ((SH_IN_LANE != 0) && is_shi);
    wire is_splat = is_mov && (f7 == KHS_MOV_SPLAT);
    wire is_extr  = is_mov && (f7 == KHS_MOV_EXTR);
    // NOT gated by RED_UNITS: an encoding must be RECOGNISED to be refused.
    // Gated here, a reduce on a build without the trees reads as a plain VMOV
    // and sails past `bad_grp` instead of faulting.
    wire is_rsum  = is_mov && (f7 == KHS_MOV_REDSUM);
    wire is_rmax  = is_mov && (f7 == KHS_MOV_REDMAX);

    // A store's DATA register rides in the rd field, so read port 1 serves it.
    wire [4:0] p1f = is_vst ? rdf : r1f;

    wire use_p1 = is_vst || is_int || is_bit || is_shi || is_prm
                || (is_mov && !is_splat)
                || is_fma || is_facwr
                || is_fel || is_fcvt;
    wire use_p2 = is_int || is_bit
                || (is_prm && (op4 <= KHS_PRM_PACK_S32))
                || is_fma
                || is_falu;
    // vfma reads its destination as the addend. One more port, and it is a
    // parameter on the file rather than a fixture: an integer-only build must
    // not pay a third mirror for an instruction it does not have.
    wire use_p3 = is_ffma;

    // FCVT completes in MEM: its own walk holds the stage until the staging
    // register is full, so the write port sees one retire. FALU and FSFU do NOT
    // appear here: they retire through the shadow below, FLOAT_ALAT later.
    wire wr_vreg = is_vld || is_int || is_bit || is_shi || is_prm
                 || is_splat || is_facrd || is_fcvt;
    wire wr_sc   = is_extr || is_rsum || is_rmax;

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
                 || ((is_fma || is_facz || is_facwr) && (rdf >= NACC))
                 || (is_ffold && (r1f >= NACC));
    // Custom-1 without the float tier is an unmapped opcode major, and any
    // float group this build does not implement is unmapped within it.
    wire bad_flt = (HAS_FLOAT == 0) && (x_instr[6:0] == KHF_OPCODE);
    // `vfredsum` is NOT BUILT and therefore faults: the fold combines the
    // partials WITHIN each slot, crossing them is a second pass that does not
    // exist, and returning slot 0 alone would be a plausible wrong answer.
    wire bad_fgrp = is_f_maj && !(is_fma || is_facz || is_facrd || is_facwr
                                  || is_falu || is_fsfu || is_fcvt);
    // ONE FLOAT TYPE. `funct7[1:0]` still carries it so the field stays where
    // the integer tier's element type is, and every value but f32 is unmapped
    // rather than a silent reinterpretation.
    wire bad_fet = is_f_maj && !is_facz && (et != KHF_FT_F32);
    // A LANE INDEX WIDER THAN THE BUILD MUST FAULT, NOT ALIAS. `m_lane` is
    // r2f[LAW-1:0], so `vextr x, v, 5` on a 4-lane build silently read lane 1 --
    // the same binary answering differently on a narrower machine, which is
    // exactly what "the ISA knows no width" has to rule out. The ENCODING still
    // allows 0..31; what a build carries is what this refuses.
    wire bad_lane = is_extr && (r2f >= SIMD);
    wire bad_et  = (is_int || is_shi) && (et == 2'd3);
    // A WIDTH AT ZERO REFUSES ITS ENCODINGS. This is what makes "not built"
    // checkable rather than a description: the alternative is a decode with no
    // datapath, which writes whatever a neighbouring arm of the result mux held.
    // AN UNUSED OPERATION SLOT IN A BUILT GROUP MUST FAULT TOO. Each of these
    // decodes has a `default` arm, so op5 above the table's last entry landed
    // on rsqrt, on FMA and on i2f rather than being refused.
    wire bad_fop = (is_fsfu && (op5 > KHF_FSFU_FRSQRT))
                 || (is_falu && (op5 > KHF_FALU_FCMPEQ))
                 || (is_cvt_maj && (op5 > KHF_FCVT_FCVT_I2F));
    wire bad_cfg = (is_shi && (SHIFT_UNITS == 0))
                 || (is_prm && (PERM_UNITS == 0))
                 || ((is_int || is_bit) && (ILANES == 0))
                 || ((is_rsum || is_rmax) && (RED_UNITS == 0))
                 || bad_fop
                 || (is_mul && (et >= KHS_ET_S32));
    // `is_mac` is GONE from this list: the dot group is unmapped, so it faults.
    wire bad_grp = !(is_vld || is_vst || is_int || is_bit || is_shi
                   || is_mov || is_prm
                   || is_fma || is_facz || is_facrd || is_facwr
                   || is_falu || is_fsfu || is_fcvt);
    assign x_illegal = x_valid && (bad_reg || bad_et || bad_cfg || bad_grp
                                   || bad_flt || bad_fgrp || bad_fet
                                   || bad_lane);

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
    reg         m_is_vld, m_is_vst;
    reg         m_is_mul, m_is_splat, m_is_alu;
    reg         m_is_extr, m_is_rsum, m_is_rmax, m_is_prm;
    reg         m_is_fma, m_is_fmsub, m_is_facz, m_is_facrd, m_is_facwr;
    reg         m_is_ffold;
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
    // them; separating FLANES from FSLOTS is what makes "8 int + 4 float"
    // expressible at all.
    wire [VW-1:0]        facc_rd_v;
    wire                 f_busy, f_done, f_iss, f_sweep;
    wire                 f_sw_hold, f_rd_hold, f_inflight, f_pass_hold;
    wire [FPW-1:0]       f_idx;
    wire [32*FLANES_W-1:0] fold_part_v;

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
    wire                 prm_hold, shf_hold, il_hold, cvt_hold;
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
                  || prm_hold || shf_hold || il_hold || cvt_hold;

    // A FLOAT ACCUMULATE IS STILL IN FLIGHT FIFTEEN CYCLES AFTER IT RETIRES.
    // Folding before it lands drops it AND captures it as a fold step, because
    // the fold gates the partial writes off. `vfmacc` is deliberately not on
    // the list: rotation is what lets one issue every cycle.
    wire wait_facc = is_facz || is_facwr || is_facrd;
    wire hz_facc = x_valid && wait_facc && (f_inflight || (m_valid && m_is_fma));

    // A REGISTER WITH A FLOAT IN FLIGHT IS NOT READABLE AND NOT WRITABLE. The
    // unit has one PC and no waves, so there is nothing to switch to while a
    // deep result is outstanding -- the scoreboard is what replaces the SIMT
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
    // The cost is real and is the next thing to fix: II is FLOAT_ALAT + passes,
    // 8 cycles at full width, so elementwise float is latency-bound rather than
    // throughput-bound until the writeback becomes per-pass with a per-element
    // write enable on the register file.
    wire hz_el = x_valid
               && ((use_p1 && el_busy_eff[p1f[VAW-1:0]])
                || (use_p2 && el_busy_eff[r2f[VAW-1:0]])
                || ((use_p3 || wr_vreg || is_fel)
                    && el_busy_eff[rdf[VAW-1:0]])
                // FCVT TOO, and not because it shares `stage_r`: its walk is
                // ~8 cycles against the elementwise tier's ~15, so a converter
                // issued after a float RETIRES BEFORE IT. `el_inflight` alone
                // leaves the one-cycle window `el_busy_eff` exists to close:
                // the float is still in MEM and has not reached the pipe yet.
                || ((is_fel || is_fcvt)
                    && (el_inflight || (m_valid && m_is_fel))));
    // `el_soon` empties MEM so the retiring float owns the write port.
    assign stall = hz_raw | hz_wb | hz_spad | hz_stretch | hz_fold
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
            // `vmul`'s stretch belongs to the FULL-WIDTH lane array only. At a
            // narrower width the walk runs one count longer instead, and a
            // stretch on top of it would hold a stage the walk already holds.
            m_left      <= x_go && (((IPASS == 1) && is_mul)
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
            m_is_mul    <= is_mul;
            m_is_alu    <= is_alu;
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

    // ---- the integer IM lane array, as a width -----------------------------
    // A LANE IS ONE IM UNIT: ALU and multiply share one operand path and one
    // result path, so `ILANES` sets both counts. Three branches, and the middle
    // one is the only place the sequencing logic exists.
    //
    // `vmul` FIRES ON ITS FIRST CYCLE ONLY. The second cycle reads the
    // registered products, and re-asserting would recompute them from operands
    // the stretch is holding anyway.
    wire [VW-1:0] lane_y, lane_mul_lo;
    wire          mul_en;

    genvar L;
    generate
    if (INT_ON == 0) begin : g_lane_none
        assign il_hold     = 1'b0;
        assign mul_en      = 1'b0;
        assign lane_y      = {VW{1'b0}};
        assign lane_mul_lo = {VW{1'b0}};
    end else if (IPASS == 1) begin : g_lane_full
        // The plain array: one lane per element, no counter, no staging.
        assign il_hold = 1'b0;
        assign mul_en  = m_valid && m_is_mul && m_left;
        for (L = 0; L < SIMD; L = L + 1) begin : g_lane
            khs_lane #(.HAS_SHIFT(SH_IN_LANE), .HAS_SHROUND(HAS_SHROUND),
                       .USE_DSP(USE_DSP)) u_lane (
                .clk(clk),
                .a(v1[32*L +: 32]), .b(v2[32*L +: 32]), .et(m_et),
                .alu_op(m_alu_op), .cmp_sub(m_cmp_sub), .alu_sat(m_alu_sat),
                .sh_rot(m_sh_rot), .sh_keep(m_sh_keep), .sh_rmask(m_sh_rmask),
                .sh_arith(m_sh_arith), .sh_left(m_sh_left),
                .sh_round(m_sh_round), .el_mask(m_el_mask),
                .y(lane_y[32*L +: 32]),
                .mul_en(mul_en),
                .mul_lo(lane_mul_lo[32*L +: 32])
            );
        end
    end else begin : g_lane_walk
        // Unit u on pass p serves element p*IU + u: a SIMD:1 operand mux per
        // unit KEPT, not per unit removed -- an earlier costing had that inverted.
        reg  [IPW:0] il_q;
        // `vmul` WALKS TOO, and it needs one cycle beyond its last pass to read
        // the registered products -- which is the stretch `m_left` provides.
        wire in_int  = m_valid && (m_is_alu || m_is_mul);
        // EVERY PASS IS STAGED AND THE WALK DRAINS, which is what the shifter
        // and the permute do. Keeping the last pass LIVE instead saved a cycle
        // and IU elements of register, and cost correctness: it ties the write
        // cycle to the counter, so any OTHER `hz_fold` contributor lets the
        // write fire on a cycle when the live units hold the wrong pass. That
        // read back as X in exactly the top 32*IU bits of the destination.
        // `iss_op` presents operands; `issuing` holds MEM. They differ for the
        // MULTIPLY by one count: `mcap` writes its last pass on the edge that
        // ENDS il_q == IPASS, so `mst` is only complete the cycle after, where
        // the ALU's `yst` is complete already.
        wire        iss_op  = (il_q < IPASS[IPW:0]);
        wire [IPW:0] il_end = m_is_mul ? (IPASS[IPW:0] + 1'b1) : IPASS[IPW:0];
        wire        issuing = (il_q < il_end);
        // NOT `!hz_fold`: that contains this signal and reading it closes a
        // combinational loop -- the trap `f_pass_hold` records.
        assign il_hold = in_int && issuing;
        assign mul_en  = in_int && m_is_mul && iss_op;
        wire [IPW-1:0] il_pass = il_q[IPW-1:0];

        // RESET ON COMPLETION: back-to-back ops hold `in_int` across the
        // boundary, so the second would start at the first's finishing count.
        always @(posedge clk) begin
            if (!resetn || !in_int || m_complete) begin
                il_q <= {(IPW+1){1'b0}};
            end
            else if (issuing) begin
                il_q <= il_q + 1'b1;
            end
        end

        // THE PRODUCT BELONGS TO THE PASS THAT PRESENTED ITS OPERANDS. `mul_lo`
        // is registered, so `mcap` -- a delayed `mul_en` -- says a product is on
        // the output now, and `il_pass_d` says which pass it came from.
        reg [IPW-1:0] il_pass_d;
        reg           mcap;
        always @(posedge clk) begin
            il_pass_d <= il_pass;
            mcap      <= mul_en;
        end

        wire [32*IU-1:0] u_y, u_mul;
        for (L = 0; L < IU; L = L + 1) begin : g_ilane
            wire [LAW-1:0] elem = il_pass * IU + L;
            khs_lane #(.HAS_SHIFT(SH_IN_LANE), .HAS_SHROUND(HAS_SHROUND),
                       .USE_DSP(USE_DSP)) u_lane (
                .clk(clk),
                .a(v1[32*elem +: 32]), .b(v2[32*elem +: 32]), .et(m_et),
                .alu_op(m_alu_op), .cmp_sub(m_cmp_sub), .alu_sat(m_alu_sat),
                .sh_rot(m_sh_rot), .sh_keep(m_sh_keep), .sh_rmask(m_sh_rmask),
                .sh_arith(m_sh_arith), .sh_left(m_sh_left),
                .sh_round(m_sh_round), .el_mask(m_el_mask),
                .y(u_y[32*L +: 32]),
                .mul_en(mul_en),
                .mul_lo(u_mul[32*L +: 32])
            );
        end

        // BOTH STAGING REGISTERS ARE FULL WIDTH and both are complete at
        // `il_q == IPASS`: the ALU's pass p lands on the cycle it issues, the
        // multiply's one cycle later, and the drain step covers the difference.
        reg [VW-1:0] yst, mst;
        integer yk;
        always @(posedge clk) begin
            if (in_int && iss_op) begin
                for (yk = 0; yk < IU; yk = yk + 1) begin
                    yst[32*(il_pass*IU + yk) +: 32] <= u_y[32*yk +: 32];
                end
            end
            if (mcap) begin
                for (yk = 0; yk < IU; yk = yk + 1) begin
                    mst[32*(il_pass_d*IU + yk) +: 32] <= u_mul[32*yk +: 32];
                end
            end
        end
        assign lane_y      = yst;
        assign lane_mul_lo = mst;
    end
    endgenerate

    wire [VW-1:0] mul_res = lane_mul_lo;

    // ---- the shape constraints, enforced at ELABORATION --------------------
    // FLANES MUST DIVIDE SIMD. Three does not: `PASSES` truncates, the walk
    // covers all but one element, and it still elaborates, synthesises and
    // reports an Fmax while failing the bench 10 of 66. Instantiating a module
    // that does not exist kills the build at elaboration with a name that says
    // which rule broke -- sb_nsu does the same for SDW > FW.
    generate
    if ((HAS_FLOAT != 0) && FL_ON
        && ((FLANES * PASSES) != FSLOTS)) begin : g_bad_fl
        khs_unit_requires_FLOAT_LANES_to_divide_SIMD u_bad ();
    end
    // A float GROUP asked for with no units. `FLOAT_LANES = 0` is now the only
    // spelling of "no float tier", so the groups must not be left switched on.
    if (!FL_ON && ((FSFU_UNITS != 0) || (HAS_FACC != 0)))
    begin : g_bad_fl0
        khs_unit_float_groups_need_FLOAT_LANES_nonzero u_bad ();
    end
    if ((HAS_FLOAT != 0) && (HAS_FACC != 0) && FL_ON
        && (((NPART / PASSES) * PASSES) != NPART)) begin : g_bad_np
        khs_unit_requires_PASSES_to_divide_NPART u_bad ();
    end
    // `> 0`, not `!= 0`: -1 is full rate and never a divisibility question.
    if ((SHIFT_UNITS > 0)
        && ((((SIMD / SHIFT_UNITS) * SHIFT_UNITS) != SIMD)
            || (SHIFT_UNITS > SIMD))) begin : g_bad_shu
        khs_unit_requires_SHIFT_UNITS_to_divide_SIMD u_bad ();
    end
    if ((ILANES > 0)
        && ((((SIMD / ILANES) * ILANES) != SIMD)
            || (ILANES > SIMD))) begin : g_bad_il
        khs_unit_requires_ILANES_to_divide_SIMD u_bad ();
    end
    if ((PERM_UNITS > 0)
        && ((((SIMD / PERM_UNITS) * PERM_UNITS) != SIMD)
            || (PERM_UNITS > SIMD))) begin : g_bad_pu
        khs_unit_requires_PERM_UNITS_to_divide_SIMD u_bad ();
    end
    if ((ILANES < -1) || (SHIFT_UNITS < -1) || (PERM_UNITS < -1)
        || (RED_UNITS < -1) || (FLOAT_LANES < -1) || (FSFU_UNITS < -1))
    begin : g_bad_neg
        khs_unit_a_width_below_minus_one_is_not_a_width u_bad ();
    end
    // NO RULE TIES THE SHIFTER TO THE LANES. Unequal widths simply put the
    // shifter in its own array, which reads the register file directly -- so a
    // shift-only machine (ILANES 0, SHIFT_UNITS nonzero) is a legal build.
    if ((HAS_SHROUND != 0) && (SHIFT_UNITS == 0)) begin : g_bad_shr
        khs_unit_HAS_SHROUND_requires_SHIFT_UNITS_nonzero u_bad ();
    end
    if ((FCVT_UNITS > 0)
        && ((((SIMD / FCVT_UNITS) * FCVT_UNITS) != SIMD)
            || (FCVT_UNITS > SIMD))) begin : g_bad_cu
        khs_unit_requires_FCVT_UNITS_to_divide_SIMD u_bad ();
    end
    if (FCVT_UNITS < -1) begin : g_bad_cun
        khs_unit_FCVT_UNITS_below_minus_one_is_not_a_width u_bad ();
    end
    if ((HAS_FLOAT != 0) && (SEED_N != 0)
        && ((((FSLOTS / SEED_N) * SEED_N) != FSLOTS)
            || (SEED_N > FLANES))) begin : g_bad_sf
        khs_unit_requires_FSFU_UNITS_to_divide_SIMD_and_not_exceed_FLANES u_bad ();
    end
    endgenerate

    // ================= the elementwise float unit ==========================
    // FALU and FSFU. The arithmetic is `khs_fp32_alu`'s; what this adds is a
    // PASS WALK over the elements and a RETIRE PATH, because a result that is
    // FLOAT_ALAT cycles deep cannot be written in MEM.
    //
    // ONE WRITE PER INSTRUCTION, NOT ONE PER PASS. Each pass's FLANES results
    // are placed into a staging register as they return, and the register is
    // written to the file when the last pass lands -- so the file needs no
    // per-lane write enable and the port is claimed once.
    generate
    if ((HAS_FLOAT != 0) && FL_ON
        && ((HAS_FALU != 0) || (FSFU_UNITS != 0))) begin : g_fel
        localparam integer PW_N = FSLOTS / FLANES;
        // The seed walk, which is a DIFFERENT number of passes over the same
        // elements whenever the seed units are fewer than the FMA units.
        localparam integer PS_N = FSLOTS / SEED_U;
        localparam integer PMAX = (PS_N > PW_N) ? PS_N : PW_N;
        localparam integer EPSW = (PMAX > 1) ? $clog2(PMAX) : 1;
        // The FMA walk's own width. Placing an FMA pass with the SEED walk's
        // index builds a wider slot decode than the walk can reach.
        localparam integer EPSW_F = (PW_N > 1) ? $clog2(PW_N) : 1;

        reg  [EPSW-1:0] iss_pass;
        // NOT `PS_N[EPSW-1:0]`. A parameter sliced to the pass index's own width
        // is 4 truncated to two bits, which is ZERO; the constants stay full
        // width and only the difference is truncated.
        wire [EPSW-1:0] last_pass = m_is_fsfu ? (PS_N - 1) : (PW_N - 1);
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
        // KohakuMPE's own FP32 array. `vec_alu` is NOT instantiated on this
        // path and the vector core is untouched -- it keeps computing E8M15.
        khs_fp32_alu #(.VW(VW), .FLANES(FLANES), .FSFU_UNITS(SEED_N),
                       .PSW(EPSW), .ALAT(FLOAT_ALAT))
        u_falu (
            .clk(clk), .rst(!resetn),
            .in_valid(el_live), .op(m_fop), .is_cmp(m_is_fcmp),
            .pass(iss_pass),
            .v1(v1), .v2(v2), .vd(v3),
            .out_valid(), .out(falu_out)
        );

        // The shadow. It carries what the lane array does not -- destination,
        // pass, and whether this is the pass that finishes the instruction --
        // and it MUST be the array's exact depth or a result lands on the wrong
        // register with no witness.
        reg [FLOAT_ALAT:1]  sh_v, sh_last;
        // WHICH WALK THIS RESULT CAME OFF. A seed's units are a different count
        // from an FMA's, so the placement below cannot read the live decode: by
        // then it belongs to whatever issued FLOAT_ALAT cycles later.
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
                sh_sfu[1]  <= m_is_fsfu;
                sh_wa[1]   <= m_vd;
                sh_pass[1] <= iss_pass;
                for (si = 2; si <= FLOAT_ALAT; si = si + 1) begin
                    sh_v[si]    <= sh_v[si-1];
                    sh_last[si] <= sh_last[si-1];
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
        always @(posedge clk) begin
            if (ret_v) begin
                for (pl = 0; pl < FLANES; pl = pl + 1) begin
                    if (!sh_sfu[FLOAT_ALAT]) begin
                        stage_r[32*(sh_pass[FLOAT_ALAT][EPSW_F-1:0]*FLANES + pl)
                                +: 32] <= falu_out[32*pl +: 32];
                    end
                    else if (pl < SEED_U) begin
                        stage_r[32*(sh_pass[FLOAT_ALAT]*SEED_U + pl) +: 32]
                            <= falu_out[32*pl +: 32];
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
    // FSLOTS binary32 elements across FLANES lanes, PASSES of them to a lane,
    // accumulating into NPART rotating partials. The rotation is what makes
    // II = 1 possible over a FLOAT_ALAT-deep lane; NPART and PASSES are
    // architectural, because both change the answers.
    //
    // NOTHING CONVERTS. A partial is a binary32 word, so `vfaccwr` seeds from
    // the register itself and `vfaccrd` returns the folded words unchanged --
    // the two walked `vec_cvt` instances and the 48-bit subnormal shifter they
    // hung on the end of a 256-cycle instruction are both gone.
    generate
    if ((HAS_FLOAT != 0) && (HAS_FACC != 0) && FL_ON) begin : g_float
        localparam [1:0] ST_IDLE = 2'd0, ST_RUN = 2'd1, ST_CAP = 2'd2;
        localparam [1:0] ST_DONE = 2'd3;
        localparam [31:0] F32_ONE = 32'h3F80_0000;

        localparam integer PSW = (PASSES > 1) ? $clog2(PASSES) : 1;
        localparam integer NP_EFF = NPART / PASSES;

        wire [32*FLANES-1:0] part_rd, lane_out;
        // Every lane is the same depth, so one valid speaks for all of them.
        wire [FLANES-1:0]    lane_ov;
        wire                 lane_ovld = lane_ov[0];
        reg  [32*FLANES-1:0] total;
        reg  [VW-1:0]        packed_out;
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

        wire [32*FLANES-1:0] a_sl = v1[32*FLANES*iss_pass +: 32*FLANES];
        wire [32*FLANES-1:0] b_sl = v2[32*FLANES*iss_pass +: 32*FLANES];

        // ---- vfaccz / vfaccwr : sweep the partials --------------------------
        // NO FILL STATE. The seed IS the source register, and the read port is
        // frozen while `f_sw_hold` stalls, so the sweep reads `v1` directly.
        reg [1:0] sw_st;
        reg       do_z, do_s;

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
                                 do_s  <= 1'b1;
                                 sw_st <= ST_RUN;
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

        // ---- vfaccrd : fold the partials, then place them -------------------
        // ONE CAPTURE CYCLE PER PASS, not one per slot: a partial is already a
        // binary32 word, so the whole FLANES-wide `total` lands at once.
        reg [1:0]     rd_st;
        reg [PSW-1:0] fold_pass;
        wire          folding = (rd_st == ST_RUN);
        wire          fold_go = (rd_st == ST_IDLE) && m_valid && m_is_ffold;
        wire          fold_last = (PASSES == 1)
                                || (fold_pass == (PASSES[PSW-1:0] - 1'b1));
        wire          fold_next = (rd_st == ST_CAP) && !fold_last;

        assign f_rd_hold  = m_valid && m_is_ffold && (rd_st != ST_DONE);
        assign facc_rd_v  = packed_out;

        integer fk;
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
                        rd_st <= ST_CAP;
                    end
                    ST_CAP: begin
                        for (fk = 0; fk < FLANES; fk = fk + 1) begin
                            packed_out[32*(fold_pass*FLANES + fk) +: 32]
                                <= total[32*fk +: 32];
                        end
                        if (fold_last) begin
                            rd_st <= ST_DONE;
                        end
                        else begin
                            rd_st     <= ST_RUN;
                            fold_pass <= fold_pass + 1'b1;
                        end
                    end
                    default: rd_st <= ST_IDLE;
                endcase
            end
        end

        // The lane's depth, as a shadow: `wait_facc` reads it in EX.
        reg [FMA_ALAT-1:0] fpipe;
        always @(posedge clk) begin
            if (!resetn) begin
                fpipe <= {FMA_ALAT{1'b0}};
            end
            else begin
                fpipe <= {fpipe[FMA_ALAT-2:0], acc_fire};
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

        khs_ffold #(.NPART(NP_EFF), .ALAT(FMA_ALAT)) u_ffold (
            .clk(clk), .resetn(resetn),
            .start(fold_go || fold_next), .busy(f_busy), .done(f_done),
            .part_idx(f_idx_e), .iss_valid(f_iss), .iss_raw()
        );

        genvar S;
        for (S = 0; S < FLANES; S = S + 1) begin : g_flane
            // A fold's addend is the running total; an accumulate's is the
            // partial the counter selected.
            wire [31:0] c_sel = folding ? total[32*S +: 32]
                                        : part_rd[32*S +: 32];
            // The subtract flips the operand's SIGN BIT: negating a float is
            // one bit, not a subtractor.
            wire [31:0] b_neg = b_sl[32*S +: 32]
                              ^ (m_is_fmsub ? 32'h8000_0000 : 32'd0);

            // ONE FMA SERVES BOTH PATHS. An accumulate is a*b + partial; a fold
            // is partial*1.0 + total, taking the partial off the accumulator's
            // own fold port. `.op` unconnected is `z` in simulation and OP_MOV
            // in synthesis -- a PASS-THROUGH, which failed `vfaccrd` 13 checks.
            rv_fpu u_fl (
                .clk(clk), .rst(!resetn),
                .in_valid(acc_fire | f_iss), .op(KHS_FOP_FMA),
                .a(f_iss ? fold_part_v[32*S +: 32] : a_sl[32*S +: 32]),
                .b(f_iss ? F32_ONE : b_neg),
                .c(c_sel),
                .out_valid(lane_ov[S]),
                .y(lane_out[32*S +: 32]), .out_pred()
            );
        end

        always @(posedge clk) begin
            if (fold_go || fold_next) begin
                total <= {(32*FLANES){1'b0}};
            end
            else if (folding && lane_ovld) begin
                total <= lane_out;
            end
        end

        // THE SEED SLICE FOR THE PASS THE SWEEP IS ON: the accumulator writes
        // FLANES partials at a time, so the source register is read a slice at
        // a time and the sweep index picks it.
        wire [32*FLANES-1:0] seed_sl =
            v1[32*FLANES*(sweep_idx[PSW-1:0]) +: 32*FLANES];

        // THE FOLD WALKS ONE PASS'S PARTIALS, not all of them. Element e's chain
        // is the turns congruent to its pass modulo PASSES, so a flat fold over
        // NPART would sum ACROSS elements.
        wire [FPW-1:0] fold_addr = (PASSES == 1)
                        ? f_idx
                        : {f_idx_e, fold_pass};

        khs_facc #(.SLOTS(FLANES), .NACC(NACC), .NPART(NPART),
                   .ALAT(FMA_ALAT), .PASSES(PASSES))
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
        assign f_iss  = 1'b0;
        assign f_sw_hold = 1'b0; assign f_rd_hold = 1'b0;
        assign f_pass_hold = 1'b0;
        assign f_inflight = 1'b0;
        assign f_idx  = {(NPART>1 ? $clog2(NPART) : 1){1'b0}};
        assign fold_part_v = {(32*FLANES_W){1'b0}};
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
    if ((SH_ON == 0) || (SH_IN_LANE != 0)) begin : g_shf_lane
        // Either not built at all, or full width and living inside the lane.
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

    // ---- the converters ----------------------------------------------------
    // BOTH DIRECTIONS ARE SIMD ELEMENTS WIDE, which is what makes one walk
    // serve both: f2i reads SIMD floats and writes SIMD int32, i2f the reverse.
    // There is no `f2f`, because there is no second float format.
    wire [VW-1:0] cvt_res;
    generate
    if (!FL_ON || !CVT_ON) begin : g_cvt_none
        assign cvt_hold = 1'b0;
        assign cvt_res  = {VW{1'b0}};
    end else begin : g_cvt_walk
        // THE WALK RUNS ONE TICK PAST ITS LAST PASS, because `khs_fcvt` is a
        // cycle deep: pass CPASS-1 presents its operand on the tick before its
        // result exists, so the terminal count is CPASS+1 and not CPASS.
        localparam integer CLAST = CPASS + 1;
        reg [CPW+1:0] cv_q;
        wire        in_mem  = m_valid && m_is_fcvt && !m_left;
        wire        issuing = (cv_q != CLAST[CPW+1:0]);
        // NOT `!hz_fold`: it contains this signal, and reading it closes the
        // combinational loop `f_pass_hold` records.
        assign cvt_hold = in_mem && issuing;

        always @(posedge clk) begin
            if (!resetn) begin
                cv_q <= {(CPW+2){1'b0}};
            end
            else if (in_mem) begin
                cv_q <= issuing ? (cv_q + 1'b1) : {(CPW+2){1'b0}};
            end
            else begin
                cv_q <= {(CPW+2){1'b0}};
            end
        end

        // ISSUE ONLY WHILE THERE IS A PASS LEFT TO PRESENT. The extra tick is
        // for draining, so it must not drive a CU-th element that does not
        // exist -- `cel` would wrap and re-convert element 0.
        wire cv_fire = in_mem && (cv_q < CPASS[CPW+1:0]);
        // CLAMPED, NOT `cv_q[CPW-1:0]`. At CU = SIMD, CPASS is 1 and CPW is 1,
        // so the drain tick puts 1 in a field whose only legal value is 0 and
        // `cel` reaches SIMD -- an out-of-range part select that reads X.
        wire [CPW-1:0] cv_ix = cv_fire ? cv_q[CPW-1:0] : {CPW{1'b0}};

        wire [32*CU-1:0] cvy;
        genvar CV;
        for (CV = 0; CV < CU; CV = CV + 1) begin : g_cvunit
            wire [LAW-1:0] cel = cv_ix * CU + CV;
            khs_fcvt u_cvt (
                .clk(clk), .op(m_fcvt_op), .a(v1[32*cel +: 32]),
                .y(cvy[32*CV +: 32])
            );
        end

        // THE PASS THAT PRESENTED THE OPERAND, not the one in flight when the
        // result lands -- the converter is registered, so the staging index has
        // to be delayed to match it (configurable-widths.md, "Results that
        // arrive late").
        reg [CPW+1:0] cv_d;
        reg           cvw_d;
        always @(posedge clk) begin
            if (!resetn) begin
                cvw_d <= 1'b0;
            end
            else begin
                cvw_d <= cv_fire;
            end
            cv_d <= cv_q;
        end

        // ONE STAGING REGISTER AND ONE DRAIN STEP, as the shifter and permute
        // do: khs_vregfile has no per-element write enable.
        reg [VW-1:0] cvst;
        integer ck;
        always @(posedge clk) begin
            if (cvw_d) begin
                for (ck = 0; ck < CU; ck = ck + 1) begin
                    cvst[32*(cv_d[CPW-1:0]*CU + ck) +: 32] <= cvy[32*ck +: 32];
                end
            end
        end
        assign cvt_res = cvst;
    end
    endgenerate

    // ---- the cross-lane network -------------------------------------------
    // PU output words a pass, SIMD/PU passes, into a staging register. The
    // walk holds MEM through `hz_fold`, so the instruction retires once.
    wire [32*PU-1:0] perm_y;
    wire [PPW-1:0]   prm_pass;
    wire [VW-1:0]    prm_res;

    generate
    if (PERM_ON == 0) begin : g_prm_none
        assign prm_pass = {PPW{1'b0}};
        assign prm_hold = 1'b0;
        assign prm_res  = {VW{1'b0}};
        assign perm_y   = {(32*PU){1'b0}};
    end else begin : g_prm_on
    // PU, not PERM_UNITS: the effective width, so -1 resolves once here.
    khs_perm #(.SIMD(SIMD), .HAS_PERM(1), .UNITS(PU),
               .PSW(PPW)) u_perm (
        .v1(v1), .v2(v2), .op4(m_prm_op4), .idx(m_prm_idx),
        .pass(prm_pass), .y(perm_y)
    );

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
    end
    endgenerate

    // Reductions: a tree, not a loop -- a chain that carries a value between
    // iterations synthesises as exactly that chain, which cost the accumulator
    // ~68 MHz once (mx_fpacc.v).
    //
    // GATED, because it was not: both trees were built unconditionally, so a
    // shader that never reduces still paid two log2(SIMD)-deep trees plus their
    // PIPE registers. Not a width -- a reduction is one instruction over the
    // whole register, so there is no per-element count to walk.
    wire [31:0] red_sum, red_max;
    generate
    if (RED_UNITS != 0) begin : g_red
        khs_reduce #(.SIMD(SIMD), .PIPE(RED_PIPE)) u_red (
            .clk(clk), .v(v1), .sum(red_sum), .max(red_max)
        );
    end else begin : g_no_red
        assign red_sum = 32'd0;
        assign red_max = 32'd0;
    end
    endgenerate

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
        else if (m_is_splat) begin
            vres = {SIMD{m_xdata}};
        end
        else if (m_is_prm) begin
            vres = prm_res;
        end
        // THE ARM WHOSE ABSENCE WAS THE DEFECT: `m_is_fcvt` was registered in
        // MEM and never read here, so every vfcvt returned the integer lane.
        else if (m_is_fcvt) begin
            vres = cvt_res;
        end
        else if (m_is_mul) begin
            vres = mul_res;
        end
        // ONLY WHEN THE SHIFTER LEFT THE LANE. Inside the lane this is a
        // constant false and the arm is trimmed, so the chain keeps the source
        // count every earlier row measured.
        else if ((SH_ON != 0) && (SH_IN_LANE == 0) && (m_alu_op == OP_SH)) begin
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
            $display("%0t khs_unit: instruction %08h is not built in this configuration (SIMD %0d ilanes %0d shift %0d perm %0d red %0d flanes %0d fsfu %0d)",
                     $time, x_instr, SIMD, ILANES, SHIFT_UNITS, PERM_UNITS,
                     RED_UNITS, FLOAT_LANES, FSFU_UNITS);
        end
        if (x_valid && x_misalign) begin
            $display("%0t khs_unit: vector address %08h is not a multiple of %0d bytes",
                     $time, x_addr, VBYTES);
        end
    end
`endif

endmodule

`default_nettype wire

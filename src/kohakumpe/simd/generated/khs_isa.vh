// GENERATED from tests/pe/tools/rv_simd_isa.py -- DO NOT EDIT.
// Regenerate with `python tests/pe/tools/rv_simd_emit.py`; the ISA test
// regenerates and compares, so a hand edit here fails rather than
// quietly disagreeing with the assembler and the golden model.
//
// The decode is STRUCTURED, not a flat case: funct3 names the group,
// funct7[1:0] is the element type for every typed group, and the
// datapath reads that pair straight off the instruction word. A flat
// 10-bit case would put the element width behind a decoder on the
// operand path, which is where this core can least afford one.

localparam [6:0] KHS_OPCODE = 7'h0b;   // RISC-V custom-0
localparam [6:0] KHF_OPCODE = 7'h2b;   // custom-1, reserved to the float tiers

// funct3: the group
localparam [2:0] KHS_F3_VLD   = 3'd0;
localparam [2:0] KHS_F3_VST   = 3'd1;
localparam [2:0] KHS_F3_VINT  = 3'd2;
localparam [2:0] KHS_F3_VBIT  = 3'd3;
localparam [2:0] KHS_F3_VSHI  = 3'd4;
localparam [2:0] KHS_F3_VMAC  = 3'd5;
localparam [2:0] KHS_F3_VMOV  = 3'd6;
localparam [2:0] KHS_F3_VPRM  = 3'd7;

// funct7[1:0]: the element type of a typed group
localparam [1:0] KHS_ET_S8  = 2'd0;   // 8-bit elements
localparam [1:0] KHS_ET_S16 = 2'd1;   // 16-bit elements
localparam [1:0] KHS_ET_S32 = 2'd2;   // 32-bit elements

// funct3: the float tier's groups, on custom-1
localparam [2:0] KHF_F3_FMAC  = 3'd0;
localparam [2:0] KHF_F3_FRED  = 3'd1;
localparam [2:0] KHF_F3_FCVT  = 3'd2;
localparam [2:0] KHF_F3_FALU  = 3'd3;
localparam [2:0] KHF_F3_FSFU  = 3'd4;

// funct7[1:0]: the float element type. FP32 is the only compute type,
// so every other value of the field is an unmapped encoding.
localparam [1:0] KHF_FT_F32 = 2'd1;

// rv_fpu's own opcodes, forwarded by khs_fp32_alu. A FALU instruction
// maps onto exactly one of these; a seed's low two bits are
// khs_fp32_sfu's function select.
localparam [4:0] KHS_FOP_MOV    = 5'd0;
localparam [4:0] KHS_FOP_NEG    = 5'd1;
localparam [4:0] KHS_FOP_ABS    = 5'd2;
localparam [4:0] KHS_FOP_ADD    = 5'd3;
localparam [4:0] KHS_FOP_SUB    = 5'd4;
localparam [4:0] KHS_FOP_MUL    = 5'd5;
localparam [4:0] KHS_FOP_FMA    = 5'd6;
localparam [4:0] KHS_FOP_FNMA   = 5'd7;
localparam [4:0] KHS_FOP_MAX    = 5'd8;
localparam [4:0] KHS_FOP_MIN    = 5'd9;
localparam [4:0] KHS_FOP_SEL    = 5'd10;
localparam [4:0] KHS_FOP_CMPLT  = 5'd11;
localparam [4:0] KHS_FOP_CMPGT  = 5'd12;
localparam [4:0] KHS_FOP_CMPEQ  = 5'd13;
localparam [4:0] KHS_FOP_EXP2   = 5'd16;
localparam [4:0] KHS_FOP_LOG2   = 5'd17;
localparam [4:0] KHS_FOP_INV    = 5'd18;
localparam [4:0] KHS_FOP_RSQRT  = 5'd19;

// custom-0 funct3 = KHS_F3_VINT: funct7[6:2] is the operation
localparam [4:0] KHS_INT_ADD       = 5'd0;
localparam [4:0] KHS_INT_SUB       = 5'd1;
localparam [4:0] KHS_INT_SADD      = 5'd2;
localparam [4:0] KHS_INT_SSUB      = 5'd3;
localparam [4:0] KHS_INT_MIN       = 5'd4;
localparam [4:0] KHS_INT_MAX       = 5'd5;
localparam [4:0] KHS_INT_MUL       = 5'd6;

// custom-0 funct3 = KHS_F3_VBIT: funct7[6:0] is the operation
localparam [6:0] KHS_BIT_AND       = 7'd0;
localparam [6:0] KHS_BIT_OR        = 7'd1;
localparam [6:0] KHS_BIT_XOR       = 7'd2;
localparam [6:0] KHS_BIT_ANDN      = 7'd3;

// custom-0 funct3 = KHS_F3_VSHI: funct7[6:2] is the operation
localparam [4:0] KHS_SH_SLLI      = 5'd0;
localparam [4:0] KHS_SH_SRLI      = 5'd1;
localparam [4:0] KHS_SH_SRAI      = 5'd2;
localparam [4:0] KHS_SH_SRARI     = 5'd3;

// custom-0 funct3 = KHS_F3_VMOV: funct7[6:0] is the operation
localparam [6:0] KHS_MOV_SPLAT     = 7'd0;
localparam [6:0] KHS_MOV_EXTR      = 7'd1;
localparam [6:0] KHS_MOV_REDSUM    = 7'd2;
localparam [6:0] KHS_MOV_REDMAX    = 7'd3;

// custom-0 funct3 = KHS_F3_VPRM: funct7[6:3] is the operation, funct7[2:0] the lane index
localparam [3:0] KHS_PRM_SLDW      = 4'd0;
localparam [3:0] KHS_PRM_PACK_S16  = 4'd1;
localparam [3:0] KHS_PRM_PACK_S32  = 4'd2;
localparam [3:0] KHS_PRM_UNPKL_S8  = 4'd3;
localparam [3:0] KHS_PRM_UNPKH_S8  = 4'd4;
localparam [3:0] KHS_PRM_UNPKL_S16 = 4'd5;
localparam [3:0] KHS_PRM_UNPKH_S16 = 4'd6;

// custom-1 funct3 = KHF_F3_FMAC: funct7[6:2] is the operation
localparam [4:0] KHF_FMAC_FMACC     = 5'd0;
localparam [4:0] KHF_FMAC_FMSAC     = 5'd1;
localparam [4:0] KHF_FMAC_FACCZ     = 5'd2;
localparam [4:0] KHF_FMAC_FACCRD    = 5'd3;
localparam [4:0] KHF_FMAC_FACCWR    = 5'd4;

// custom-1 funct3 = KHF_F3_FRED: funct7[6:2] is the operation
localparam [4:0] KHF_FRED_FREDSUM   = 5'd0;

// custom-1 funct3 = KHF_F3_FCVT: funct7[6:2] is the operation
localparam [4:0] KHF_FCVT_FCVT_F2I  = 5'd0;
localparam [4:0] KHF_FCVT_FCVT_I2F  = 5'd1;

// custom-1 funct3 = KHF_F3_FALU: funct7[6:2] is the operation
localparam [4:0] KHF_FALU_FMUL      = 5'd0;
localparam [4:0] KHF_FALU_FADD      = 5'd1;
localparam [4:0] KHF_FALU_FSUB      = 5'd2;
localparam [4:0] KHF_FALU_FMA       = 5'd3;
localparam [4:0] KHF_FALU_FMIN      = 5'd4;
localparam [4:0] KHF_FALU_FMAX      = 5'd5;
localparam [4:0] KHF_FALU_FCMPLT    = 5'd6;
localparam [4:0] KHF_FALU_FCMPGT    = 5'd7;
localparam [4:0] KHF_FALU_FCMPEQ    = 5'd8;

// custom-1 funct3 = KHF_F3_FSFU: funct7[6:2] is the operation
localparam [4:0] KHF_FSFU_FEXP2     = 5'd0;
localparam [4:0] KHF_FSFU_FLOG2     = 5'd1;
localparam [4:0] KHF_FSFU_FRCP      = 5'd2;
localparam [4:0] KHF_FSFU_FRSQRT    = 5'd3;

// 77 instructions: 56 integer on custom-0, 21 float on custom-1.


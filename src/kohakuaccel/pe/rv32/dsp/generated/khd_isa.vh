// GENERATED from tests/pe/tools/rv_dsp_isa.py -- DO NOT EDIT.
// Regenerate with `python tests/pe/tools/rv_dsp_emit.py`; the ISA test
// regenerates and compares, so a hand edit here fails rather than
// quietly disagreeing with the assembler and the golden model.
//
// The decode is STRUCTURED, not a flat case: funct3 names the group,
// funct7[1:0] is the element type for every typed group, and the
// datapath reads that pair straight off the instruction word. A flat
// 10-bit case would put the element width behind a decoder on the
// operand path, which is where this core can least afford one.

localparam [6:0] KHD_OPCODE = 7'h0b;   // RISC-V custom-0
localparam [6:0] KHF_OPCODE = 7'h2b;   // custom-1, reserved to the float tiers

// funct3: the group
localparam [2:0] KHD_F3_VLD   = 3'd0;
localparam [2:0] KHD_F3_VST   = 3'd1;
localparam [2:0] KHD_F3_VINT  = 3'd2;
localparam [2:0] KHD_F3_VBIT  = 3'd3;
localparam [2:0] KHD_F3_VSHI  = 3'd4;
localparam [2:0] KHD_F3_VMAC  = 3'd5;
localparam [2:0] KHD_F3_VMOV  = 3'd6;
localparam [2:0] KHD_F3_VPRM  = 3'd7;

// funct7[1:0]: the element type of a typed group
localparam [1:0] KHD_ET_S8  = 2'd0;   // 8-bit elements
localparam [1:0] KHD_ET_S16 = 2'd1;   // 16-bit elements
localparam [1:0] KHD_ET_S32 = 2'd2;   // 32-bit elements

// funct3: the float tier's groups, on custom-1
localparam [2:0] KHF_F3_FMAC  = 3'd0;
localparam [2:0] KHF_F3_FRED  = 3'd1;
localparam [2:0] KHF_F3_FCVT  = 3'd2;

// funct7[1:0]: the float element type
localparam [1:0] KHF_FT_F16 = 2'd0;
localparam [1:0] KHF_FT_F32 = 2'd1;

// custom-0 funct3 = KHD_F3_VINT: funct7[6:2] is the operation
localparam [4:0] KHD_INT_ADD       = 5'd0;
localparam [4:0] KHD_INT_SUB       = 5'd1;
localparam [4:0] KHD_INT_SADD      = 5'd2;
localparam [4:0] KHD_INT_SSUB      = 5'd3;
localparam [4:0] KHD_INT_MIN       = 5'd4;
localparam [4:0] KHD_INT_MAX       = 5'd5;
localparam [4:0] KHD_INT_MUL       = 5'd6;

// custom-0 funct3 = KHD_F3_VBIT: funct7[6:0] is the operation
localparam [6:0] KHD_BIT_AND       = 7'd0;
localparam [6:0] KHD_BIT_OR        = 7'd1;
localparam [6:0] KHD_BIT_XOR       = 7'd2;
localparam [6:0] KHD_BIT_ANDN      = 7'd3;

// custom-0 funct3 = KHD_F3_VSHI: funct7[6:2] is the operation
localparam [4:0] KHD_SH_SLLI      = 5'd0;
localparam [4:0] KHD_SH_SRLI      = 5'd1;
localparam [4:0] KHD_SH_SRAI      = 5'd2;
localparam [4:0] KHD_SH_SRARI     = 5'd3;

// custom-0 funct3 = KHD_F3_VMAC: funct7[6:2] is the operation
localparam [4:0] KHD_MAC_DOT       = 5'd0;
localparam [4:0] KHD_MAC_DOTN      = 5'd1;
localparam [4:0] KHD_MAC_ACCZ      = 5'd2;
localparam [4:0] KHD_MAC_ACCRD     = 5'd3;
localparam [4:0] KHD_MAC_ACCWR     = 5'd4;

// custom-0 funct3 = KHD_F3_VMOV: funct7[6:0] is the operation
localparam [6:0] KHD_MOV_SPLAT     = 7'd0;
localparam [6:0] KHD_MOV_EXTR      = 7'd1;
localparam [6:0] KHD_MOV_REDSUM    = 7'd2;
localparam [6:0] KHD_MOV_REDMAX    = 7'd3;

// custom-0 funct3 = KHD_F3_VPRM: funct7[6:3] is the operation, funct7[2:0] the lane index
localparam [3:0] KHD_PRM_SLDW      = 4'd0;
localparam [3:0] KHD_PRM_PACK_S16  = 4'd1;
localparam [3:0] KHD_PRM_PACK_S32  = 4'd2;
localparam [3:0] KHD_PRM_UNPKL_S8  = 4'd3;
localparam [3:0] KHD_PRM_UNPKH_S8  = 4'd4;
localparam [3:0] KHD_PRM_UNPKL_S16 = 4'd5;
localparam [3:0] KHD_PRM_UNPKH_S16 = 4'd6;

// custom-1 funct3 = KHF_F3_FMAC: funct7[6:2] is the operation
localparam [4:0] KHF_FMAC_FMACC     = 5'd0;
localparam [4:0] KHF_FMAC_FMSAC     = 5'd1;
localparam [4:0] KHF_FMAC_FACCZ     = 5'd2;
localparam [4:0] KHF_FMAC_FACCRD    = 5'd3;
localparam [4:0] KHF_FMAC_FACCWR    = 5'd4;

// custom-1 funct3 = KHF_F3_FRED: funct7[6:2] is the operation
localparam [4:0] KHF_FRED_FREDSUM   = 5'd0;

// 69 instructions: 63 integer on custom-0, 6 float on custom-1.


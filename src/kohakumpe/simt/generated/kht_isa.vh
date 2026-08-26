// GENERATED from tests/pe/tools/rv_simt_isa.py -- DO NOT EDIT.
// Regenerate with `python tests/pe/tools/rv_simt_emit.py`; the ISA test
// regenerates and compares, so a hand edit here fails rather than
// quietly disagreeing with the assembler and the golden model.
//
// The decode is STRUCTURED, not a flat case: funct3 names the group and
// the datapath reads the group and its operation straight off the
// instruction word. custom-2 carries the R-type groups; custom-3 carries
// the I-type ones, because an I-type layout has no funct7 and would
// otherwise hold one instruction per group.

localparam [6:0] KHT_OPCODE  = 7'h5b;   // RISC-V custom-2, R-type groups
localparam [6:0] KHGI_OPCODE = 7'h7b;   // custom-3, I-type groups

// funct3 on custom-2: the group
localparam [2:0] KHT_F3_SALU   = 3'd0;
localparam [2:0] KHT_F3_SMOV   = 3'd1;
localparam [2:0] KHT_F3_DIV    = 3'd2;
localparam [2:0] KHT_F3_SUB    = 3'd3;
localparam [2:0] KHT_F3_VMEM   = 3'd4;
localparam [2:0] KHT_F3_FLT    = 3'd5;
localparam [2:0] KHT_F3_RSVD6  = 3'd6;
localparam [2:0] KHT_F3_RSVD7  = 3'd7;

// funct3 on custom-3: one instruction each
localparam [2:0] KHGI_F3_ADDI  = 3'd0;
localparam [2:0] KHGI_F3_ANDI  = 3'd1;
localparam [2:0] KHGI_F3_ORI   = 3'd2;
localparam [2:0] KHGI_F3_SLLI  = 3'd3;
localparam [2:0] KHGI_F3_SRLI  = 3'd4;
localparam [2:0] KHGI_F3_SRAI  = 3'd5;
localparam [2:0] KHGI_F3_BEQZ  = 3'd6;
localparam [2:0] KHGI_F3_BNEZ  = 3'd7;

// custom-2 funct3 = KHT_F3_SALU: funct7 is the operation
localparam [6:0] KHT_SALU_SADD      = 7'd0;
localparam [6:0] KHT_SALU_SSUB      = 7'd1;
localparam [6:0] KHT_SALU_SSLL      = 7'd2;
localparam [6:0] KHT_SALU_SSLT      = 7'd3;
localparam [6:0] KHT_SALU_SSLTU     = 7'd4;
localparam [6:0] KHT_SALU_SXOR      = 7'd5;
localparam [6:0] KHT_SALU_SSRL      = 7'd6;
localparam [6:0] KHT_SALU_SSRA      = 7'd7;
localparam [6:0] KHT_SALU_SOR       = 7'd8;
localparam [6:0] KHT_SALU_SAND      = 7'd9;

// custom-2 funct3 = KHT_F3_SMOV: funct7 is the operation
localparam [6:0] KHT_SMOV_S2V       = 7'd0;
localparam [6:0] KHT_SMOV_RDCTL     = 7'd1;

// custom-2 funct3 = KHT_F3_DIV: funct7 is the operation
localparam [6:0] KHT_DIV_SPLIT     = 7'd0;
localparam [6:0] KHT_DIV_JOIN      = 7'd1;
localparam [6:0] KHT_DIV_TMC       = 7'd2;
localparam [6:0] KHT_DIV_BAR       = 7'd3;

// custom-2 funct3 = KHT_F3_SUB: funct7 is the operation
localparam [6:0] KHT_SUB_SHFLXOR   = 7'd0;
localparam [6:0] KHT_SUB_BCAST     = 7'd1;
localparam [6:0] KHT_SUB_BALLOT    = 7'd2;
localparam [6:0] KHT_SUB_REDUXADD  = 7'd3;
localparam [6:0] KHT_SUB_REDUXMAX  = 7'd4;
localparam [6:0] KHT_SUB_REDUXMIN  = 7'd5;
localparam [6:0] KHT_SUB_REDUXAND  = 7'd6;
localparam [6:0] KHT_SUB_REDUXOR   = 7'd7;
localparam [6:0] KHT_SUB_VREADFIRST = 7'd8;
localparam [6:0] KHT_SUB_VLANEID   = 7'd9;

// custom-2 funct3 = KHT_F3_FLT: funct7 is the operation
localparam [6:0] KHT_FLT_VFMA      = 7'd0;
localparam [6:0] KHT_FLT_VFMUL     = 7'd1;
localparam [6:0] KHT_FLT_VFADD     = 7'd2;
localparam [6:0] KHT_FLT_VFSUB     = 7'd3;
localparam [6:0] KHT_FLT_VFEXP2    = 7'd4;
localparam [6:0] KHT_FLT_VFLOG2    = 7'd5;
localparam [6:0] KHT_FLT_VFRCP     = 7'd6;
localparam [6:0] KHT_FLT_VFRSQRT   = 7'd7;

// The vmem group packs three things into funct7, so the datapath slices
// it rather than comparing against one constant per encoding:
//     funct7 = op<<4 | scale<<2 | width
// op 0-2 take a per-lane offset from vs2; op 3-5 are LANE-LINEAR and
// take no vector operand at all, so their addresses are known at
// decode and the request count is one by construction.
localparam [2:0] KHT_MEM_OP_L    = 3'd0;   // load, sign-extended
localparam [2:0] KHT_MEM_OP_LU   = 3'd1;   // load, zero-extended
localparam [2:0] KHT_MEM_OP_S    = 3'd2;   // store
localparam [2:0] KHT_MEM_OP_LIN  = 3'd3;   // lane-linear load, signed
localparam [2:0] KHT_MEM_OP_LINU = 3'd4;   // lane-linear load, unsigned
localparam [2:0] KHT_MEM_OP_SIN  = 3'd5;   // lane-linear store
localparam [1:0] KHT_MW_B = 2'd0;
localparam [1:0] KHT_MW_H = 2'd1;
localparam [1:0] KHT_MW_W = 2'd2;

// Machine shape. The ENCODING always allows 32 registers and any lane
// index; these are what a build actually carries, so a program that
// names more FAULTS rather than aliasing.
localparam integer KHT_LANES = 8;
localparam integer KHT_WAVES = 16;
localparam integer KHT_SREGS = 32;
// A split pushes TWO entries and a join pops ONE, so a depth of D
// permits D/2 nested divergent levels. Overflow is a fault.
localparam integer KHT_IPDOM_DEPTH = 8;

// 106 instructions: 98 on custom-2, 8 on custom-3.


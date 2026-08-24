// kht_ctrl.vh -- the bit layout of the PREDECODED control word.
//
// The SIMT core has no decode stage: `instr` comes straight out of the window
// and decode, operand read, address generation and the PC update all happen in
// one cycle. The base core does not work that way -- rv_id REGISTERS its
// decoded outputs into EX -- and that is the whole of why rv_pe closes at 410
// and kht_pe did not.
//
// Adding a decode stage would cost a cycle of branch latency. Predecoding does
// not: every signal here is a pure function of the instruction word, so it is
// computed ONCE on the write path when the shader image lands and read back
// beside the instruction. Measured: four of the thirteen levels between the
// window and the per-wave PC's clock enable were spent producing `mem_store`.
//
// Included by kht_predec.v (which drives it) and kht_core.v (which reads it),
// so the two cannot disagree about the layout.

localparam integer C_SALU    = 0;
localparam integer C_SMOV    = 1;
localparam integer C_DIV     = 2;
localparam integer C_SUB     = 3;
localparam integer C_VMEM    = 4;

localparam integer C_SPLIT   = 5;
localparam integer C_JOIN    = 6;
localparam integer C_TMC     = 7;
localparam integer C_BAR     = 8;

localparam integer C_S2V     = 9;
localparam integer C_RDCTL   = 10;

localparam integer C_BALLOT  = 11;
localparam integer C_VRF     = 12;
localparam integer C_REDUX   = 13;
localparam integer C_SHFL    = 14;
localparam integer C_BCAST   = 15;
localparam integer C_LANID   = 16;

localparam integer C_MEMLIN  = 17;
localparam integer C_MEMST   = 18;

localparam integer C_SBEQZ   = 19;
localparam integer C_SBNEZ   = 20;
localparam integer C_SBR     = 21;
localparam integer C_SIMM    = 22;

localparam integer C_LUI     = 23;
localparam integer C_AUIPC   = 24;
localparam integer C_JAL     = 25;
localparam integer C_JALR    = 26;
localparam integer C_LOAD    = 27;
localparam integer C_STORE   = 28;
localparam integer C_OPIMM   = 29;
localparam integer C_OP      = 30;
localparam integer C_SYS     = 31;
localparam integer C_FENCE   = 32;

localparam integer C_ALU0    = 33;   // 4 bits: the lane ALU operation
localparam integer C_ILLEGAL = 37;
localparam integer C_VTWEN   = 38;   // class only; `rd != 0` is a raw bit
localparam integer C_VTOP2I  = 39;
localparam integer C_EXRDV   = 40;   // class only; f2_valid is not an instr bit
localparam integer C_SWEN    = 41;   // class only
localparam integer C_PERLANE = 42;   // class only
localparam integer C_LDANY   = 43;
localparam integer C_ECALL   = 44;
localparam integer C_EBRK    = 45;

// The SCALAR ALU operation, mapped from custom-2 funct7 and custom-3 funct3
// onto one encoding so the core builds ONE scalar ALU instead of two case
// statements and a mux between them.
localparam integer C_SOP0    = 46;   // 4 bits

// DOES THIS INSTRUCTION READ THE SCALAR FILE? The scalar half interlocks at
// distance 1 rather than forwarding, so the stall must fire ONLY for a real
// scalar read -- an ordinary RV32I opcode has rs1/rs2 in the vector file and
// would otherwise stall against a scalar write it never reads.
localparam integer C_RDS1    = 50;
localparam integer C_RDS2    = 51;

// G9, the float tier. `C_FOP` is the funct7 narrowed to what kht_fpu selects on,
// so the lane array never sees the instruction word.
localparam integer C_FLT     = 52;
// 4 bits: {half, op[2:0]}. Three until the FSFU seeds needed four operations
// beyond the arithmetic four, which took the last spare bit of the word -- 53
// through 59 is exactly the seven bits left, so widening this again means
// widening KHT_CW and every port that carries it.
localparam integer C_FOP0    = 53;   // 4 bits

// RV32M. In the EXISTING register-register group at funct7 = 0000001, so it
// costs no custom opcode space -- all four majors are already spoken for.
localparam integer C_IMUL    = 57;
localparam integer C_MOP0    = 58;   // 2 bits: funct3[1:0]

localparam integer KHT_CW    = 60;

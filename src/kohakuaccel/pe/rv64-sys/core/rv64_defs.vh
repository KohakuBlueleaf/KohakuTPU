// SysCore's internal encodings. NOT the RISC-V instruction encoding -- these are
// the decoded control values that travel the pipeline, and they are chosen so a
// decoder is a re-map rather than arithmetic.
//
// RV64_ALU_* DELIBERATELY MATCH `funct3` for the OP-IMM / OP group, so the
// common decode is a wire and only the SUB/SRA distinction (`funct7[5]`) and the
// PASSB form need logic.

localparam [3:0] RV64_ALU_ADD   = 4'd0;
localparam [3:0] RV64_ALU_SLL   = 4'd1;
localparam [3:0] RV64_ALU_SLT   = 4'd2;
localparam [3:0] RV64_ALU_SLTU  = 4'd3;
localparam [3:0] RV64_ALU_XOR   = 4'd4;
localparam [3:0] RV64_ALU_SRL   = 4'd5;
localparam [3:0] RV64_ALU_OR    = 4'd6;
localparam [3:0] RV64_ALU_AND   = 4'd7;
localparam [3:0] RV64_ALU_SUB   = 4'd8;
localparam [3:0] RV64_ALU_SRA   = 4'd9;
localparam [3:0] RV64_ALU_PASSB = 4'd10;

// SysCore's RV64I decoder: one instruction word in, the pipeline's control out.
//
// COMBINATIONAL AND STATELESS. It names what an instruction wants; it does not
// know what stage it is in, and it never stalls. Everything that depends on
// timing lives above it.
//
// ILLEGAL IS AN OUTPUT, NOT A HALT. The RV32 compute PE halts on an unmapped
// encoding because a compute unit's fault IS its completion. SysCore hosts a
// runtime and takes a trap instead, so the decoder only reports.

`default_nettype none

module rv64_decode (
    input  wire [31:0] instr,

    output wire [4:0]  rs1,
    output wire [4:0]  rs2,
    output wire [4:0]  rd,
    output reg  [63:0] imm,

    output reg  [3:0]  alu_op,
    output reg         alu_word,     // the W form
    output reg         op1_pc,       // operand 1 is the PC, not rs1
    output reg         op2_imm,      // operand 2 is the immediate, not rs2

    output reg         wr_reg,
    output reg         is_muldiv,    // RV64M: funct7 0000001 on OP / OP-32
    output reg         is_branch,
    output reg         is_jal,
    output reg         is_jalr,
    output reg         is_load,
    output reg         is_store,
    output reg         is_amo,       // the A group, LR and SC included
    output wire [4:0]  amo_op,       // funct5
    output reg  [2:0]  mem_f3,       // width and signedness, funct3 as-is
    output reg         is_fence,
    output reg         is_fence_i,   // FENCE.I: make written code visible to fetch
    output reg         is_ecall,
    output reg         is_ebreak,
    output reg         is_mret,
    output reg         is_sret,
    output reg         is_sfence,
    output reg         is_wfi,
    output reg         is_csr,
    output reg         csr_wr,       // the write side actually happens
    output reg         csr_imm,      // the operand is the 5-bit field, not rs1
    output wire [11:0] csr_addr,
    output reg         illegal
);
`include "rv64_defs.vh"

    localparam [6:0] OP_LUI    = 7'b0110111, OP_AUIPC  = 7'b0010111;
    localparam [6:0] OP_JAL    = 7'b1101111, OP_JALR   = 7'b1100111;
    localparam [6:0] OP_BRANCH = 7'b1100011, OP_LOAD   = 7'b0000011;
    localparam [6:0] OP_STORE  = 7'b0100011, OP_IMM    = 7'b0010011;
    localparam [6:0] OP_REG    = 7'b0110011, OP_IMM32  = 7'b0011011;
    localparam [6:0] OP_REG32  = 7'b0111011, OP_MISCM  = 7'b0001111;
    localparam [6:0] OP_SYSTEM = 7'b1110011, OP_AMO = 7'b0101111;

    assign amo_op   = instr[31:27];
    assign csr_addr = instr[31:20];

    wire [6:0] opcode = instr[6:0];
    wire [2:0] f3     = instr[14:12];
    wire [6:0] f7     = instr[31:25];

    assign rs1 = instr[19:15];
    assign rs2 = instr[24:20];
    assign rd  = instr[11:7];

    // ---- immediates ---------------------------------------------------------
    wire [63:0] imm_i = {{52{instr[31]}}, instr[31:20]};
    wire [63:0] imm_s = {{52{instr[31]}}, instr[31:25], instr[11:7]};
    wire [63:0] imm_b = {{51{instr[31]}}, instr[31], instr[7],
                         instr[30:25], instr[11:8], 1'b0};
    wire [63:0] imm_u = {{32{instr[31]}}, instr[31:12], 12'd0};
    wire [63:0] imm_j = {{43{instr[31]}}, instr[31], instr[19:12],
                         instr[20], instr[30:21], 1'b0};

    // SHIFT AMOUNTS ARE 6 BITS AT RV64 AND 5 AT `W`, and the bit above the
    // field is part of the operation rather than the amount -- `SRAI` differs
    // from `SRLI` by instr[30] alone. Sizing this wrong is the classic RV64
    // decode bug: it turns SRAI into a shift by 32 more than asked.
    wire [63:0] imm_sh   = {58'd0, instr[25:20]};
    wire [63:0] imm_sh_w = {59'd0, instr[24:20]};

    // ---- shift and arithmetic legality --------------------------------------
    wire is_shift_i  = (f3 == 3'b001) || (f3 == 3'b101);
    wire shamt_ok    = (f3 == 3'b001) ? (instr[31:26] == 6'b000000)
                                      : (instr[31:26] == 6'b000000
                                         || instr[31:26] == 6'b010000);
    wire shamt_ok_w  = (f3 == 3'b001) ? (f7 == 7'b0000000)
                                      : (f7 == 7'b0000000 || f7 == 7'b0100000);
    wire f7_ok       = (f7 == 7'b0000000)
                    || (f7 == 7'b0100000 && (f3 == 3'b000 || f3 == 3'b101));

    // funct3 IS the ALU code for the common group; only SUB and SRA differ, and
    // they differ by one bit of funct7.
    wire alt = f7[5];
    // The "alternate" R-type op: SUB for funct3 000, SRA for the shift.
    wire [3:0] alt_op  = (f3 == 3'b000) ? RV64_ALU_SUB : RV64_ALU_SRA;
    // SYSTEM encodings with rs1 = rd = 0, and with rd = 0 alone.
    wire       lo_zero = (instr[19:7] == 13'd0);
    wire       rd_zero = (instr[11:7] == 5'd0);

    always @(*) begin
        imm       = imm_i;
        alu_op    = RV64_ALU_ADD;
        alu_word  = 1'b0;
        op1_pc    = 1'b0;
        op2_imm   = 1'b1;
        wr_reg    = 1'b0;
        is_muldiv = 1'b0;
        is_branch = 1'b0;
        is_jal    = 1'b0;
        is_jalr   = 1'b0;
        is_load   = 1'b0;
        is_store  = 1'b0;
        is_amo    = 1'b0;
        mem_f3    = f3;
        is_fence  = 1'b0;
        is_fence_i = 1'b0;
        is_ecall  = 1'b0;
        is_ebreak = 1'b0;
        is_mret   = 1'b0;
        is_sret   = 1'b0;
        is_sfence = 1'b0;
        is_wfi    = 1'b0;
        is_csr    = 1'b0;
        csr_wr    = 1'b0;
        csr_imm   = 1'b0;
        illegal   = 1'b0;

        case (opcode)
            OP_LUI: begin
                imm = imm_u; alu_op = RV64_ALU_PASSB; wr_reg = 1'b1;
            end
            OP_AUIPC: begin
                imm = imm_u; op1_pc = 1'b1; wr_reg = 1'b1;
            end
            OP_JAL: begin
                imm = imm_j; is_jal = 1'b1; wr_reg = 1'b1;
            end
            OP_JALR: begin
                imm = imm_i; is_jalr = 1'b1; wr_reg = 1'b1;
                illegal = (f3 != 3'b000);
            end
            OP_BRANCH: begin
                imm = imm_b; is_branch = 1'b1;
                illegal = (f3 == 3'b010) || (f3 == 3'b011);
            end
            OP_LOAD: begin
                imm = imm_i; is_load = 1'b1; wr_reg = 1'b1;
                // 011 is LD and legal at RV64; 111 is the only hole.
                illegal = (f3 == 3'b111);
            end
            OP_STORE: begin
                imm = imm_s; is_store = 1'b1;
                illegal = (f3 > 3'b011);
            end
            OP_IMM: begin
                imm    = is_shift_i ? imm_sh : imm_i;
                alu_op = is_shift_i && (f3 == 3'b101) && instr[30]
                       ? RV64_ALU_SRA : {1'b0, f3};
                wr_reg = 1'b1;
                illegal = is_shift_i && !shamt_ok;
            end
            OP_IMM32: begin
                imm      = is_shift_i ? imm_sh_w : imm_i;
                alu_word = 1'b1;
                wr_reg   = 1'b1;
                alu_op   = is_shift_i && (f3 == 3'b101) && instr[30]
                         ? RV64_ALU_SRA : {1'b0, f3};
                // ADDIW, SLLIW, SRLIW, SRAIW and nothing else.
                illegal  = !(f3 == 3'b000 || (is_shift_i && shamt_ok_w));
            end
            OP_REG: begin
                op2_imm = 1'b0; wr_reg = 1'b1;
                if (f7 == 7'b0000001) begin
                    is_muldiv = 1'b1;
                end
                else begin
                    alu_op  = alt ? alt_op : {1'b0, f3};
                    illegal = !f7_ok;
                end
            end
            OP_REG32: begin
                op2_imm  = 1'b0; alu_word = 1'b1; wr_reg = 1'b1;
                if (f7 == 7'b0000001) begin
                    is_muldiv = 1'b1;
                    // MULW, DIVW, DIVUW, REMW, REMUW -- funct3 1..3 are holes.
                    illegal   = (f3 == 3'b001) || (f3 == 3'b010)
                             || (f3 == 3'b011);
                end
                else begin
                    alu_op   = alt ? alt_op : {1'b0, f3};
                    illegal  = !((f3 == 3'b000 && (f7 == 7'b0000000
                                                   || f7 == 7'b0100000))
                                 || (f3 == 3'b001 && f7 == 7'b0000000)
                                 || (f3 == 3'b101 && (f7 == 7'b0000000
                                                      || f7 == 7'b0100000)));
                end
            end
            OP_AMO: begin
                // The address is rs1 with no offset, so the immediate is zero
                // and the operand path is the ordinary one.
                imm     = 64'd0;
                is_amo  = 1'b1;
                wr_reg  = 1'b1;
                illegal = (f3 != 3'b010) && (f3 != 3'b011);
                case (instr[31:27])
                    5'b00010, 5'b00011, 5'b00001, 5'b00000, 5'b00100,
                    5'b01100, 5'b01000, 5'b10000, 5'b10100, 5'b11000,
                    5'b11100: ;
                    default: illegal = 1'b1;
                endcase
                // LR takes no second operand; SC's is the value to store.
            end
            OP_MISCM: begin
                is_fence   = 1'b1;
                is_fence_i = (f3 == 3'b001);   // FENCE.I, distinct from FENCE
                illegal    = (f3 != 3'b000) && (f3 != 3'b001);
            end
            OP_SYSTEM: begin
                if (f3 == 3'b000) begin
                    is_ecall  = (instr[31:7] == 25'd0);
                    is_ebreak = (instr[31:20] == 12'd1)   && lo_zero;
                    is_mret   = (instr[31:20] == 12'h302) && lo_zero;
                    is_sret   = (instr[31:20] == 12'h102) && lo_zero;
                    is_wfi    = (instr[31:20] == 12'h105) && lo_zero;
                    // SFENCE.VMA ignores rs1/rs2 here: the TLB has no ASIDs and
                    // no per-address invalidate; every form is the full sweep.
                    is_sfence = (instr[31:25] == 7'b0001001) && rd_zero;
                    illegal   = !is_ecall && !is_ebreak && !is_mret && !is_sret
                             && !is_wfi && !is_sfence;
                end
                else if (f3 == 3'b100) begin
                    illegal = 1'b1;
                end
                else begin
                    // Zicsr. The immediate forms put a 5-bit zero-extended
                    // value where rs1 would be, so the operand select is the
                    // same wire that names the source.
                    is_csr    = 1'b1;
                    csr_imm   = f3[2];
                    wr_reg    = 1'b1;
                    // A SET OR CLEAR OF ZERO MUST NOT WRITE. `csrrs x1, m, x0`
                    // is the idiomatic read, and a write would have side
                    // effects the program did not ask for.
                    csr_wr    = (f3[1:0] == 2'b01) || (rs1 != 5'd0);
                    imm       = {59'd0, rs1};
                end
            end
            default: illegal = 1'b1;
        endcase

        // x0 IS NOT A DESTINATION. Suppressing the write here rather than in the
        // regfile keeps the hazard logic honest: a pipeline that thinks x0 was
        // written will forward a value nothing can observe.
        if (rd == 5'd0) begin
            wr_reg = 1'b0;
        end
    end

endmodule

`default_nettype wire

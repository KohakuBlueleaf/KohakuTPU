// kht_predec -- the SIMT PE's instruction decode, moved OFF the fetch path.
//
// Pure combinational, and it runs on the WRITE side: a shader image is decoded
// once as it lands in the window and the result is stored beside it. The core
// then reads control out of memory instead of computing it between the window
// and its own registers. See kht_ctrl.vh for why.
//
// Every expression here was lifted verbatim out of kht_core so the two cannot
// disagree about what an instruction means.

`default_nettype none

// The width is KHT_CW in kht_ctrl.vh; a port cannot read an included localparam,
// so the three places that carry this word (here, kht_pe, kht_core) state it as
// a literal and kht_core checks them against each other at elaboration.
module kht_predec (
    input  wire [31:0] instr,
    output wire [59:0] ctrl
);
`include "kht_isa.vh"
`include "kht_ctrl.vh"

    localparam [3:0] A_ADD = 4'd0, A_SUB = 4'd1, A_SLL = 4'd2, A_SLT = 4'd3;
    localparam [3:0] A_SLTU = 4'd4, A_XOR = 4'd5, A_SRL = 4'd6, A_SRA = 4'd7;
    localparam [3:0] A_OR = 4'd8, A_AND = 4'd9;

    wire [6:0] opc = instr[6:0];
    wire [2:0] f3  = instr[14:12];
    wire [6:0] f7  = instr[31:25];

    wire is_khg  = (opc == KHT_OPCODE);
    wire is_khgi = (opc == KHGI_OPCODE);

    wire is_salu = is_khg && (f3 == KHT_F3_SALU);
    wire is_smov = is_khg && (f3 == KHT_F3_SMOV);
    wire is_div  = is_khg && (f3 == KHT_F3_DIV);
    wire is_sub  = is_khg && (f3 == KHT_F3_SUB);
    wire is_vmem = is_khg && (f3 == KHT_F3_VMEM);
    // f7[2:0] is the operation: 0-3 the arithmetic four, 4-7 the FSFU seeds.
    // FP32 is the only compute type, so there is no format bit and no `_H` half
    // of the table; anything above VFRSQRT is an unmapped encoding.
    wire is_flt  = is_khg && (f3 == KHT_F3_FLT) && (f7 <= KHT_FLT_VFRSQRT);

    wire [2:0] mem_op = f7[6:4];
    wire mem_lin   = is_vmem && (mem_op >= KHT_MEM_OP_LIN);
    wire mem_store = is_vmem && (
        (mem_op == KHT_MEM_OP_S)
        || (mem_op == KHT_MEM_OP_SIN)
    );

    wire is_sbeqz = is_khgi && (f3 == KHGI_F3_BEQZ);
    wire is_sbnez = is_khgi && (f3 == KHGI_F3_BNEZ);
    wire is_sbr   = is_sbeqz || is_sbnez;
    wire is_simm  = is_khgi && !is_sbr;

    wire is_lui   = (opc == 7'b0110111);
    wire is_auipc = (opc == 7'b0010111);
    wire is_jal   = (opc == 7'b1101111);
    wire is_jalr  = (opc == 7'b1100111);
    wire is_load  = (opc == 7'b0000011);
    wire is_store = (opc == 7'b0100011);
    wire is_opimm = (opc == 7'b0010011);
    wire is_op    = (opc == 7'b0110011);
    // RV32M rides the same group. funct3 100..111 is div/rem and stays illegal.
    wire is_imul  = is_op && (f7 == 7'b0000001) && (f3[2] == 1'b0);
    wire is_mdiv  = is_op && (f7 == 7'b0000001) && (f3[2] == 1'b1);
    wire is_sys   = (opc == 7'b1110011);
    wire is_fence = (opc == 7'b0001111);
    wire is_rvbr  = (opc == 7'b1100011);

    wire is_shfl  = is_sub && (f7 == KHT_SUB_SHFLXOR);
    wire is_bcast = is_sub && (f7 == KHT_SUB_BCAST);
    wire is_redux = is_sub && (f7 >= KHT_SUB_REDUXADD) && (f7 <= KHT_SUB_REDUXOR);

    reg [3:0] alu_op;
    always @(*) begin
        if (is_op || is_opimm) begin
            case (f3)
                3'b000:  alu_op = (is_op && f7[5]) ? A_SUB : A_ADD;
                3'b001:  alu_op = A_SLL;
                3'b010:  alu_op = A_SLT;
                3'b011:  alu_op = A_SLTU;
                3'b100:  alu_op = A_XOR;
                3'b101:  alu_op = f7[5] ? A_SRA : A_SRL;
                3'b110:  alu_op = A_OR;
                default: alu_op = A_AND;
            endcase
        end
        else begin
            alu_op = A_ADD;
        end
    end

    // The butterfly is G8's and a build without it faults rather than writing
    // the ALU result; the CORE applies that gate, because it is the thing that
    // knows HAS_SHFL. Here the two are only reported.
    wire illegal = is_rvbr
                || is_mdiv
                || (is_khg && !(is_salu || is_smov || is_div || is_sub
                                || is_vmem || is_flt))
                || (is_load && ((f3 == 3'b011) || (f3 == 3'b110) || (f3 == 3'b111)))
                || (is_store && (f3 > 3'b010))
                || !(is_khg || is_khgi || is_lui || is_auipc || is_jal || is_jalr
                     || is_load || is_store || is_opimm || is_op || is_sys
                     || is_fence);

    assign ctrl[C_SALU]    = is_salu;
    assign ctrl[C_SMOV]    = is_smov;
    assign ctrl[C_DIV]     = is_div;
    assign ctrl[C_SUB]     = is_sub;
    assign ctrl[C_VMEM]    = is_vmem;

    assign ctrl[C_SPLIT]   = is_div && (f7 == KHT_DIV_SPLIT);
    assign ctrl[C_JOIN]    = is_div && (f7 == KHT_DIV_JOIN);
    assign ctrl[C_TMC]     = is_div && (f7 == KHT_DIV_TMC);
    assign ctrl[C_BAR]     = is_div && (f7 == KHT_DIV_BAR);

    assign ctrl[C_S2V]     = is_smov && (f7 == KHT_SMOV_S2V);
    assign ctrl[C_RDCTL]   = is_smov && (f7 == KHT_SMOV_RDCTL);

    assign ctrl[C_BALLOT]  = is_sub && (f7 == KHT_SUB_BALLOT);
    assign ctrl[C_VRF]     = is_sub && (f7 == KHT_SUB_VREADFIRST);
    assign ctrl[C_REDUX]   = is_redux;
    assign ctrl[C_SHFL]    = is_shfl;
    assign ctrl[C_BCAST]   = is_bcast;
    assign ctrl[C_LANID]   = is_sub && (f7 == KHT_SUB_VLANEID);

    assign ctrl[C_MEMLIN]  = mem_lin;
    assign ctrl[C_MEMST]   = mem_store;

    assign ctrl[C_SBEQZ]   = is_sbeqz;
    assign ctrl[C_SBNEZ]   = is_sbnez;
    assign ctrl[C_SBR]     = is_sbr;
    assign ctrl[C_SIMM]    = is_simm;

    assign ctrl[C_LUI]     = is_lui;
    assign ctrl[C_AUIPC]   = is_auipc;
    assign ctrl[C_JAL]     = is_jal;
    assign ctrl[C_JALR]    = is_jalr;
    assign ctrl[C_LOAD]    = is_load;
    assign ctrl[C_STORE]   = is_store;
    assign ctrl[C_OPIMM]   = is_opimm;
    assign ctrl[C_OP]      = is_op;
    assign ctrl[C_SYS]     = is_sys;
    assign ctrl[C_FENCE]   = is_fence;

    assign ctrl[C_ALU0+3:C_ALU0] = alu_op;
    assign ctrl[C_ILLEGAL] = illegal;

    // `is_op && !is_imul`: a multiply writes the vector file through the
    // multiplier's own retire slot, so the ordinary writeback must not claim it.
    assign ctrl[C_VTWEN]   = is_lui || is_auipc || is_jal || is_jalr || is_load
                          || is_opimm || (is_op && !is_imul)
                          || (is_smov && (f7 == KHT_SMOV_S2V))
                          || (is_sub && (f7 == KHT_SUB_VLANEID))
                          || is_shfl || is_bcast
                          || (is_vmem && !mem_store);
    assign ctrl[C_VTOP2I]  = is_opimm || is_lui || is_auipc || is_load || is_store;

    assign ctrl[C_EXRDV]   = (is_div && (f7 == KHT_DIV_SPLIT))
                          || (is_sub && (f7 == KHT_SUB_BALLOT))
                          || (is_sub && (f7 == KHT_SUB_VREADFIRST))
                          || is_redux;
    assign ctrl[C_SWEN]    = is_salu || is_simm
                          || (is_smov && (f7 == KHT_SMOV_RDCTL))
                          || (is_sub && (f7 == KHT_SUB_BALLOT))
                          || (is_sub && (f7 == KHT_SUB_VREADFIRST))
                          || is_redux;

    assign ctrl[C_PERLANE] = is_load || is_store || is_vmem;
    assign ctrl[C_LDANY]   = !mem_store && !is_store;
    assign ctrl[C_ECALL]   = is_sys && (instr[20] == 1'b0);
    assign ctrl[C_EBRK]    = is_sys && (instr[20] == 1'b1);

    // custom-2 funct7 already IS the A_* order; custom-3 funct3 is not, so it
    // is remapped here rather than in a second ALU in the core.
    reg [3:0] sop;
    always @(*) begin
        if (is_simm) begin
            case (f3)
                KHGI_F3_ADDI: sop = A_ADD;
                KHGI_F3_ANDI: sop = A_AND;
                KHGI_F3_ORI:  sop = A_OR;
                KHGI_F3_SLLI: sop = A_SLL;
                KHGI_F3_SRLI: sop = A_SRL;
                default:      sop = A_SRA;
            endcase
        end
        else begin
            sop = f7[3:0];
        end
    end
    assign ctrl[C_SOP0+3:C_SOP0] = sop;

    // Every consumer of sv1 and sv2 in kht_core, and nothing else. `rdctl`
    // is NOT here: it uses rs2 as a control-slot index, not as a register.
    assign ctrl[C_RDS1] = is_salu || is_simm || is_sbr || is_vmem
                       || (is_smov && (f7 == KHT_SMOV_S2V))
                       || (is_div  && (f7 == KHT_DIV_TMC));
    assign ctrl[C_RDS2] = is_salu || is_shfl;

    // The float class writes the vector file like any other lane operation --
    // it just does it FLAT cycles later, through its own retire slot.
    assign ctrl[C_FLT]  = is_flt;
    assign ctrl[C_FOP0+3:C_FOP0] = f7[3:0];

    assign ctrl[C_IMUL] = is_imul;
    assign ctrl[C_MOP0+1:C_MOP0] = f3[1:0];

endmodule

`default_nettype wire

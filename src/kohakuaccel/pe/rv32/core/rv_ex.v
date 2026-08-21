// rv_ex -- execute: the ALU, the branch comparator, and the effective address.
//
// EVERY BRANCH AND JUMP IS RESOLVED HERE against the architectural answer, so
// the predictor is an optimisation with no correctness role. A misprediction
// raises `ex_redir`, which rv_if registers; the two younger instructions in
// flight are killed by the same signal.
//
// The effective address leaves this stage COMBINATIONALLY, not through the
// pipeline register: the data arrays have a registered address input, so the
// address must be at their pins in this cycle for the data to be out in MEM.
// It is the ALU's own adder output -- loads and stores are rs1 + imm.
//
// A HALT IS A REDIRECT THAT ALSO STOPS FETCH. ECALL, EBREAK, an illegal
// encoding and a misaligned access all take this path: there are no CSRs and no
// trap vector (design note s16.3), so the core stops and the reason is read
// through the control region.
//
// Resolve, predictor update and halt are all qualified by `!x_hold`. Without
// that, a stalled memory stage would let the same branch resolve every cycle
// and walk its saturating counter to a value it never earned.

`default_nettype none

module rv_ex (
    input  wire        clk,
    input  wire        resetn,

    input  wire        x_hold,       // memory stall: EX may not advance

    input  wire [31:0] x_op1,
    input  wire [31:0] x_op2,
    input  wire [31:0] x_rs2v,
    input  wire [31:0] x_pc,
    input  wire [31:0] x_target,
    input  wire        x_valid,
    input  wire [4:0]  x_rd,
    input  wire        x_wen,
    input  wire [3:0]  x_alu,
    input  wire        x_branch,
    input  wire        x_jal,
    input  wire        x_jalr,
    input  wire        x_link,
    input  wire        x_load,
    input  wire        x_store,
    input  wire [2:0]  x_f3,
    input  wire        x_sys,
    input  wire        x_ebreak,
    input  wire        x_illegal,
    // The address decoder in rv_mem reports an unmapped region or a load from
    // a push-only window here, so every fault this core has is raised in one
    // stage and takes one path.
    input  wire        x_addr_fault,
    input  wire        x_pred_taken,
    input  wire [31:0] x_pred_target,

    // ---- combinational, consumed in this cycle ----
    output wire [31:0] ex_alu,       // the distance-1 forwarding source
    output wire [31:0] ex_addr,      // to the data arrays' address pins
    output wire        ex_redir,
    output wire [31:0] ex_redir_pc,
    output wire        ex_halt,
    output wire [1:0]  ex_cause,     // 1 ECALL, 2 EBREAK, 3 fault
    output wire [31:0] ex_halt_word,
    output wire        bp_valid,
    output wire [31:0] bp_pc,
    output wire        bp_taken,
    output wire        bp_is_jump,
    output wire [31:0] bp_target,

    // ---- to MEM ----
    output reg         m_valid,
    output reg  [4:0]  m_rd,
    output reg         m_wen,
    output reg  [31:0] m_pc,
    output reg  [31:0] m_val,        // writeback value for everything but a load
    output reg  [31:0] m_addr,
    output reg         m_load,
    output reg         m_store,
    output reg  [2:0]  m_f3,
    output reg  [3:0]  m_be,
    output reg  [31:0] m_sdata
);
    localparam [3:0] A_ADD = 4'd0, A_SUB = 4'd1, A_SLL = 4'd2, A_SLT  = 4'd3,
                     A_SLTU= 4'd4, A_XOR = 4'd5, A_SRL = 4'd6, A_SRA  = 4'd7,
                     A_OR  = 4'd8, A_AND = 4'd9;

    wire [31:0] sum  = x_op1 + x_op2;
    wire [31:0] diff = x_op1 - x_op2;
    wire [4:0]  sh   = x_op2[4:0];

    wire eq  = (x_op1 == x_op2);
    wire ltu = (x_op1 < x_op2);
    wire lt  = ($signed(x_op1) < $signed(x_op2));

    // ONE BARREL SHIFTER FOR ALL THREE SHIFTS: a left shift is a right shift
    // between two bit reversals, which are wiring, and SRA is the same shifter
    // with the sign fed in above the word.
    function [31:0] rev32;
        input [31:0] v;
        integer k;
        begin
            for (k = 0; k < 32; k = k + 1) rev32[k] = v[31-k];
        end
    endfunction

    wire        sh_left = (x_alu == A_SLL);
    wire        sh_sign = (x_alu == A_SRA) && x_op1[31];
    wire [32:0] sh_in   = {sh_sign, sh_left ? rev32(x_op1) : x_op1};
    wire [32:0] sh_out  = $signed(sh_in) >>> sh;
    wire [31:0] shift_r = sh_left ? rev32(sh_out[31:0]) : sh_out[31:0];

    reg [31:0] alu_r;
    always @(*) begin
        case (x_alu)
        A_SUB:   alu_r = diff;
        A_SLT:   alu_r = {31'd0, lt};
        A_SLTU:  alu_r = {31'd0, ltu};
        A_XOR:   alu_r = x_op1 ^ x_op2;
        A_OR:    alu_r = x_op1 | x_op2;
        A_AND:   alu_r = x_op1 & x_op2;
        A_SLL,
        A_SRL,
        A_SRA:   alu_r = shift_r;
        default: alu_r = sum;
        endcase
    end

    assign ex_alu  = x_link ? (x_pc + 32'd4) : alu_r;
    assign ex_addr = sum;

    reg br_cond;
    always @(*) begin
        case (x_f3[2:1])
        2'b00:   br_cond = x_f3[0] ^ eq;
        2'b10:   br_cond = x_f3[0] ^ lt;
        default: br_cond = x_f3[0] ^ ltu;
        endcase
    end

    wire        act_taken  = (x_branch && br_cond) || x_jal || x_jalr;
    wire [31:0] act_target = x_jalr ? {sum[31:1], 1'b0} : x_target;
    wire [31:0] seq_pc     = x_pc + 32'd4;
    wire [31:0] next_pc    = act_taken ? act_target : seq_pc;

    wire mispredict = (x_pred_taken != act_taken) ||
                      (act_taken && (x_pred_target != act_target));

    // Misaligned is a fault, not a fixup: RV32I allows either, and the fixup
    // needs two accesses and a merge that this MEM stage does not have.
    wire misalign = (x_load || x_store) &&
                    (((x_f3[1:0] == 2'b01) && sum[0]) ||
                     ((x_f3[1:0] == 2'b10) && (sum[1:0] != 2'b00)));

    wire fault = x_illegal || misalign || x_addr_fault;
    wire live  = x_valid && !x_hold;

    assign ex_halt      = live && (x_sys || fault);
    assign ex_cause     = fault ? 2'd3 : x_ebreak ? 2'd2 : 2'd1;
    // The FAULT word only: ECALL and EBREAK report a0 from rv_core's committed
    // copy, because reading it here cost an opcode compare in the fetch path.
    assign ex_halt_word = x_pc;

    assign ex_redir    = live && (mispredict || x_sys || fault);
    assign ex_redir_pc = next_pc;

    assign bp_valid   = live && !fault && (x_branch || x_jal || x_jalr);
    assign bp_pc      = x_pc;
    assign bp_taken   = act_taken;
    assign bp_is_jump = x_jal || x_jalr;
    assign bp_target  = act_target;

    reg [3:0]  be_n;
    reg [31:0] sd_n;
    always @(*) begin
        case (x_f3[1:0])
        2'b00: begin be_n = 4'b0001 << sum[1:0];         sd_n = {4{x_rs2v[7:0]}};  end
        2'b01: begin be_n = sum[1] ? 4'b1100 : 4'b0011;  sd_n = {2{x_rs2v[15:0]}}; end
        default: begin be_n = 4'b1111;                   sd_n = x_rs2v;            end
        endcase
    end

    always @(posedge clk) begin
        if (!resetn) begin
            m_valid <= 1'b0;
        end else if (!x_hold) begin
            // NOT gated by the redirect: the instruction that caused it is the
            // one in this stage, and it must retire.
            m_valid <= x_valid;
            m_rd    <= x_rd;
            // A faulting instruction still retires -- the halt is raised by it,
            // so squashing it would leave nothing to report -- but it must not
            // commit. A faulting load would otherwise write back the address.
            m_wen   <= x_wen && !fault;
            m_pc    <= x_pc;
            m_val   <= ex_alu;
            m_addr  <= sum;
            // A faulting access must not reach memory: the halt is raised here
            // and the request is cancelled in the same edge.
            m_load  <= x_load  && !fault;
            m_store <= x_store && !fault;
            m_f3    <= x_f3;
            m_be    <= be_n;
            m_sdata <= sd_n;
        end
    end

endmodule

`default_nettype wire

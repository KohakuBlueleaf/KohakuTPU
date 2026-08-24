// rv_id -- decode and operand fetch.
//
// Decode is COMBINATIONAL ON THE FETCHED WORD, in the fetch stage's second
// cycle, and its result is what the decode register holds. The register file
// address therefore leaves at the same edge as the control bits, which is what
// buys the operand-fetch cycle for free instead of costing a seventh boundary.
//
// The forwarding network is deliberately small: three sources, no more.
//   distance 2 (producer in MEM) and 3 (producer in WB) forward from registers
//   distance 1 (producer in EX)  forwards from the ALU output, or stalls
//   distance 4                   is the register file's own write-through
// FWD_X is the knob for that middle line. At 1 the ALU output reaches the
// operand register through one extra mux; at 0 every back-to-back dependency
// costs a cycle instead. s3.4 of the design note says to prefer the stall if
// the mux costs frequency, so it is a parameter and both are measured.
//
// The branch/jump target adder lives here rather than in EX: PC and immediate
// are both registered by then, the adder is off every critical path in this
// stage, and carrying the target instead of the immediate keeps the EX register
// the same width.

`default_nettype none

module rv_id #(
    parameter integer FWD_X  = 1,
    // The KohakuMPE extension seam. At 0 nothing below is elaborated and the
    // base configuration is bit-identical; at 1 the custom-0 opcode stops being
    // illegal and the instruction word travels to EX for the vector unit.
    parameter integer SIMD_EN = 0,
    // Custom-1 is claimed only by a build carrying the float tier; without it
    // the major is unmapped and a float instruction faults here.
    parameter integer SIMD_FLOAT = 0
)(
    input  wire        clk,
    input  wire        resetn,

    // Two enables, because the two registers in this module stop for different
    // reasons: a data hazard freezes decode and feeds EX a bubble, while a
    // memory stall freezes EX as well and nothing moves at all.
    input  wire        d_hold,       // hazard OR memory stall
    input  wire        x_hold,       // memory stall only
    input  wire        bubble,       // hazard: EX gets a bubble this edge
    input  wire        kill,         // discard what is in flight, at this edge

    // ---- fetch stage, combinational ----
    input  wire [31:0] f2_instr,
    input  wire [31:0] f2_pc,
    input  wire        f2_valid,
    input  wire        f2_pred_taken,
    input  wire [31:0] f2_pred_target,

    // register-file addresses, taken straight off the fetched word
    output wire [4:0]  ra1,
    output wire [4:0]  ra2,
    input  wire [31:0] rd1,
    input  wire [31:0] rd2,

    // ---- what the hazard unit needs to see ----
    output wire        d_valid,
    output wire [4:0]  d_rs1a,
    output wire [4:0]  d_rs2a,
    output wire        d_use_rs1,
    output wire        d_use_rs2,

    // ---- forwarding, selected by the hazard unit ----
    input  wire [1:0]  fwd1_sel,
    input  wire [1:0]  fwd2_sel,
    input  wire [31:0] fwd_x_val,
    input  wire [31:0] fwd_m_val,
    input  wire [31:0] fwd_w_val,

    // ---- to EX ----
    output reg  [31:0] x_op1,
    output reg  [31:0] x_op2,
    output reg  [31:0] x_rs2v,
    output reg  [31:0] x_pc,
    output reg  [31:0] x_target,
    output reg         x_valid,
    output reg  [4:0]  x_rd,
    output reg         x_wen,
    output reg  [3:0]  x_alu,
    output reg         x_branch,
    output reg         x_jal,
    output reg         x_jalr,
    output reg         x_link,
    output reg         x_load,
    output reg         x_store,
    output reg  [2:0]  x_f3,
    output reg         x_sys,
    output reg         x_ebreak,
    output reg         x_illegal,
    output reg         x_pred_taken,
    output reg  [31:0] x_pred_target,

    // ---- to the vector unit, at SIMD_EN only ----
    output wire        x_vec,
    output wire [31:0] x_instr,
    // A vector load in DECODE. It claims the vector scratchpad's address port
    // one stage later, so the core bubbles it past a scalar store that is
    // claiming the same port from MEM.
    output wire        d_vec_ld
);
    localparam [3:0] A_ADD = 4'd0, A_SUB = 4'd1, A_SLL = 4'd2, A_SLT = 4'd3;
    localparam [3:0] A_SLTU = 4'd4, A_XOR = 4'd5, A_SRL = 4'd6, A_SRA = 4'd7;
    localparam [3:0] A_OR = 4'd8, A_AND = 4'd9;

    localparam [6:0] OP_LUI = 7'b0110111, OP_AUIPC = 7'b0010111;
    localparam [6:0] OP_JAL = 7'b1101111, OP_JALR = 7'b1100111;
    localparam [6:0] OP_BRANCH = 7'b1100011, OP_LOAD = 7'b0000011;
    localparam [6:0] OP_STORE = 7'b0100011, OP_IMM = 7'b0010011;
    localparam [6:0] OP_REG = 7'b0110011, OP_MISC = 7'b0001111;
    localparam [6:0] OP_SYS = 7'b1110011;

    wire [6:0] opc = f2_instr[6:0];
    wire [2:0] f3  = f2_instr[14:12];
    wire [6:0] f7  = f2_instr[31:25];

    wire [31:0] imm_i = {{20{f2_instr[31]}}, f2_instr[31:20]};
    wire [31:0] imm_s = {{20{f2_instr[31]}}, f2_instr[31:25], f2_instr[11:7]};
    wire [31:0] imm_b = {{19{f2_instr[31]}}, f2_instr[31], f2_instr[7],
                         f2_instr[30:25], f2_instr[11:8], 1'b0};
    wire [31:0] imm_u = {f2_instr[31:12], 12'd0};
    wire [31:0] imm_j = {{11{f2_instr[31]}}, f2_instr[31], f2_instr[19:12],
                         f2_instr[20], f2_instr[30:21], 1'b0};

    reg  [31:0] n_imm;
    reg  [3:0]  n_alu;
    reg         n_wen, n_branch, n_jal, n_jalr, n_link, n_load, n_store;
    reg         n_sys, n_ebreak, n_illegal, n_op1_pc, n_op1_zero, n_op2_imm;
    reg         n_use1, n_use2;

    // ---- the extension seam -------------------------------------------------
    // Declared BEFORE the decode that reads it: xvlog rejects a use-before-
    // declare that synthesis had accepted silently (rv_l1.v records the same).
    wire vec_is, vec_use1, vec_wrd, vec_ld;
    generate
    if (SIMD_EN != 0) begin : g_simd
        khs_scalar_decode #(.HAS_FLOAT(SIMD_FLOAT)) u_vdec (
            .instr(f2_instr), .is_khd(vec_is),
            .use_rs1(vec_use1), .wr_rd(vec_wrd), .is_vld(vec_ld)
        );
    end else begin : g_nodsp
        assign vec_is   = 1'b0;
        assign vec_use1 = 1'b0;
        assign vec_wrd  = 1'b0;
        assign vec_ld   = 1'b0;
    end
    endgenerate

    wire shift_ok = (f7 == 7'b0000000) || ((f7 == 7'b0100000) && (f3 == 3'b101));
    wire arith_ok = (
        (f7 == 7'b0000000)
        || ((f7 == 7'b0100000) && ((f3 == 3'b000) || (f3 == 3'b101)))
    );

    always @(*) begin
        n_imm      = imm_i;
        n_alu      = A_ADD;
        n_wen      = 1'b0;
        n_branch   = 1'b0;
        n_jal      = 1'b0;
        n_jalr     = 1'b0;
        n_link     = 1'b0;
        n_load     = 1'b0;
        n_store    = 1'b0;
        n_sys      = 1'b0;
        n_ebreak   = 1'b0;
        n_illegal  = 1'b0;
        n_op1_pc   = 1'b0;
        n_op1_zero = 1'b0;
        n_op2_imm  = 1'b1;
        n_use1     = 1'b0;
        n_use2     = 1'b0;

        case (opc)
            OP_LUI: begin
                n_imm = imm_u; n_wen = 1'b1; n_op1_zero = 1'b1;
            end
            OP_AUIPC: begin
                n_imm = imm_u; n_wen = 1'b1; n_op1_pc = 1'b1;
            end
            OP_JAL: begin
                n_imm = imm_j; n_wen = 1'b1; n_jal = 1'b1; n_link = 1'b1;
            end
            OP_JALR: begin
                n_wen = 1'b1; n_jalr = 1'b1; n_link = 1'b1; n_use1 = 1'b1;
                n_illegal = (f3 != 3'b000);
            end
            OP_BRANCH: begin
                n_imm = imm_b; n_branch = 1'b1; n_op2_imm = 1'b0;
                n_use1 = 1'b1; n_use2 = 1'b1;
                n_illegal = (f3 == 3'b010) || (f3 == 3'b011);
            end
            OP_LOAD: begin
                n_wen = 1'b1; n_load = 1'b1; n_use1 = 1'b1;
                n_illegal = (f3 == 3'b011) || (f3 == 3'b110) || (f3 == 3'b111);
            end
            OP_STORE: begin
                n_imm = imm_s; n_store = 1'b1; n_use1 = 1'b1; n_use2 = 1'b1;
                n_illegal = (f3[2] != 1'b0) || (f3[1:0] == 2'b11);
            end
            OP_IMM: begin
                n_wen = 1'b1; n_use1 = 1'b1;
                case (f3)
                    3'b000: n_alu = A_ADD;
                    3'b010: n_alu = A_SLT;
                    3'b011: n_alu = A_SLTU;
                    3'b100: n_alu = A_XOR;
                    3'b110: n_alu = A_OR;
                    3'b111: n_alu = A_AND;
                    3'b001: begin n_alu = A_SLL; n_illegal = (f7 != 7'b0000000); end
                    default: begin
                        n_alu = f7[5] ? A_SRA : A_SRL;
                        n_illegal = !shift_ok;
                    end
                endcase
            end
            OP_REG: begin
                n_wen = 1'b1; n_op2_imm = 1'b0; n_use1 = 1'b1; n_use2 = 1'b1;
                n_illegal = !arith_ok;
                case (f3)
                    3'b000: n_alu = f7[5] ? A_SUB : A_ADD;
                    3'b001: n_alu = A_SLL;
                    3'b010: n_alu = A_SLT;
                    3'b011: n_alu = A_SLTU;
                    3'b100: n_alu = A_XOR;
                    3'b101: n_alu = f7[5] ? A_SRA : A_SRL;
                    3'b110: n_alu = A_OR;
                    default: n_alu = A_AND;
                endcase
            end
            // FENCE is a NOP: one core, one memory port, already ordered.
            OP_MISC: ;
            // No `use1`: the halt word comes from rv_core's committed copy of a0,
            // not from a register read, so SYSTEM has no source operand at all.
            OP_SYS: begin
                n_sys     = 1'b1;
                n_ebreak  = f2_instr[20];
                n_illegal = (f2_instr[31:21] != 11'd0) || (f2_instr[19:7] != 13'd0);
            end
            default: n_illegal = 1'b1;
        endcase

        // The extension, after the case so it overrides the default refusal.
        // Its operands live in the vector file, which the scalar hazard unit
        // does not track: only the address base and the broadcast source are
        // real scalar reads, and only a reduction is a real scalar write.
        if (vec_is) begin
            n_illegal = 1'b0;
            n_use1    = vec_use1;
            n_use2    = 1'b0;
            n_wen     = vec_wrd;
            n_imm     = imm_i;
            n_op2_imm = 1'b1;
        end
    end

    // ---- the instruction word, on its way to the vector unit ---------------
    generate
    if (SIMD_EN != 0) begin : g_vins
        // The instruction word travels IF2 -> ID -> EX beside the control bits,
        // because the vector unit decodes it in EX and there is nowhere else it
        // could come from. 64 flops, and only in this configuration.
        reg [31:0] d_ins, x_ins;
        reg        d_v, x_v, d_ld;
        assign d_vec_ld = d_ld;
        always @(posedge clk) begin
            if (!resetn) begin
                d_v <= 1'b0;
                x_v <= 1'b0;
                d_ld <= 1'b0;
            end else begin
                if (!d_hold) begin
                    d_ins <= f2_instr;
                    d_v   <= f2_valid && vec_is && !kill;
                    d_ld  <= f2_valid && vec_ld && !kill;
                end else if (kill) begin
                    d_v  <= 1'b0;
                    d_ld <= 1'b0;
                end
                if (!x_hold) begin
                    x_ins <= d_ins;
                    x_v   <= d_v && !bubble && !kill;
                end
            end
        end
        assign x_instr = x_ins;
        assign x_vec   = x_v;
    end else begin : g_novins
        assign x_instr  = 32'd0;
        assign x_vec    = 1'b0;
        assign d_vec_ld = 1'b0;
    end
    endgenerate

    // STRAIGHT OFF THE FETCHED WORD, window data into array address: the ECALL
    // opcode compare that sat here was most of the iteration-3 binding path.
    assign ra1 = f2_instr[19:15];
    assign ra2 = f2_instr[24:20];

    // ---- the decode register ------------------------------------------------
    reg [31:0] d_pc, d_imm, d_pred_target;
    reg [4:0]  d_rd, d_r1a, d_r2a;
    reg [3:0]  d_alu;
    reg [2:0]  d_f3;
    reg        d_v, d_wen, d_branch, d_jal, d_jalr, d_link, d_load, d_store;
    reg        d_sys, d_ebreak, d_illegal, d_op1_pc, d_op1_zero, d_op2_imm;
    reg        d_u1, d_u2, d_pred_taken;

    always @(posedge clk) begin
        if (!resetn) begin
            d_v <= 1'b0;
        end else if (!d_hold) begin
            d_v           <= f2_valid && !kill;
            d_pc          <= f2_pc;
            d_imm         <= n_imm;
            d_rd          <= f2_instr[11:7];
            d_r1a         <= ra1;
            d_r2a         <= ra2;
            d_alu         <= n_alu;
            d_f3          <= f3;
            d_wen         <= n_wen && (f2_instr[11:7] != 5'd0);
            d_branch      <= n_branch;
            d_jal         <= n_jal;
            d_jalr        <= n_jalr;
            d_link        <= n_link;
            d_load        <= n_load;
            d_store       <= n_store;
            d_sys         <= n_sys;
            d_ebreak      <= n_ebreak;
            d_illegal     <= n_illegal;
            d_op1_pc      <= n_op1_pc;
            d_op1_zero    <= n_op1_zero;
            d_op2_imm     <= n_op2_imm;
            d_u1          <= n_use1;
            d_u2          <= n_use2;
            d_pred_taken  <= f2_pred_taken;
            d_pred_target <= f2_pred_target;
        end else if (kill) begin
            d_v <= 1'b0;
        end
    end

    assign d_valid   = d_v;
    assign d_rs1a    = d_r1a;
    assign d_rs2a    = d_r2a;
    assign d_use_rs1 = d_u1;
    assign d_use_rs2 = d_u2;

    // ---- operand select -----------------------------------------------------
    function [31:0] fwd_pick;
        input [1:0]  sel;
        input [31:0] base;
        input [31:0] fx;
        input [31:0] fm;
        input [31:0] fw;
        case (sel)
            2'd1:    fwd_pick = (FWD_X != 0) ? fx : base;
            2'd2:    fwd_pick = fm;
            2'd3:    fwd_pick = fw;
            default: fwd_pick = base;
        endcase
    endfunction

    wire [31:0] v1 = fwd_pick(fwd1_sel, rd1, fwd_x_val, fwd_m_val, fwd_w_val);
    wire [31:0] v2 = fwd_pick(fwd2_sel, rd2, fwd_x_val, fwd_m_val, fwd_w_val);

    wire [31:0] sel_op1 = d_op1_zero ? 32'd0 : d_op1_pc ? d_pc : v1;
    wire [31:0] sel_op2 = d_op2_imm  ? d_imm : v2;

    always @(posedge clk) begin
        if (!resetn) begin
            x_valid <= 1'b0;
        end else if (!x_hold) begin
            x_valid       <= d_v && !bubble && !kill;
            x_op1         <= sel_op1;
            x_op2         <= sel_op2;
            x_rs2v        <= v2;
            x_pc          <= d_pc;
            x_target      <= d_pc + d_imm;
            x_rd          <= d_rd;
            x_wen         <= d_wen;
            x_alu         <= d_alu;
            x_branch      <= d_branch;
            x_jal         <= d_jal;
            x_jalr        <= d_jalr;
            x_link        <= d_link;
            x_load        <= d_load;
            x_store       <= d_store;
            x_f3          <= d_f3;
            x_sys         <= d_sys;
            x_ebreak      <= d_ebreak;
            x_illegal     <= d_illegal;
            x_pred_taken  <= d_pred_taken;
            x_pred_target <= d_pred_target;
        end
    end

endmodule

`default_nettype wire

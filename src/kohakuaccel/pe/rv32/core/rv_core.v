// rv_core -- the RV32I pipeline: the five stage modules, the register file,
// the hazard unit, and the run/halt state.
//
// SIX REGISTER BOUNDARIES for five architectural stages, and the two extra
// exist for one reason each:
//
//   IF1  next-PC select                  -> the window's address register
//   IF2  instruction out, decode         -> the register file's address register
//   ID   operands out, forward           -> EX
//   EX   ALU, branch resolve, address    -> MEM
//   MEM  array address and write enables -> WB
//   WB   array data out, commit
//
// The instruction window and the register file are both synchronous arrays, so
// each costs a cycle between address and data. The design note (s16.2) predicts
// exactly this: the textbook five become six or seven once those two reads are
// paid for honestly.
//
// THE HAZARD UNIT IS THE WHOLE OF THE COMPLEXITY BUDGET. Three forwarding
// sources by position, one stall rule, and nothing else:
//
//   producer in EX  (distance 1)  forward the ALU output, or stall (FWD_X)
//   producer in MEM (distance 2)  forward the EX result register; stall if load
//   producer in WB  (distance 3)  forward the writeback value, load included
//   distance 4                    the register file's own write-through
//
// A load's data does not exist until WB, which is why distance 1 and 2 stall on
// a load and distance 3 does not. That is the load-use penalty and it is two
// cycles back to back, one at a spacing of one.

`default_nettype none

module rv_core #(
    parameter integer IMEM_WORDS   = 2048,
    parameter integer SPAD_WORDS   = 2048,
    parameter integer BTB_ENTRIES  = 32,
    parameter integer BTB_TAG_W    = 8,
    parameter         REGFILE_PRIM = "distributed",
    parameter integer FWD_X        = 1,
    parameter integer POS_WIDTH    = 4
)(
    input  wire        clk,
    input  wire        resetn,

    // ---- kick and halt ----
    input  wire        boot_v,
    input  wire [31:0] boot_pc,
    output wire        run,
    output wire        halted,
    output wire [1:0]  cause,
    output wire [31:0] halt_word,
    output wire        pipe_empty,

    input  wire [7:0]  coreid,
    input  wire [31:0] arg,
    input  wire [15:0] wr_out,

    // ---- instruction window ----
    output wire [$clog2(IMEM_WORDS)-1:0] imem_addr,
    input  wire [31:0] imem_data,

    // ---- scratchpad, port B ----
    output wire [$clog2(SPAD_WORDS)-1:0] spad_addr,
    output wire [3:0]  spad_we,
    output wire [31:0] spad_wdata,
    input  wire [31:0] spad_rdata,

    // ---- internal L1 ----
    output wire [31:0] l1_probe,
    output wire        l1_req,
    output wire        l1_we,
    output wire [3:0]  l1_be,
    output wire [31:0] l1_addr,
    output wire [31:0] l1_wdata,
    input  wire [31:0] l1_rdata,
    input  wire        l1_stall,
    output wire        l1_flush,
    output wire        l1_inval,
    input  wire        l1_flush_busy,

    // ---- peer push ----
    output wire                  push_valid,
    input  wire                  push_ready,
    output wire [POS_WIDTH-1:0]  push_dx,
    output wire [POS_WIDTH-1:0]  push_dy,
    output wire                  push_win,
    output wire [13:0]           push_gran,
    output wire [2:0]            push_sel,
    output wire [3:0]            push_be,
    output wire [31:0]           push_data,

    // ---- retirement, for co-simulation ----
    output wire        retire_valid,
    output wire [31:0] retire_pc,
    output wire [4:0]  retire_rd,
    output wire [31:0] retire_val,
    output wire [31:0] cycle_ctr,
    output wire [31:0] instret_ctr
);
    // ---- fetch ----
    wire [31:0] f2_pc, f2_instr, f2_pred_target;
    wire        f2_valid, f2_pred_taken;

    // ---- decode ----
    wire [4:0]  ra1, ra2;
    wire [31:0] rd1, rd2;
    wire        d_valid, d_use_rs1, d_use_rs2;
    wire [4:0]  d_rs1a, d_rs2a;

    wire [31:0] x_op1, x_op2, x_rs2v, x_pc, x_target, x_pred_target;
    wire        x_valid, x_wen, x_branch, x_jal, x_jalr, x_link;
    wire        x_load, x_store, x_sys, x_ebreak, x_illegal, x_pred_taken;
    wire [4:0]  x_rd;
    wire [3:0]  x_alu;
    wire [2:0]  x_f3;

    // ---- execute ----
    wire [31:0] ex_alu, ex_addr, ex_redir_pc, ex_halt_word;
    wire        ex_redir, ex_halt;
    wire [1:0]  ex_cause;
    wire        bp_valid, bp_taken, bp_is_jump;
    wire [31:0] bp_pc, bp_target;
    wire        ex_bad_region;

    wire        m_valid, m_wen, m_load, m_store;
    wire [4:0]  m_rd;
    wire [31:0] m_pc, m_val, m_addr, m_sdata;
    wire [2:0]  m_f3;
    wire [3:0]  m_be;

    // ---- memory / writeback ----
    wire        stall_m;
    wire [31:0] wstage_val;
    wire        w_valid, w_wen;
    wire [4:0]  w_rd;
    wire [31:0] w_val, w_pc;

    wire        rf_we;
    wire [4:0]  rf_wa;
    wire [31:0] rf_wd;

    // ---- hazards ------------------------------------------------------------
    wire x_prod = x_valid && x_wen;
    wire m_prod = m_valid && m_wen;
    wire w_prod = w_valid && w_wen;

    wire h1_1 = x_prod && d_use_rs1 && (x_rd == d_rs1a);
    wire h1_2 = x_prod && d_use_rs2 && (x_rd == d_rs2a);
    wire h2_1 = m_prod && d_use_rs1 && (m_rd == d_rs1a);
    wire h2_2 = m_prod && d_use_rs2 && (m_rd == d_rs2a);
    wire h3_1 = w_prod && d_use_rs1 && (w_rd == d_rs1a);
    wire h3_2 = w_prod && d_use_rs2 && (w_rd == d_rs2a);

    wire hz1 = h1_1 || h1_2;
    wire hz2 = h2_1 || h2_2;

    wire stall_d = d_valid &&
                   (((FWD_X != 0) ? (hz1 && x_load) : hz1) || (hz2 && m_load));

    // Nearest producer wins. A select that names a value which is not ready is
    // harmless because the same condition raised the stall.
    wire [1:0] fwd1_sel = h1_1 ? 2'd1 : h2_1 ? 2'd2 : h3_1 ? 2'd3 : 2'd0;
    wire [1:0] fwd2_sel = h1_2 ? 2'd1 : h2_2 ? 2'd2 : h3_2 ? 2'd3 : 2'd0;

    wire hold_front = stall_d || stall_m;

    // ---- run / halt ---------------------------------------------------------
    reg        run_q, halted_q;
    reg [1:0]  cause_q;
    reg [31:0] halt_q;
    reg [31:0] cyc_q, ret_q;

    // A COMMITTED COPY OF a0, what ECALL and EBREAK report. Exact with no
    // forwarding: the halt redirects, so nothing younger than it ever commits.
    reg [31:0] a0_q;
    always @(posedge clk) if (rf_we && (rf_wa == 5'd10)) a0_q <= rf_wd;

    assign run       = run_q;
    assign halted    = halted_q;
    assign cause     = cause_q;
    assign halt_word = (cause_q == 2'd3) ? halt_q : a0_q;
    assign cycle_ctr = cyc_q;
    assign instret_ctr = ret_q;
    assign pipe_empty  = !f2_valid && !d_valid && !x_valid && !m_valid && !w_valid;

    always @(posedge clk) begin
        if (!resetn) begin
            run_q    <= 1'b0;
            halted_q <= 1'b0;
            cause_q  <= 2'd0;
            halt_q   <= 32'd0;
            cyc_q    <= 32'd0;
            ret_q    <= 32'd0;
        end else begin
            if (boot_v) begin
                run_q    <= 1'b1;
                halted_q <= 1'b0;
                cause_q  <= 2'd0;
                cyc_q    <= 32'd0;
                ret_q    <= 32'd0;
            end else if (ex_halt) begin
                run_q    <= 1'b0;
                halted_q <= 1'b1;
                cause_q  <= ex_cause;
                halt_q   <= ex_halt_word;
            end
            if (run_q) cyc_q <= cyc_q + 32'd1;
            if (retire_valid) ret_q <= ret_q + 32'd1;
        end
    end

    // ---- the stages ---------------------------------------------------------
    rv_if #(.IMEM_WORDS(IMEM_WORDS), .BTB_ENTRIES(BTB_ENTRIES),
            .BTB_TAG_W(BTB_TAG_W)) u_if (
        .clk(clk), .resetn(resetn),
        .run(run_q), .hold(hold_front), .kill(ex_redir),
        .boot_v(boot_v), .boot_pc(boot_pc),
        .ex_redir(ex_redir), .ex_redir_pc(ex_redir_pc),
        .imem_addr(imem_addr), .imem_data(imem_data),
        .f2_pc(f2_pc), .f2_valid(f2_valid), .f2_instr(f2_instr),
        .f2_pred_taken(f2_pred_taken), .f2_pred_target(f2_pred_target),
        .u_valid(bp_valid), .u_pc(bp_pc), .u_taken(bp_taken),
        .u_is_jump(bp_is_jump), .u_target(bp_target)
    );

    rv_regfile #(.MEM_PRIM(REGFILE_PRIM)) u_rf (
        .clk(clk),
        .ra_en(!hold_front), .ra1(ra1), .ra2(ra2), .rd1(rd1), .rd2(rd2),
        .we(rf_we), .wa(rf_wa), .wd(rf_wd)
    );

    rv_id #(.FWD_X(FWD_X)) u_id (
        .clk(clk), .resetn(resetn),
        .d_hold(hold_front), .x_hold(stall_m), .bubble(stall_d), .kill(ex_redir),
        .f2_instr(f2_instr), .f2_pc(f2_pc), .f2_valid(f2_valid),
        .f2_pred_taken(f2_pred_taken), .f2_pred_target(f2_pred_target),
        .ra1(ra1), .ra2(ra2), .rd1(rd1), .rd2(rd2),
        .d_valid(d_valid), .d_rs1a(d_rs1a), .d_rs2a(d_rs2a),
        .d_use_rs1(d_use_rs1), .d_use_rs2(d_use_rs2),
        .fwd1_sel(fwd1_sel), .fwd2_sel(fwd2_sel),
        .fwd_x_val(ex_alu), .fwd_m_val(m_val), .fwd_w_val(wstage_val),
        .x_op1(x_op1), .x_op2(x_op2), .x_rs2v(x_rs2v), .x_pc(x_pc),
        .x_target(x_target), .x_valid(x_valid), .x_rd(x_rd), .x_wen(x_wen),
        .x_alu(x_alu), .x_branch(x_branch), .x_jal(x_jal), .x_jalr(x_jalr),
        .x_link(x_link), .x_load(x_load), .x_store(x_store), .x_f3(x_f3),
        .x_sys(x_sys), .x_ebreak(x_ebreak), .x_illegal(x_illegal),
        .x_pred_taken(x_pred_taken), .x_pred_target(x_pred_target)
    );

    rv_ex u_ex (
        .clk(clk), .resetn(resetn), .x_hold(stall_m),
        .x_op1(x_op1), .x_op2(x_op2), .x_rs2v(x_rs2v), .x_pc(x_pc),
        .x_target(x_target), .x_valid(x_valid), .x_rd(x_rd), .x_wen(x_wen),
        .x_alu(x_alu), .x_branch(x_branch), .x_jal(x_jal), .x_jalr(x_jalr),
        .x_link(x_link), .x_load(x_load), .x_store(x_store), .x_f3(x_f3),
        .x_sys(x_sys), .x_ebreak(x_ebreak), .x_illegal(x_illegal),
        .x_addr_fault(ex_bad_region),
        .x_pred_taken(x_pred_taken), .x_pred_target(x_pred_target),
        .ex_alu(ex_alu), .ex_addr(ex_addr),
        .ex_redir(ex_redir), .ex_redir_pc(ex_redir_pc),
        .ex_halt(ex_halt), .ex_cause(ex_cause), .ex_halt_word(ex_halt_word),
        .bp_valid(bp_valid), .bp_pc(bp_pc), .bp_taken(bp_taken),
        .bp_is_jump(bp_is_jump), .bp_target(bp_target),
        .m_valid(m_valid), .m_rd(m_rd), .m_wen(m_wen), .m_pc(m_pc),
        .m_val(m_val), .m_addr(m_addr), .m_load(m_load), .m_store(m_store),
        .m_f3(m_f3), .m_be(m_be), .m_sdata(m_sdata)
    );

    rv_mem #(.SPAD_WORDS(SPAD_WORDS), .POS_WIDTH(POS_WIDTH)) u_mem (
        .clk(clk), .resetn(resetn),
        .ex_addr(ex_addr), .x_load(x_load), .x_store(x_store),
        .ex_bad_region(ex_bad_region),
        .m_valid(m_valid), .m_rd(m_rd), .m_wen(m_wen), .m_pc(m_pc),
        .m_val(m_val), .m_addr(m_addr), .m_load(m_load), .m_store(m_store),
        .m_f3(m_f3), .m_be(m_be), .m_sdata(m_sdata),
        .spad_addr(spad_addr), .spad_we(spad_we), .spad_wdata(spad_wdata),
        .spad_rdata(spad_rdata),
        .l1_probe(l1_probe), .l1_req(l1_req), .l1_we(l1_we), .l1_be(l1_be),
        .l1_addr(l1_addr), .l1_wdata(l1_wdata), .l1_rdata(l1_rdata),
        .l1_stall(l1_stall), .l1_flush(l1_flush), .l1_inval(l1_inval),
        .l1_flush_busy(l1_flush_busy),
        .push_valid(push_valid), .push_ready(push_ready),
        .push_dx(push_dx), .push_dy(push_dy), .push_win(push_win),
        .push_gran(push_gran), .push_sel(push_sel), .push_be(push_be),
        .push_data(push_data),
        .ctl_coreid(coreid), .ctl_arg(arg), .ctl_cycle(cyc_q),
        .ctl_instret(ret_q), .ctl_cause(cause_q), .ctl_wr_out(wr_out),
        .stall_m(stall_m), .wstage_val(wstage_val),
        .w_valid(w_valid), .w_rd(w_rd), .w_wen(w_wen), .w_val(w_val),
        .w_pc(w_pc)
    );

    rv_wb u_wb (
        .w_valid(w_valid), .w_rd(w_rd), .w_wen(w_wen), .w_val(wstage_val),
        .w_pc(w_pc),
        .rf_we(rf_we), .rf_wa(rf_wa), .rf_wd(rf_wd),
        .retire_valid(retire_valid), .retire_pc(retire_pc),
        .retire_rd(retire_rd), .retire_val(retire_val)
    );

endmodule

`default_nettype wire

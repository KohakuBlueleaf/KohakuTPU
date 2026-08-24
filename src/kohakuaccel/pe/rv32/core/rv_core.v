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
    parameter integer POS_WIDTH    = 4,
    // ---- the KohakuMPE SIMD extension. At 0 none of it is elaborated. ----
    parameter integer SIMD_EN        = 0,
    parameter integer SIMD_LANES      = 8,
    parameter integer SIMD_VREGS     = 8,
    parameter integer SIMD_NACC      = 2,
    parameter integer SIMD_VSPAD     = 1024,
    parameter integer SIMD_MULS      = 4,
    parameter integer SIMD_SHIFT     = 1,
    parameter integer SIMD_PERM      = 1,
    // Cross-lane permute OUTPUT words per pass; 0 = one per word.
    parameter integer SIMD_PERM_UNITS = 0,
    parameter integer SIMD_SHIFT_UNITS = 0,
    parameter integer SIMD_DOTDSP    = 0,
    parameter integer SIMD_WB        = 0,
    parameter integer SIMD_FLOAT       = 0,
    // Float UNITS against 2*SIMD float ELEMENTS. **0 = NOT BUILT**, the same
    // spelling `FLANES` uses on the SIMT PE; it used to mean "one per element",
    // so the same 0 described opposite machines on the two cores. A build that
    // wants one per element says 2*SIMD, and a float GROUP asked for with no
    // units is refused at elaboration.
    parameter integer SIMD_FLOAT_LANES = 0,
    // The float groups. FALU -- mul/add/sub/fma/min/max/compare -- is what a
    // SIMD extension IS, so it follows SIMD_FLOAT. The seeds and the accumulator
    // are additions, priced separately, and the eight SIMD PEs of a mesh may
    // carry different combinations.
    parameter integer SIMD_FALU      = 1,
    // A UNIT COUNT, not a boolean: 0 builds no seeds, N builds N seed units out
    // of SIMD_FLOAT_LANES. A row measured when this was a boolean is NOT
    // comparable -- 1 meant "every lane", which is N = SIMD_FLOAT_LANES here.
    parameter integer SIMD_FSFU      = 0,
    parameter integer SIMD_FACC      = 0,
    parameter integer SIMD_FCVT      = 0,
    parameter integer SIMD_F16       = 1,
    parameter integer SIMD_F32       = 1,
    parameter integer SIMD_NPART     = 16,
    parameter         SIMD_USE_DSP   = "yes",
    parameter         SIMD_VREG_PRIM = "distributed",
    parameter         MEM_PRIM      = "block"
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

    output wire                  disp_wr,
    output wire                  disp_fire,
    input  wire                  disp_ready,
    output wire [2:0]            disp_sel,
    output wire [POS_WIDTH-1:0]  disp_dx,
    output wire [POS_WIDTH-1:0]  disp_dy,
    output wire [7:0]            disp_txn,
    output wire                  disp_last,
    output wire [2:0]            disp_rsvd,
    output wire [31:0]           disp_data,

    output wire                  sig_pop,
    input  wire [7:0]            ctl_sig_cnt,
    input  wire                  ctl_sig_ovf,
    input  wire [7:0]            ctl_sig_code,
    input  wire [7:0]            ctl_sig_id,
    input  wire [31:0]           ctl_sig_arg,

    // ---- retirement, for co-simulation ----
    output wire        retire_valid,
    output wire [31:0] retire_pc,
    output wire [4:0]  retire_rd,
    output wire [31:0] retire_val,
    output wire [31:0] cycle_ctr,
    output wire [31:0] instret_ctr,

    // ---- the vector scratchpad's NoC window, at SIMD_EN only ----
    input  wire        vspad_en,
    input  wire [3:0]  vspad_we,
    input  wire [$clog2(SIMD_VSPAD*SIMD_LANES)-1:0] vspad_word,
    input  wire [31:0] vspad_wdata
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

    // ---- the SIMD extension's wires ----
    wire        x_vec, d_vec_ld;
    wire [31:0] x_instr;
    wire        vec_stall, vec_fault, vec_illegal, vec_misalign, vec_w_valid;
    wire [31:0] vec_w_val;
    wire        base_stall;
    wire        vsp_st_valid;
    wire [31:0] vsp_st_addr, vsp_st_data;
    wire [3:0]  vsp_st_be;

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

    // A vector load one instruction behind a scalar store takes a bubble, not a
    // stall, and the difference is a deadlock: the vector unit's stall holds the
    // MEM stage too, so a stall waiting on a store IN that stage waits forever.
    // A bubble holds decode and lets MEM drain, which is what the load-use
    // hazard beside it already does. `x_store` rather than "a store to the
    // vector window" on purpose: the region needs the EX adder's output, and
    // that would put the ALU on the fetch address's path to save a cycle in a
    // sequence nothing runs.
    wire dsp_bubble = (SIMD_EN != 0) && d_vec_ld && x_store;

    wire stall_d = (
        dsp_bubble
        || (
            d_valid
            && (
                ((FWD_X != 0) ? (hz1 && x_load) : hz1)
                || (hz2 && m_load)
            )
        )
    );

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
    always @(posedge clk) begin
        if (rf_we && (rf_wa == 5'd10)) begin
            a0_q <= rf_wd;
        end
    end

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
            if (run_q) begin
                cyc_q <= cyc_q + 32'd1;
            end
            if (retire_valid) begin
                ret_q <= ret_q + 32'd1;
            end
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

    rv_id #(.FWD_X(FWD_X), .SIMD_EN(SIMD_EN), .SIMD_FLOAT(SIMD_FLOAT)) u_id (
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
        .x_pred_taken(x_pred_taken), .x_pred_target(x_pred_target),
        .x_vec(x_vec), .x_instr(x_instr), .d_vec_ld(d_vec_ld)
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

    rv_mem #(.SPAD_WORDS(SPAD_WORDS), .POS_WIDTH(POS_WIDTH),
             .SIMD_EN(SIMD_EN)) u_mem (
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
        .disp_wr(disp_wr), .disp_fire(disp_fire), .disp_ready(disp_ready),
        .disp_sel(disp_sel), .disp_dx(disp_dx), .disp_dy(disp_dy),
        .disp_txn(disp_txn), .disp_last(disp_last), .disp_rsvd(disp_rsvd),
        .disp_data(disp_data),
        .sig_pop(sig_pop), .ctl_sig_cnt(ctl_sig_cnt),
        .ctl_sig_ovf(ctl_sig_ovf), .ctl_sig_code(ctl_sig_code),
        .ctl_sig_id(ctl_sig_id), .ctl_sig_arg(ctl_sig_arg),
        .ctl_coreid(coreid), .ctl_arg(arg), .ctl_cycle(cyc_q),
        .ctl_instret(ret_q), .ctl_cause(cause_q), .ctl_wr_out(wr_out),
        .vec_stall(vec_stall), .vec_fault(vec_fault),
        .vec_w_valid(vec_w_valid), .vec_w_val(vec_w_val),
        .base_stall(base_stall),
        .vsp_st_valid(vsp_st_valid), .vsp_st_addr(vsp_st_addr),
        .vsp_st_be(vsp_st_be), .vsp_st_data(vsp_st_data),
        .stall_m(stall_m), .wstage_val(wstage_val),
        .w_valid(w_valid), .w_rd(w_rd), .w_wen(w_wen), .w_val(w_val),
        .w_pc(w_pc)
    );

    // ---- the SIMD extension --------------------------------------------------
    // It sees the EX stage exactly as the data arrays do: the instruction word,
    // the ALU's own sum as an address, and rs1's value for a broadcast. Its
    // `x_hold` is the MEM stage's OWN stall, not the combined one -- feeding it
    // the combined signal would make its stall an input to itself.
    generate
    // `g_simd.u_khs`, not `g_dsp.u_khd`. This PE is a CPU with SIMD
    // instructions; naming its extension after a DSP is what the class rename
    // exists to stop. The OOC census path moved with it.
    if (SIMD_EN != 0) begin : g_simd
        khs_unit #(.SIMD(SIMD_LANES), .VREGS(SIMD_VREGS), .NACC(SIMD_NACC),
                   .VSPAD_ENTRIES(SIMD_VSPAD), .MULS(SIMD_MULS),
                   .HAS_SHIFT(SIMD_SHIFT), .HAS_PERM(SIMD_PERM),
                   .PERM_UNITS(SIMD_PERM_UNITS),
                   .SHIFT_UNITS(SIMD_SHIFT_UNITS),
                   .DOT_DSP(SIMD_DOTDSP),
                   .HAS_FLOAT(SIMD_FLOAT), .FLOAT_LANES(SIMD_FLOAT_LANES),
                   .HAS_FALU(SIMD_FALU), .FSFU_UNITS(SIMD_FSFU),
                   .HAS_FACC(SIMD_FACC), .HAS_FCVT(SIMD_FCVT),
                   .HAS_F16(SIMD_F16), .HAS_F32(SIMD_F32),
                   .NPART(SIMD_NPART),
                   .WB_STAGE(SIMD_WB),
                   .USE_DSP(SIMD_USE_DSP), .MEM_PRIM(MEM_PRIM),
                   .VREG_PRIM(SIMD_VREG_PRIM)) u_khs (
            .clk(clk), .resetn(resetn),
            .x_valid(x_vec && x_valid), .x_instr(x_instr),
            .x_addr(ex_addr), .x_xdata(x_op1), .x_hold(base_stall),
            .stall(vec_stall), .x_illegal(vec_illegal),
            .x_misalign(vec_misalign),
            .w_sc_valid(vec_w_valid), .w_sc(vec_w_val),
            .dbg_wr_valid(), .dbg_wr_vd(), .dbg_wr_data(),
            .noc_en(vspad_en), .noc_we(vspad_we), .noc_word(vspad_word),
            .noc_wdata(vspad_wdata),
            .sc_st_valid(vsp_st_valid), .sc_st_addr(vsp_st_addr),
            .sc_st_be(vsp_st_be), .sc_st_data(vsp_st_data)
        );
        assign vec_fault = vec_illegal || vec_misalign;
    end else begin : g_nodsp
        assign vec_stall    = 1'b0;
        assign vec_fault    = 1'b0;
        assign vec_illegal  = 1'b0;
        assign vec_misalign = 1'b0;
        assign vec_w_valid  = 1'b0;
        assign vec_w_val    = 32'd0;
    end
    endgenerate

    rv_wb u_wb (
        .w_valid(w_valid), .w_rd(w_rd), .w_wen(w_wen), .w_val(wstage_val),
        .w_pc(w_pc),
        .rf_we(rf_we), .rf_wa(rf_wa), .rf_wd(rf_wd),
        .retire_valid(retire_valid), .retire_pc(retire_pc),
        .retire_rd(retire_rd), .retire_val(retire_val)
    );

endmodule

`default_nettype wire

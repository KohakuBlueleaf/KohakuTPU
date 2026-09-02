// SysCore's RV64I pipeline: F, D, E, M, W.
//
// THE REGISTER FILE'S READ LATENCY SETS THE SHAPE. Addresses go out in D and
// data arrives in E, and a write landing on that same edge is not seen -- so
// THREE instructions ahead are unforwarded by the array and all three are
// forwarded here. That is the cost of putting the register file in BRAM, and it
// is paid once in a mux rather than continuously in LUTRAM.
//
// NO LOAD-USE STALL. A load issues its address in E and its data arrives in M,
// which is the same cycle the next instruction is in E -- so the forward from M
// covers it and the pipeline never stalls for a load.
//
// A BRANCH RESOLVES IN E, so the two instructions behind it are killed. There is
// no predictor: a control processor's branches are loop backedges, and a
// predictor is LUT that the budget would rather spend elsewhere.

`default_nettype none

module rv64_core #(
    parameter [63:0] RESET_PC = 64'h0000_0000_0000_0000,
    // MEASURED: `distributed` 323.7 MHz / 4,704 LUT / 0 failing paths, against
    // `block` 264.1 / 4,699 / 67. BRAM clock-to-out was the binding path and no
    // logic restructuring moves it; 5 LUT buys 59.6 MHz.
    parameter        MEM_PRIM = "distributed",
    // A mesh compute unit has no second writer to race, so it can drop the A
    // group. SysCore cannot: staging L2 is single-reader, so without atomics
    // the machine cannot express mutual exclusion or a shared counter at all.
    parameter integer HAS_ATOMIC = 1,
    // Physical address width; it sizes satp.PPN, and bits beyond are WARL zero.
    parameter integer PADDR_W = 40
)(
    input  wire        clk,
    input  wire        resetn,

    output wire [63:0] imem_addr,
    input  wire [31:0] imem_data,
    // The wrapper cannot name a physical fetch address yet. Held while a
    // fetch translation is being resolved; zero when translation is off.
    input  wire        imem_stall,
    input  wire        imem_fault,    // with `imem_data`: the fetch page-faulted

    output wire [63:0] dmem_addr,
    output wire [63:0] dmem_wdata,
    output wire [7:0]  dmem_wstrb,
    output wire        dmem_re,
    // A memory op is in E. DECODE ONLY -- no address, so a wrapper can decide to
    // stall without the effective-address adder in the path. The address decode
    // reaching `stall` put 14 logic levels in front of every pipeline register's
    // enable; the budget is 11.
    output wire        dmem_req,
    output wire        dmem_st,
    input  wire [63:0] dmem_rdata,
    // Held high while the access is not yet answered. The request is issued in
    // E, so E is what holds -- M and W keep draining, per the drain rule.
    input  wire        dmem_stall,
    // A translation fault from the wrapper's MMU. Held as a level while the
    // request stands, because the trap is taken at the next instruction
    // boundary and not on the cycle the walk gave up.
    input  wire        dmem_fault,
    input  wire [3:0]  dmem_fault_cause,

    // The node's doorbell lands on `irq_soft`; `irq_ext` is the summary line.
    input  wire        irq_ext,
    input  wire        irq_soft,

    // Privilege and translation, for the wrapper's MMU.
    output wire [1:0]  priv_o,
    output wire [63:0] satp_o,
    output wire        sum_o,
    output wire        mxr_o,
    output wire        sfence_o,
    output wire        fence_i_o,     // FENCE.I retired: invalidate the I-cache
    output wire        priv_settle_o, // hold fetch: `priv_o` lands next edge

    // The wrapper's exit-region store, and the host's stop. Program exit is a
    // store rather than ECALL because ECALL has to remain a call.
    input  wire        ext_halt,

    output reg         halted,
    output reg  [1:0]  halt_cause,   // 1 ECALL, 2 EBREAK, 3 illegal/misaligned
    output reg  [63:0] halt_pc,
    output wire [63:0] dbg_pc,
    output wire        dbg_retire
);
`include "rv64_defs.vh"

    // Tying `e_amo` off is enough: `amo_active` goes constant-0, the FSM never
    // leaves A_IDLE, and synthesis constant-propagates the whole block away.
    localparam AMO_EN = (HAS_ATOMIC != 0);

    // ---- F ------------------------------------------------------------------
    reg  [63:0] pc;
    wire [63:0] pc_next;
    wire        redirect;
    wire [63:0] redirect_pc;
    // Declared here, assigned where they are computed: xvlog rejects a net
    // read above its declaration.
    wire        e_kill, taken, csr_wait, load_use;
    wire [63:0] target, ea, eff;
    reg         e_valid;
    reg  [63:0] e_pc;
    reg         e_br, e_jal, e_jalr;

    // THE ONLY STALL IN THE PIPELINE. RV64M is multi-cycle, so E holds and F/D
    // hold with it; nothing else stops this core, and a load in particular does
    // not (its data arrives in M, which is when the consumer needs it).
    wire        stall;
    wire        go = !halted && !stall;
    // TWO DIFFERENT STALLS. `stall` holds the whole pipeline because a
    // multi-cycle unit owns E. `bubble` holds only F and D and lets E drain,
    // because the load in E must reach M for its data to exist at all.
    wire        bubble;
    wire        fd_go = go && !bubble;

    assign imem_addr = pc;

    // A REDIRECT MUST SURVIVE A BUBBLE. `go` retires E but `fd_go` (go && !bubble)
    // gates `pc`, so an E-stage redirect (`e_kill`) retiring while F is bubbled by
    // `imem_stall` (DRAM/I-cache only) loses BOTH halves of its squash: `pc` never
    // takes the target, and the wrong-path word held in D keeps `d_valid` and later
    // issues -- decoded illegal, it traps. Latch the target AND kill D, apply at
    // fd_go. `d_redir` is excluded: it is the branch, not its shadow.
    reg         redir_pend;
    reg  [63:0] redir_pend_pc;
    wire        take_redir    = redirect || redir_pend;
    wire [63:0] take_redir_pc = redir_pend ? redir_pend_pc : redirect_pc;
    assign pc_next = take_redir ? take_redir_pc : (pc + 64'd4);

    reg d_valid;
    reg [63:0] d_pc;
    reg [31:0] d_instr_hold;
    reg        d_fault_hold;
    reg        d_hold_v;
    // imem_data is the read of the PREVIOUS fetch, but imem_stall is the CURRENT
    // fetch's -- a one-cycle skew. Gate the D-word capture on the stall that
    // matches the data, or a ret whose shadow misses on the next line has its
    // word (valid, already on the bus) skipped and then overwritten by the fill.
    reg        imem_stall_q;
    always @(posedge clk) begin
        imem_stall_q <= !resetn ? 1'b1 : imem_stall;
    end
    always @(posedge clk) begin
        if (!resetn) begin
            pc         <= RESET_PC;
            d_valid    <= 1'b0;
            d_pc       <= RESET_PC;
            d_hold_v   <= 1'b0;
            redir_pend <= 1'b0;
        end
        else if (fd_go) begin
            pc         <= pc_next;
            d_valid    <= !take_redir;
            d_pc       <= pc;
            d_hold_v   <= 1'b0;
            redir_pend <= 1'b0;
        end
        else begin
            // F held: pin an E-stage redirect firing now, and squash the wrong-path
            // instruction it left in D (first wins; younger is already killed).
            if (e_kill && !redir_pend) begin
                redir_pend    <= 1'b1;
                redir_pend_pc <= redirect_pc;
                d_valid       <= 1'b0;
            end
            // Capture D's word once, gated on imem_stall_q (the stall of the fetch
            // that produced this word): under a live imem_stall the address is not
            // yet physical, so its bus word belongs to nowhere and pins garbage.
            if ((stall || bubble) && !d_hold_v && !imem_stall_q) begin
                d_instr_hold <= imem_data;
                d_fault_hold <= imem_fault;
                d_hold_v     <= 1'b1;
            end
        end
    end

    // A FAULTED FETCH DECODES AS A NOP AND TRAPS IN E. The word on the bus is
    // whatever the poisoned page returned; decoded, it could issue a load or
    // redirect fetch before the trap lands.
    wire        d_fault = d_hold_v ? d_fault_hold : imem_fault;
    wire [31:0] d_instr = d_fault  ? 32'h0000_0013
                        : d_hold_v ? d_instr_hold : imem_data;

    // ---- D ------------------------------------------------------------------
    wire [4:0]  rs1_a, rs2_a, rd_a;
    wire [63:0] imm_d;
    wire [3:0]  alu_op_d;
    wire        alu_word_d, op1_pc_d, op2_imm_d, wr_reg_d;
    wire        br_d, jal_d, jalr_d, ld_d, st_d, fence_d, fence_i_d, ecall_d, ebreak_d;
    wire [2:0]  mem_f3_d;
    wire        illegal_d;

    wire md_d, amo_d;
    wire [4:0] amo_op_d;
    wire       csr_d, csr_wr_d, csr_imm_d, mret_d, wfi_d, sret_d, sfence_d;
    wire [11:0] csr_addr_d;

    rv64_decode u_dec (
        .instr(d_instr),
        .rs1(rs1_a), .rs2(rs2_a), .rd(rd_a), .imm(imm_d),
        .alu_op(alu_op_d), .alu_word(alu_word_d),
        .op1_pc(op1_pc_d), .op2_imm(op2_imm_d),
        .wr_reg(wr_reg_d), .is_muldiv(md_d),
        .is_branch(br_d), .is_jal(jal_d), .is_jalr(jalr_d),
        .is_load(ld_d), .is_store(st_d),
        .is_amo(amo_d), .amo_op(amo_op_d), .mem_f3(mem_f3_d),
        .is_fence(fence_d), .is_fence_i(fence_i_d),
        .is_ecall(ecall_d), .is_ebreak(ebreak_d),
        .is_mret(mret_d), .is_wfi(wfi_d),
        .is_sret(sret_d), .is_sfence(sfence_d),
        .is_csr(csr_d), .csr_wr(csr_wr_d), .csr_imm(csr_imm_d),
        .csr_addr(csr_addr_d),
        .illegal(illegal_d)
    );

    // ---- prediction ---------------------------------------------------------
    // The lookup goes out with the FETCH address and the answer arrives with the
    // instruction, so a predicted-taken branch redirects from D and kills one
    // instruction where an unpredicted one resolves in E and kills two.
    wire        pr_taken;
    wire [63:0] pr_target;

    // THE STACK DOES NOT WAIT FOR `imem_stall`. `fd_go` carries the fetch
    // stall -- the I-cache tag compare behind the privilege mux -- and through
    // the push it reached the RAS enables at 12 levels, -0.189 ns at the node.
    // The push or pop happens on the first cycle the word in D is real and E
    // is not held; `d_seen` stops it repeating while D waits for fetch. A word
    // a redirect then kills leaves a wrong entry, which costs a mispredict and
    // never correctness.
    reg  d_seen;
    wire d_word  = d_valid && go && !load_use && !d_seen
                && (d_hold_v || !imem_stall_q);
    wire d_call  = d_word && (jal_d || jalr_d)
                && ((rd_a == 5'd1) || (rd_a == 5'd5));
    wire d_ret   = d_word && jalr_d && (rd_a == 5'd0)
                && ((rs1_a == 5'd1) || (rs1_a == 5'd5));
    always @(posedge clk) begin
        if (!resetn || fd_go) begin
            d_seen <= 1'b0;
        end else if (d_word) begin
            d_seen <= 1'b1;
        end
    end

    // NOT `!redirect`: `redirect` is driven by this, so reading it closes a
    // combinational loop. `d_redir` carries the `!e_redir` term instead.
    wire d_predict = d_valid && pr_taken && (br_d || jal_d || jalr_d);

    // JALR's check rides one cycle behind its bubble and updates the predictor
    // from here; declared with its reader (the block that drives it is below)
    reg         jchk_v, jchk_pt;
    reg  [63:0] jchk_tgt, jchk_ptgt, jchk_pc;

    rv64_bpred u_bp (
        .clk(clk), .resetn(resetn),
        .q_en(1'b1), .q_addr(pc), .q_pc(d_pc),
        .q_taken(pr_taken), .q_target(pr_target),
        .p_call(d_call), .p_ret(d_ret), .p_link(d_pc + 64'd4),
        .u_valid(jchk_v || (e_valid && (e_br || e_jal) && !stall)),
        .u_pc(jchk_v ? jchk_pc : e_pc),
        .u_taken(jchk_v ? 1'b1 : taken),
        .u_is_jump(jchk_v ? 1'b1 : e_jal),
        .u_is_cond(jchk_v ? 1'b0 : e_br),
        .u_target(jchk_v ? jchk_tgt : e_btgt)
    );

    wire [63:0] rf_rs1, rf_rs2;
    wire        w_we;
    wire [4:0]  w_rd;
    wire [63:0] w_data;

    rv64_regfile #(.MEM_PRIM(MEM_PRIM)) u_rf (
        .clk(clk),
        .wr_en(w_we), .wr_addr(w_rd), .wr_data(w_data),
        .rs1_addr(rs1_a), .rs2_addr(rs2_a),
        .rs1_data(rf_rs1), .rs2_data(rf_rs2)
    );

    reg [63:0] e_imm;
    reg [4:0]  e_rs1, e_rs2, e_rd;
    reg [3:0]  e_alu_op;
    reg        e_word, e_op1pc, e_op2imm, e_wr;
    reg        e_ld, e_st, e_ecall, e_ebreak, e_ill;
    reg        e_md, e_amo;
    reg        e_csr, e_csr_wr, e_csr_imm, e_mret, e_wfi, e_sret, e_sfence;
    reg        e_fence_i;
    reg        e_ifault;
    reg [11:0] e_csr_addr;
    reg        e_pred_t;
    reg [63:0] e_pred_tgt;
    // the branch/JAL target and its prediction match, computed in D where pc
    // and imm are registers: E's redirect carries no adder or comparator
    reg [63:0] e_btgt, e_pc4;
    reg [4:0]  e_amo_op;
    reg [2:0]  e_f3;

    // THE INSTRUCTION ONLY REACHES THE LOW 21 BITS (B imm 13, J 21): the high
    // half is d_pc +/- 1 off the REGISTER, picked by the low add's carry, so
    // the imem -> decode cone ends in a 21-bit add (3 CARRY8).

    always @(posedge clk) begin
        if (!resetn) begin
            e_valid <= 1'b0;
        end
        else if (go) begin
            // `e_redir`, NOT `redirect`: a D-stage prediction kills the FETCH
            // behind the branch, and the branch itself must reach E to be
            // checked against what the predictor said.
            e_valid   <= d_valid && !e_kill && !bubble;
            e_pred_t  <= d_predict;
            e_pred_tgt<= pr_target;
            e_btgt    <= d_pc + imm_d;
            e_pc4     <= d_pc + 64'd4;
            e_md      <= md_d;
            e_pc     <= d_pc;
            e_imm    <= imm_d;
            e_rs1    <= rs1_a;
            e_rs2    <= rs2_a;
            e_rd     <= rd_a;
            e_alu_op <= alu_op_d;
            e_word   <= alu_word_d;
            e_op1pc  <= op1_pc_d;
            e_op2imm <= op2_imm_d;
            e_wr     <= wr_reg_d;
            e_br     <= br_d;
            e_jal    <= jal_d;
            e_jalr   <= jalr_d;
            e_ld     <= ld_d;
            e_st     <= st_d;
            e_amo    <= amo_d && AMO_EN;
            e_amo_op <= amo_op_d;
            e_f3     <= mem_f3_d;
            e_ecall  <= ecall_d;
            e_ebreak <= ebreak_d;
            e_ill    <= illegal_d || (amo_d && !AMO_EN);
            e_csr      <= csr_d;
            e_csr_wr   <= csr_wr_d;
            e_csr_imm  <= csr_imm_d;
            e_csr_addr <= csr_addr_d;
            e_mret     <= mret_d;
            e_wfi      <= wfi_d;
            e_sret     <= sret_d;
            e_sfence   <= sfence_d;
            e_fence_i  <= fence_i_d;
            e_ifault   <= d_fault;
        end
    end

    // ---- E: forwarding ------------------------------------------------------
    // Three sources, and the third exists because the array is read-first: an
    // instruction whose write lands on the same edge as this one's read is not
    // in the value the array returned.
    reg  [63:0] m_val;
    reg  [4:0]  m_rd;
    reg         m_wr, m_ld;
    reg  [63:0] w_val_q;
    reg  [4:0]  w_rd_q;
    reg         w_wr_q;

    // EVERY FORWARD SOURCE IS A REGISTER, and the SELECT is precomputed in D.
    // Comparing in E made the comparator itself the binding path (`wb_rd_reg ->
    // m_val_reg`, 19 levels, -0.338 ns), so D compares and E is left a mux.
    //
    // D compares against what E, M and W hold NOW, because the pipeline shifts
    // exactly one stage per cycle: this instruction's M, W and W-1 sources ARE
    // the current E, M and W. Held every cycle D is held, so the value captured
    // on the final `go` is right after a stall of any length.
    reg e_s1_m, e_s1_w, e_s1_q;
    reg e_s2_m, e_s2_w, e_s2_q;

    // A load is excluded at M only: by W its data has been aligned and extended.
    function sel_m; input [4:0] a;
        sel_m = (a != 5'd0) && e_valid && e_wr && !e_ld && (e_rd == a);
    endfunction
    function sel_w; input [4:0] a;
        sel_w = (a != 5'd0) && m_wr && (m_rd == a);
    endfunction
    function sel_q; input [4:0] a;
        sel_q = (a != 5'd0) && w_we && (w_rd == a);
    endfunction

    always @(posedge clk) begin
        if (go) begin
            e_s1_m <= sel_m(rs1_a);
            e_s1_w <= sel_w(rs1_a);
            e_s1_q <= sel_q(rs1_a);
            e_s2_m <= sel_m(rs2_a);
            e_s2_w <= sel_w(rs2_a);
            e_s2_q <= sel_q(rs2_a);
        end
    end

    wire [63:0] op_rs1_raw = e_s1_m ? m_val : e_s1_w ? w_data
                           : e_s1_q ? w_val_q : rf_rs1;
    wire [63:0] op_rs2_raw = e_s2_m ? m_val : e_s2_w ? w_data
                           : e_s2_q ? w_val_q : rf_rs2;

    // THE FORWARDING NETWORK IS ONLY VALID IN THE CYCLE THE INSTRUCTION ENTERS
    // E. M and W keep draining while E is held, so the sources these selects
    // point at move on and the operands drift. AMO latched its own copy for
    // this reason; once memory can stall, EVERY multi-cycle E operation needs
    // it -- a stalled store recomputed `ea` and faulted on a spurious misalign.
    reg        op_held;
    reg [63:0] op1_h, op2_h;
    always @(posedge clk) begin
        if (!resetn) begin
            op_held <= 1'b0;
        end else if (stall && !op_held) begin
            op1_h   <= op_rs1_raw;
            op2_h   <= op_rs2_raw;
            op_held <= 1'b1;
        end else if (!stall) begin
            op_held <= 1'b0;
        end
    end

    wire [63:0] op_rs1 = op_held ? op1_h : op_rs1_raw;
    wire [63:0] op_rs2 = op_held ? op2_h : op_rs2_raw;

    wire [63:0] alu_a = e_op1pc  ? e_pc  : op_rs1;
    wire [63:0] alu_b = e_op2imm ? e_imm : op_rs2;
    wire [63:0] alu_y;

    rv64_alu u_alu (
        .op(e_alu_op), .word(e_word), .a(alu_a), .b(alu_b), .y(alu_y)
    );

    // ---- E: RV64M -----------------------------------------------------------
    // START ONCE. `md_fired` is what stops the unit being re-launched on every
    // stalled cycle, which would restart the divide forever.
    reg  md_fired;
    wire md_busy, md_done;
    wire [63:0] md_y;
    wire md_go = e_valid && e_md && !md_fired && !md_busy && !halted;

    rv64_muldiv u_md (
        .clk(clk), .resetn(resetn),
        .start(md_go), .f3(e_f3), .word(e_word), .a(op_rs1), .b(op_rs2),
        .busy(md_busy), .done(md_done), .y(md_y)
    );

    always @(posedge clk) begin
        if (!resetn) begin
            md_fired <= 1'b0;
        end else if (md_done) begin
            md_fired <= 1'b0;
        end else if (md_go) begin
            md_fired <= 1'b1;
        end
    end

    // ---- E: RV64A -----------------------------------------------------------
    // AN AMO IS READ, MODIFY, WRITE, and the pipeline holds through all three.
    // LR/SC carry a single reservation: this core is one hart, so the only way
    // to lose it is another LR or any SC, which is what the spec allows.
    localparam [1:0] A_IDLE = 2'd0, A_RD = 2'd1, A_WR = 2'd2, A_FIN = 2'd3;
    localparam [4:0] AMO_LR = 5'b00010, AMO_SC = 5'b00011, AMO_SWAP = 5'b00001;
    localparam [4:0] AMO_ADD = 5'b00000, AMO_XOR = 5'b00100, AMO_AND = 5'b01100;
    localparam [4:0] AMO_OR = 5'b01000, AMO_MIN = 5'b10000, AMO_MAX = 5'b10100;
    localparam [4:0] AMO_MINU = 5'b11000, AMO_MAXU = 5'b11100;

    reg [1:0]  a_state;
    reg [63:0] a_old, a_res;
    reg        resv_v;
    reg [63:0] resv_a;
    // LATCHED ON ENTRY. Both operands come through the forwarding network, and
    // the M and W registers keep shifting while E is held -- so recomputing
    // them each cycle changes the address underneath a running AMO.
    reg [63:0] a_addr, a_srcq;

    wire amo_w = (e_f3 == 3'b010);
    wire [63:0] a_loaded = amo_w ? {{32{a_old[31]}}, a_old[31:0]} : a_old;
    wire [63:0] a_src    = amo_w ? {{32{a_srcq[31]}}, a_srcq[31:0]} : a_srcq;

    wire a_lt  = amo_w ? ($signed(a_loaded[31:0]) < $signed(a_src[31:0]))
                       : ($signed(a_loaded) < $signed(a_src));
    wire a_ltu = amo_w ? (a_loaded[31:0] < a_src[31:0])
                       : (a_loaded < a_src);

    reg [63:0] a_new;
    always @(*) begin
        case (e_amo_op)
            AMO_SWAP: a_new = a_src;
            AMO_ADD:  a_new = a_loaded + a_src;
            AMO_XOR:  a_new = a_loaded ^ a_src;
            AMO_AND:  a_new = a_loaded & a_src;
            AMO_OR:   a_new = a_loaded | a_src;
            AMO_MIN:  a_new = a_lt  ? a_loaded : a_src;
            AMO_MAX:  a_new = a_lt  ? a_src : a_loaded;
            AMO_MINU: a_new = a_ltu ? a_loaded : a_src;
            AMO_MAXU: a_new = a_ltu ? a_src : a_loaded;
            default:  a_new = a_src;
        endcase
    end

    wire is_lr = e_amo_op == AMO_LR;
    wire is_sc = e_amo_op == AMO_SC;
    wire sc_ok = resv_v && (resv_a == a_addr);
    wire amo_active = e_valid && e_amo && !halted;

    always @(posedge clk) begin
        if (!resetn) begin
            a_state <= A_IDLE;
            resv_v  <= 1'b0;
        end
        else if (amo_active && !dmem_stall) begin
            case (a_state)
                A_IDLE: begin
                    a_state <= A_RD;
                    a_addr  <= ea;
                    a_srcq  <= op_rs2;
                end
                A_RD: begin
                    a_old <= dmem_rdata >> {a_addr[2:0], 3'b000};
                    if (is_lr) begin
                        resv_v  <= 1'b1;
                        resv_a  <= a_addr;
                        a_state <= A_FIN;
                    end
                    else if (is_sc && !sc_ok) begin
                        a_res   <= 64'd1;      // 1 is failure
                        resv_v  <= 1'b0;
                        a_state <= A_FIN;
                    end
                    else begin
                        a_state <= A_WR;
                    end
                end
                A_WR: begin
                    a_res   <= is_sc ? 64'd0 : a_loaded;
                    resv_v  <= 1'b0;
                    a_state <= A_FIN;
                end
                default: a_state <= A_IDLE;
            endcase
        end
        // ONLY when the AMO is gone, never merely because memory is slow: a
        // reset here on a stalled cycle restarts the sequence and re-issues
        // every phase it had already completed.
        else if (!amo_active) begin
            a_state <= A_IDLE;
        end
    end

    wire a_done = amo_active && (a_state == A_FIN);
    wire [63:0] a_result = is_lr ? a_loaded : a_res;

    wire mem_wait;   // driven below, where `misalign` exists

    assign stall = (e_valid && e_md && !md_done)
                || (amo_active && (a_state != A_FIN))
                || csr_wait
                || mem_wait;

    wire [63:0] csr_rdata, mtvec_v, mepc_v, irq_cause_v;
    wire        csr_ill, irq_pend, tvec_installed;

    wire [63:0] e_result = e_md                 ? md_y
                         : e_amo                ? a_result
                         : e_csr                ? csr_rdata
                         : (e_jal || e_jalr)    ? (e_pc + 64'd4)
                                                : alu_y;

    // ---- E: branch resolve --------------------------------------------------
    wire beq  = (op_rs1 == op_rs2);
    wire blt  = ($signed(op_rs1) < $signed(op_rs2));
    wire bltu = (op_rs1 < op_rs2);
    reg  br_take;
    always @(*) begin
        case (e_f3)
            3'b000:  br_take = beq;
            3'b001:  br_take = !beq;
            3'b100:  br_take = blt;
            3'b101:  br_take = !blt;
            3'b110:  br_take = bltu;
            default: br_take = !bltu;
        endcase
    end

    assign taken  = e_valid && ((e_br && br_take) || e_jal || e_jalr);
    // JALR's register-relative target: only ever the D input of jchk_tgt
    wire [63:0] jalr_tgt = (op_rs1 + e_imm) & ~64'd1;
    assign target = (e_br || e_jal) ? e_btgt : jalr_tgt;

    // E REDIRECTS ONLY ON A MISPREDICTED BRANCH OR JAL, register against
    // register (target and its match come from D); a JALR checks a cycle later
    // against its registered target (jchk below) behind its own bubble.
    // e_pred_tgt, not live pr_target: a BTB update must not move it mid-hold.
    wire e_tgt_ne = (e_btgt != e_pred_tgt);
    wire mispred  = e_valid && (e_br || e_jal) && !stall
                 && ((taken != e_pred_t)
                     || (taken && e_tgt_ne));
    wire e_redir  = mispred && !halted;
    wire [63:0] e_redir_pc = taken ? e_btgt : e_pc4;

    // D redirects on a prediction, which kills the one instruction already
    // fetched behind it. The branch itself carries on into E to be checked.
    wire d_redir = d_predict && !halted && !e_redir;

    // A trap outranks a mispredict, which outranks a prediction. Declared here
    // and driven below, where `ea` exists to become `mtval`.
    wire        trap_redir, trap_take, mret_take, sret_take;
    wire [63:0] sepc_v;
    wire [1:0]  priv_v;
    wire        can_deleg, dg_ill, dg_brk, dg_ldm, dg_stm, dg_pgf, dg_ecall,
                dg_irq;
    wire [63:0] trap_pc_next;

    // ---- the JALR check, one cycle behind its bubble ------------------------
    // E is empty when jmis fires (bubble below), so nothing needs a late kill:
    // no dmem, no AMO, no M write is in flight for the wrong path.
    wire jmis = jchk_v && !halted && (!jchk_pt || (jchk_tgt != jchk_ptgt));
    always @(posedge clk) begin
        if (!resetn) begin
            jchk_v <= 1'b0;
            jchk_pt <= 1'b0;
            jchk_tgt <= 64'd0; jchk_ptgt <= 64'd0; jchk_pc <= 64'd0;
        end else if (go) begin
            jchk_v    <= e_valid && e_jalr && !trap_take;
            jchk_pt   <= e_pred_t;
            jchk_tgt  <= jalr_tgt;
            jchk_ptgt <= e_pred_tgt;
            jchk_pc   <= e_pc;
        end
    end

    assign redirect    = jmis || trap_redir || e_redir || d_redir;
    assign redirect_pc = jmis       ? jchk_tgt
                       : trap_redir ? trap_pc_next
                       : e_redir    ? e_redir_pc : pr_target;

    // Everything behind a trap dies with it, exactly as it does behind a
    // mispredict.
    assign e_kill = e_redir || trap_redir || jmis;

    // ---- E: the data address ------------------------------------------------
    assign ea = op_rs1 + e_imm;
    // Four bits: as `3'd8` the double-word size truncated to 0 and a
    // misaligned 8-byte access never trapped.
    wire [3:0]  sz = (
        (e_f3[1:0] == 2'b00)   ? 4'd1
        : (e_f3[1:0] == 2'b01) ? 4'd2
        : (e_f3[1:0] == 2'b10) ? 4'd4
        : 4'd8
    );
    wire misalign = (
        ((sz == 4'd2) && eff[0])
        || ((sz == 4'd4) && (|eff[1:0]))
        || ((sz == 4'd8) && (|eff[2:0]))
    );

    // The latched address once the AMO is past its first cycle; `ea` otherwise.
    assign eff = (amo_active && (a_state != A_IDLE)) ? a_addr : ea;

    // NO `misalign` HERE, and that is the whole point: it was the LAST
    // address-derived term in `stall`, and `stall` gates every pipeline
    // register -- 68 failing paths, all of them `wb_val_reg -> u_bp/ras_*`, were
    // this one chain. `dmem_req` is now registers only, so `stall` is too.
    //
    // A misaligned access therefore issues a transaction and then traps. That is
    // harmless: a misaligned store already emits no byte strobes, and a read has
    // no side effect on this fabric.
    assign dmem_req = e_valid && !halted && (e_ld || e_st || e_amo);
    // The same promise as `dmem_req`, for the wrapper's translation: which
    // permission to check, named from decode. `dmem_wstrb` cannot be used --
    // it carries `misalign`, and therefore the address adder.
    assign dmem_st  = e_valid && !halted && (e_st || e_amo);
    assign mem_wait = dmem_stall;

    // ---- E: CSRs, traps and interrupts --------------------------------------
    // The immediate forms put a 5-bit zero-extended value where rs1 would be, so
    // the register NUMBER is the operand.
    // THE WRITE DATA IS REGISTERED, so a CSR instruction holds E for one cycle.
    // Driven combinationally it is `wb_val -> forward mux -> op_rs1 -> next ->
    // EVERY CSR register`: 572 failing paths across nine groups at the node,
    // where the module-level build had passed. CSR instructions are rare enough
    // that a cycle is the cheapest thing to spend.
    wire [63:0] csr_operand = e_csr_imm ? {59'd0, e_rs1} : op_rs1;
    wire        csr_req     = e_valid && e_csr && !halted;

    reg         csr_hold;
    reg [63:0]  csr_wd_q;
    always @(posedge clk) begin
        if (!resetn) begin
            csr_hold <= 1'b0;
        end else if (csr_req && !csr_hold) begin
            csr_hold <= 1'b1;
            csr_wd_q <= csr_operand;
        end else begin
            csr_hold <= 1'b0;
        end
    end

    // The read is unaffected: `rdata` is combinational on the registered address
    // and the write lands at the end of the holding cycle, so a CSR read still
    // returns the pre-write value, which is what the instruction owes.
    assign csr_wait = csr_req && !csr_hold;

    // A TRAP NEEDS AN INSTRUCTION BOUNDARY. `!stall` is what provides it: a
    // multi-cycle op that has started must finish, or its latched operands
    // describe a transaction nobody will ever complete.
    // A privileged instruction below its own level is an illegal instruction,
    // not a silent no-op: that is what stops user code returning to machine
    // mode with `mret` or flushing the TLB out from under the kernel.
    wire priv_ill = (e_mret   && (priv_v != 2'b11))
                 || (e_sret   && (priv_v == 2'b00))
                 || (e_sfence && (priv_v == 2'b00));
    wire ex_ill    = e_ill || (e_csr && csr_ill) || priv_ill;
    // Everything that can kill a CSR write, with nothing address-derived in it.
    // NOT `boundary`: that carries `mem_wait`, the L1 tag compare, and a CSR
    // instruction never waits on memory -- so the term is constant-true for
    // the only instruction this gates, and cost 13 levels into every CSR.
    wire csr_kill  = e_valid && !halted && (ex_ill || irq_pend);
    // `misalign` STAYS COMBINATIONAL HERE, and the two obvious economies are
    // both wrong. A plain register survives its own instruction and traps the
    // next aligned access on it. Latching it with `op_held` instead is only
    // valid where every access stalls -- true of the node wrapper, false of the
    // bare core, where the trap then never fires at all. It is the address
    // adder's low three bits, so the depth is in the fan-out to the CSR file
    // rather than in this term.
    wire ex_ld_mis = e_ld && misalign;
    wire ex_st_mis = (e_st || e_amo) && misalign;
    // `mem_wait` IS SAFE HERE AGAIN. It once carried the wrapper's address
    // decode and cost 200 failing paths; it is now register-derived, and having
    // it back means a misaligned access traps ONCE, after its transaction
    // retires, instead of every cycle it is held.
    wire boundary  = e_valid && !halted && !mem_wait
                  && !(e_valid && e_md && !md_done)
                  && !(amo_active && (a_state != A_FIN));
    wire ex_pgf    = dmem_fault && (e_ld || e_st || e_amo);
    // `boundary` ENTERS ONCE, AT THE END. It carries the L1 tag compare on the
    // address adder; folded in up front it rode through the cause, delegation
    // and vector selects -- 15 levels, WNS -0.804. The raw terms decide WHAT
    // the trap is; `boundary` only decides WHEN.
    wire exc_raw   = e_ifault || e_ecall || e_ebreak || ex_ill || ex_ld_mis
                  || ex_st_mis || ex_pgf;
    wire irq_raw   = irq_pend && !exc_raw && !(e_ld || e_st || e_amo);
    wire exception = boundary && exc_raw;

    // NO HANDLER INSTALLED MEANS BARE METAL. `mtvec` still zero is a program
    // that never set one, and jumping to 0 would silently restart it -- so the
    // inherited halt-and-report stays, and is what every current test relies on.
    wire handler = tvec_installed;
    assign trap_take = boundary && handler && (exc_raw || irq_raw);
    assign mret_take = boundary && e_mret && !trap_take && !priv_ill;
    assign sret_take = boundary && e_sret && !trap_take && !priv_ill;
    wire   fault_halt = !handler && exception;
    // AN INVALIDATION MAY BE SPURIOUS; IT MAY NEVER BE MISSED. Qualifying this
    // with `!trap_take` put the whole trap cone -- and so `handler` and the
    // delegation mux -- into the fetch page register's clock enable, 21 logic
    // levels. A fence that fires on an SFENCE that then traps costs a re-walk.
    assign sfence_o   = boundary && e_sfence;
    assign fence_i_o  = boundary && e_fence_i;

    // ECALL's cause names the mode it came from, which is how one handler tells
    // a user syscall from a supervisor one without reading any other state.
    wire [63:0] ecall_cause = (priv_v == 2'b11) ? 64'd11
                            : (priv_v == 2'b01) ? 64'd9
                                                : 64'd8;
    wire [63:0] trap_cause = irq_raw    ? irq_cause_v
                           : e_ifault  ? 64'd12
                           : ex_ill    ? 64'd2
                           : e_ebreak  ? 64'd3
                           : ex_ld_mis ? 64'd4
                           : ex_st_mis ? 64'd6
                           : ex_pgf    ? {60'd0, dmem_fault_cause}
                                       : ecall_cause;
    // The same priority order as `trap_cause`, but selecting bits the CSR file
    // has already indexed with constants. Indexing `medeleg` with the cause
    // itself puts a 64:1 mux downstream of the address adder.
    wire deleg = can_deleg && (irq_raw   ? dg_irq
                             : e_ifault  ? dg_pgf
                             : ex_ill    ? dg_ill
                             : e_ebreak  ? dg_brk
                             : ex_ld_mis ? dg_ldm
                             : ex_st_mis ? dg_stm
                             : ex_pgf    ? dg_pgf
                                         : dg_ecall);

    // REGISTERED: `eff` is the address adder's output, and driven straight into
    // `mtval` it put the forward mux on a CSR register's data pins. `boundary`
    // holds the trap until the access retires, and `op_held` freezes the
    // operands meanwhile, so the registered copy is the faulting address.
    reg [63:0] eff_q;
    always @(posedge clk) begin
        eff_q <= eff;
    end
    wire        val_is_ea = ex_ld_mis || ex_st_mis || ex_pgf;
    wire [63:0] trap_val = (
        e_ifault    ? e_pc
        : val_is_ea ? eff_q
        : 64'd0
    );

    assign trap_redir   = trap_take || mret_take || sret_take;
    assign trap_pc_next = mret_take ? mepc_v
                        : sret_take ? sepc_v
                                    : mtvec_v;

    rv64_csr #(.ADDR_W(PADDR_W)) u_csr (
        .clk(clk), .resetn(resetn),
        .req(csr_req), .addr(e_csr_addr), .op(e_f3[1:0]),
        .wdata(csr_wd_q),
        // NOT `!stall && !trap_take`: both carry `misalign`, which carries the
        // forward mux, and the CSR write enable then reached 12 levels. A CSR
        // instruction is never a load, store or AMO, so a misaligned-access trap
        // cannot coincide with it and it cannot stall on memory -- the only
        // causes that CAN kill it are an illegal CSR and a pending interrupt.
        .wr_en(csr_hold && e_csr_wr && !csr_kill),
        .wr_intent(e_csr_wr),
        .rdata(csr_rdata), .illegal(csr_ill),
        .trap(trap_take), .trap_pc(e_pc), .trap_cause(trap_cause),
        .trap_val(trap_val), .mret(mret_take), .sret(sret_take),
        .pgf_cause(e_ifault ? 4'd12 : dmem_fault_cause),
        .deleg(deleg), .can_deleg(can_deleg),
        .dg_ill(dg_ill), .dg_brk(dg_brk), .dg_ldm(dg_ldm), .dg_stm(dg_stm),
        .dg_pgf(dg_pgf), .dg_ecall(dg_ecall), .dg_irq(dg_irq),
        .tvec_o(mtvec_v), .tvec_set(tvec_installed),
        .mepc_o(mepc_v), .sepc_o(sepc_v),
        .priv_o(priv_v), .settle(priv_settle_o),
        .satp_o(satp_o), .sum_o(sum_o), .mxr_o(mxr_o),
        .irq_pending(irq_pend), .irq_cause(irq_cause_v),
        .irq_ext(irq_ext), .irq_soft(irq_soft),
        .retire(dbg_retire)
    );
    assign priv_o = priv_v;

    assign dmem_addr = {eff[63:3], 3'd0};
    assign dmem_re   = (e_valid && e_ld && !misalign)
                    || (amo_active && (a_state == A_IDLE));

    // A byte-enable per lane, placed by the low address bits.
    reg [7:0] strb;
    always @(*) begin
        case (e_f3[1:0])
            2'b00:   strb = 8'h01 << eff[2:0];
            2'b01:   strb = 8'h03 << {eff[2:1], 1'b0};
            2'b10:   strb = 8'h0f << {eff[2], 2'b00};
            default: strb = 8'hff;
        endcase
    end

    // The AMO's write phase reuses the store path, with the modified value.
    wire amo_wr = amo_active && (a_state == A_WR) && !misalign;
    // NOT gated on `trap_take`: that would close a loop through the wrapper --
    // wstrb -> node request -> dmem_stall -> stall -> boundary -> trap_take.
    // A misaligned store already emits no strobe, and an interrupt is deferred
    // past a memory instruction instead (see `interrupt`), which is always
    // allowed.
    assign dmem_wstrb = (e_valid && e_st && !misalign && !halted && !e_ill)
                      ? strb
                      : amo_wr                                    ? strb
                                                                  : 8'd0;
    // REPLICATE, DO NOT SHIFT. `strb` already selects the lane, so putting the
    // datum in every lane is pure wiring where `<< {eff[2:0],3'b0}` is a 64-bit
    // barrel shifter -- and it sat on `wb_val_reg -> spad/DIN_B`, the path
    // holding both assembled units near 290 MHz.
    wire [63:0] st_src = amo_wr ? a_new : op_rs2;
    reg  [63:0] st_data;
    always @(*) begin
        case (e_f3[1:0])
            2'b00:   st_data = {8{st_src[7:0]}};
            2'b01:   st_data = {4{st_src[15:0]}};
            2'b10:   st_data = {2{st_src[31:0]}};
            default: st_data = st_src;
        endcase
    end
    assign dmem_wdata = st_data;

    // ---- M ------------------------------------------------------------------
    reg [2:0] m_f3;
    reg [2:0] m_off;
    always @(posedge clk) begin
        if (!resetn) begin
            m_wr <= 1'b0;
            m_ld <= 1'b0;
            m_rd <= 5'd0;
        end
        else if (go) begin
            // A trapping instruction retires nothing: an interrupt re-executes
            // it from `mepc`, so a writeback here would apply it twice.
            m_wr  <= e_valid && e_wr && !trap_take;
            m_ld  <= e_valid && e_ld && !trap_take;
            m_rd  <= e_rd;
            m_f3  <= e_f3;
            m_off <= ea[2:0];
            m_val <= e_result;
        end
        else if (stall) begin
            // Nothing retires while E holds, or the same instruction would be
            // written back once per stalled cycle.
            m_wr <= 1'b0;
            m_ld <= 1'b0;
        end
    end

    wire [63:0] shifted = dmem_rdata >> {m_off, 3'b000};
    reg  [63:0] load_ext;
    always @(*) begin
        case (m_f3)
            3'b000:  load_ext = {{56{shifted[7]}},  shifted[7:0]};
            3'b001:  load_ext = {{48{shifted[15]}}, shifted[15:0]};
            3'b010:  load_ext = {{32{shifted[31]}}, shifted[31:0]};
            3'b011:  load_ext = shifted;
            3'b100:  load_ext = {56'd0, shifted[7:0]};
            3'b101:  load_ext = {48'd0, shifted[15:0]};
            3'b110:  load_ext = {32'd0, shifted[31:0]};
            default: load_ext = shifted;
        endcase
    end

    // ---- W ------------------------------------------------------------------
    // REGISTERED, so the forward out of W is a register and not the tail of the
    // load-align chain. This is the cycle `load_use` pays for.
    // M AND W ALWAYS DRAIN. A stall belongs to whatever owns E; the instruction
    // already in M is not the one being held, and gating this on `go` threw its
    // writeback away -- which is what made a loop counter read 9993 instead of
    // 3000. The bubble is inserted at E->M, not here.
    reg [63:0] wb_val;
    reg [4:0]  wb_rd;
    reg        wb_we;
    always @(posedge clk) begin
        if (!resetn) begin
            wb_we <= 1'b0;
        end else if (!halted) begin
            wb_we  <= m_wr;
            wb_rd  <= m_rd;
            wb_val <= m_ld ? load_ext : m_val;
        end
    end

    assign w_we   = wb_we;
    assign w_rd   = wb_rd;
    assign w_data = wb_val;

    // A load's value does not exist until M has run, and it is not forwardable
    // from M any more, so a consumer one behind has to wait exactly one cycle.
    // `imem_stall` IS THE SAME SHAPE AS A LOAD-USE BUBBLE, which is why it
    // joins here rather than in `stall`: F and D hold while E drains, so no
    // instruction enters E until fetch can name one, and nothing already in
    // flight is delayed by a translation it does not need.
    assign load_use = (
        e_valid
        && e_ld
        && (e_rd != 5'd0)
        && d_valid
        && ((rs1_a == e_rd) || (rs2_a == e_rd))
    );
    // a JALR owns the next E slot for its check: the gap costs one cycle per
    // JALR and buys a redirect with nothing in flight to kill
    assign bubble = imem_stall || load_use || (e_valid && e_jalr);

    // Drains with W, for the same reason W drains with M.
    always @(posedge clk) begin
        if (!resetn) begin
            w_wr_q <= 1'b0;
        end else if (!halted) begin
            w_wr_q  <= w_we;
            w_rd_q  <= w_rd;
            w_val_q <= w_data;
        end
    end

    // ---- halting ------------------------------------------------------------
    // Only when no handler is installed -- see `fault_halt` above.
    always @(posedge clk) begin
        if (!resetn) begin
            halted     <= 1'b0;
            halt_cause <= 2'd0;
            halt_pc    <= 64'd0;
        end
        else if (fault_halt) begin
            halted     <= 1'b1;
            halt_pc    <= e_pc;
            halt_cause <= e_ecall ? 2'd1 : e_ebreak ? 2'd2 : 2'd3;
        end
        else if (ext_halt && !halted) begin
            halted     <= 1'b1;
            halt_pc    <= e_pc;
            halt_cause <= 2'd0;      // a clean exit, not a fault
        end
    end

    assign dbg_pc = e_pc;

    // ONE PULSE PER INSTRUCTION, not per cycle: `e_valid` stays high for all 66
    // cycles of a divide, so without `!stall` this counts occupancy and
    // `minstret` measures nothing. REGISTERED: `stall` and `trap_take` both
    // carry the address adder, and a count one cycle late is still a count.
    reg retire_q;
    always @(posedge clk) begin
        retire_q <= resetn && e_valid && !halted && !stall && !trap_take;
    end
    assign dbg_retire = retire_q;

endmodule

`default_nettype wire

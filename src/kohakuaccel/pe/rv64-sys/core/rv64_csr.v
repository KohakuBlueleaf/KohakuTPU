// SysCore's CSR file, trap state and privilege: machine, supervisor and user.
//
// ONLY THE CSRs THE DESIGN NAMES. Architecturally-visible state is measured as
// the expensive part of a core -- adding PMP alone took an Ibex CSR block +84.2%
// -- so this implements the set a runtime uses and refuses the rest, rather
// than the set the specification permits.
//
// `mtime` IS FREE-RUNNING AND SURVIVES A HALT. The compute PE's cycle counter
// resets on every kick and stops while halted, which means a runtime there
// cannot both sleep and keep time. SysCore hosts a runtime, so it must.
//
// SUPERVISOR EXISTS SO THE KERNEL CAN REACH USER MEMORY. An M+U machine can run
// user code under Sv39, but its kernel is untranslated and must walk the tables
// in software to touch a user buffer. With S mode and `mstatus.SUM` the kernel
// reads a user page directly, which is what `copy_to_user` needs.

`default_nettype none

module rv64_csr #(
    parameter integer ADDR_W = 40   // physical address width; sizes satp.PPN
)(
    input  wire        clk,
    input  wire        resetn,

    // the CSR instruction port
    input  wire        req,
    input  wire [11:0] addr,
    input  wire [1:0]  op,          // 1 RW, 2 RS, 3 RC
    input  wire [63:0] wdata,
    input  wire        wr_en,       // the write actually happens
    // Decode's "this instruction would write", which `wr_en` cannot be used for:
    // `wr_en` is gated on the illegal flag, so testing it here closes a loop.
    input  wire        wr_intent,
    output reg  [63:0] rdata,
    output reg         illegal,

    // trap entry and return
    input  wire        trap,
    input  wire [63:0] trap_pc,
    input  wire [63:0] trap_cause,
    input  wire [63:0] trap_val,
    input  wire        mret,
    input  wire        sret,
    // The core owns the trap priority chain, so it resolves delegation from
    // these pre-indexed bits and hands the answer back.
    input  wire [3:0]  pgf_cause,
    input  wire        deleg,
    output wire        can_deleg,
    output wire        dg_ill, dg_brk, dg_ldm, dg_stm, dg_pgf, dg_ecall, dg_irq,
    output wire [63:0] tvec_o,      // already resolved: stvec if delegated
    output wire        tvec_set,    // ...and whether that one is non-zero
    output wire [63:0] mepc_o,
    output wire [63:0] sepc_o,

    // privilege and translation
    output wire [1:0]  priv_o,      // 3 = machine, 1 = supervisor, 0 = user
    output wire        settle,      // `priv_o` changes at the end of this cycle
    output wire [63:0] satp_o,
    output wire        sum_o,
    output wire        mxr_o,

    // interrupts
    output wire        irq_pending,
    output wire [63:0] irq_cause,
    input  wire        irq_ext,
    input  wire        irq_soft,

    // One pulse per retired instruction. `minstret` against `mcycle` is the
    // only direct measure of stall cost this core has.
    input  wire        retire
);
    localparam [11:0] C_SSTATUS  = 12'h100;
    localparam [11:0] C_SIE      = 12'h104;
    localparam [11:0] C_STVEC    = 12'h105;
    localparam [11:0] C_SSCRATCH = 12'h140;
    localparam [11:0] C_SEPC     = 12'h141;
    localparam [11:0] C_SCAUSE   = 12'h142;
    localparam [11:0] C_STVAL    = 12'h143;
    localparam [11:0] C_SIP      = 12'h144;
    localparam [11:0] C_SATP     = 12'h180;
    localparam [11:0] C_MSTATUS  = 12'h300;
    localparam [11:0] C_MISA     = 12'h301;
    localparam [11:0] C_MEDELEG  = 12'h302;
    localparam [11:0] C_MIDELEG  = 12'h303;
    localparam [11:0] C_MIE      = 12'h304;
    localparam [11:0] C_MTVEC    = 12'h305;
    localparam [11:0] C_MSCRATCH = 12'h340;
    localparam [11:0] C_MEPC     = 12'h341;
    localparam [11:0] C_MCAUSE   = 12'h342;
    localparam [11:0] C_MTVAL    = 12'h343;
    localparam [11:0] C_MIP      = 12'h344;
    localparam [11:0] C_MCYCLE   = 12'hB00;
    localparam [11:0] C_MINSTRET = 12'hB02;
    localparam [11:0] C_CYCLE    = 12'hC00;
    localparam [11:0] C_TIME     = 12'hC01;
    localparam [11:0] C_INSTRET  = 12'hC02;
    localparam [11:0] C_MVENDORID = 12'hF11;
    localparam [11:0] C_MARCHID  = 12'hF12;
    localparam [11:0] C_MIMPID   = 12'hF13;
    localparam [11:0] C_MHARTID  = 12'hF14;
    localparam [11:0] C_MTIMECMP = 12'h7C0;   // a SysCore addition, see below

    localparam [1:0] P_U = 2'b00, P_S = 2'b01, P_M = 2'b11;

    localparam [63:0] MTI = 64'h8000_0000_0000_0007;
    localparam [63:0] MSI = 64'h8000_0000_0000_0003;
    localparam [63:0] MEI = 64'h8000_0000_0000_000B;
    localparam [63:0] STI = 64'h8000_0000_0000_0005;
    localparam [63:0] SSI = 64'h8000_0000_0000_0001;
    localparam [63:0] SEI = 64'h8000_0000_0000_0009;

    reg [63:0] mstatus, mie, mtvec, mscratch, mepc, mcause, mtval;
    reg [63:0] medeleg, mideleg;
    reg [63:0] stvec, sscratch, sepc, scause, stval, satp;
    reg [63:0] mcycle, minstret, mtime, mtimecmp;
    reg        mip_msi;
    reg [1:0]  priv;

    assign mepc_o = mepc;
    assign sepc_o = sepc;
    assign priv_o = priv;
    assign settle = trap_q || mret_q || sret_q;
    assign satp_o = satp;

    // mstatus: SIE 1, MIE 3, SPIE 5, MPIE 7, SPP 8, MPP 12:11, SUM 18, MXR 19.
    assign sum_o = mstatus[18];
    assign mxr_o = mstatus[19];

    // REGISTERED, AND IT IS THE WHOLE REDIRECT PATH. `mtime >= mtimecmp` is a
    // 64-bit magnitude compare -- eight CARRY8 blocks in a chain -- and it fed
    // `irq_pending` -> `interrupt` -> `trap_take` -> `pc_next`, so the carry
    // chain was in front of the program counter. A timer interrupt one cycle
    // late is indistinguishable from one on time: the thing it compares
    // against advances every cycle anyway.
    reg timer_irq;
    always @(posedge clk) begin
        if (!resetn) begin
            timer_irq <= 1'b0;
        end else begin
            timer_irq <= (mtime >= mtimecmp);
        end
    end
    wire soft_irq  = mip_msi || irq_soft;
    wire [63:0] mip = {52'd0, irq_ext, 3'd0, timer_irq, 3'd0, soft_irq, 3'd0};

    // An interrupt at privilege x is taken when the hart runs below x, or at x
    // with x's global enable set. Running above x never takes it.
    wire m_on = (priv != P_M) || mstatus[3];
    wire s_on = (priv == P_U) || ((priv == P_S) && mstatus[1]);

    wire d_ext  = mideleg[11] && (priv != P_M);
    wire d_tmr  = mideleg[7]  && (priv != P_M);
    wire d_soft = mideleg[3]  && (priv != P_M);

    wire pend_ext  = irq_ext  && mie[11] && (d_ext  ? s_on : m_on);
    wire pend_tmr  = timer_irq && mie[7] && (d_tmr  ? s_on : m_on);
    wire pend_soft = soft_irq && mie[3]  && (d_soft ? s_on : m_on);

    // Priority is external, then software, then timer.
    assign irq_pending = pend_ext || pend_soft || pend_tmr;
    assign irq_cause   = pend_ext  ? (d_ext  ? SEI : MEI)
                       : pend_soft ? (d_soft ? SSI : MSI)
                                   : (d_tmr  ? STI : MTI);

    // ---- where this trap goes ----------------------------------------------
    // INDEXED BY A CONSTANT, NOT BY THE CAUSE. `medeleg[trap_cause[5:0]]` is a
    // 64:1 mux on a value derived from the address adder, and it selects which
    // CSR set the trap writes -- measured 28 logic levels and WNS -3.496 at the
    // node. The core owns the priority chain, so it selects among these bits
    // instead; every one of them is a register read.
    assign dg_ill   = medeleg[2];
    assign dg_brk   = medeleg[3];
    assign dg_ldm   = medeleg[4];
    assign dg_stm   = medeleg[6];
    assign dg_pgf   = medeleg[{2'd0, pgf_cause}];
    assign dg_ecall = (priv == P_M) ? medeleg[11]
                    : (priv == P_S) ? medeleg[9]
                                    : medeleg[8];
    assign dg_irq   = pend_ext ? mideleg[11]
                    : pend_soft ? mideleg[3]
                                : mideleg[7];
    assign can_deleg = (priv != P_M);

    assign tvec_o = deleg ? stvec : mtvec;

    // "IS A HANDLER INSTALLED" IS A PROPERTY OF THE WRITE, NOT OF THE TRAP.
    // Testing `tvec_o != 0` in the core put a 64-bit compare -- three CARRY8 --
    // downstream of `deleg`, inside the trap decision.
    reg mtvec_nz, stvec_nz;
    always @(posedge clk) begin
        if (!resetn) begin
            mtvec_nz <= 1'b0;
            stvec_nz <= 1'b0;
        end
        else if (wr_en) begin
            if (addr == C_MTVEC) begin
                mtvec_nz <= |(next & TVEC_MASK);
            end
            if (addr == C_STVEC) begin
                stvec_nz <= |(next & TVEC_MASK);
            end
        end
    end
    assign tvec_set = deleg ? stvec_nz : mtvec_nz;

    // sstatus is a window on mstatus, not a register: SIE, SPIE, SPP, SUM, MXR.
    localparam [63:0] SSTATUS_MASK = 64'h0000_0000_000C_0122;

    // WARL: A BIT THAT IS NOT IMPLEMENTED IS NOT STORED. Writes land through
    // these masks, so the dead bits leave the flops and the 64-bit read mux.
    //   mstatus SIE MIE SPIE MPIE SPP MPP SUM MXR | mie/mideleg the six S/M
    //   bits | medeleg codes 0..15 | satp MODE + PPN, no ASID | xcause the
    //   interrupt bit + 5 | xtvec direct only | xepc IALIGN 32
    localparam [63:0] MSTATUS_MASK = 64'h0000_0000_000C_19AA;
    localparam [63:0] MIE_MASK     = 64'h0000_0000_0000_0AAA;
    localparam [63:0] MEDELEG_MASK = 64'h0000_0000_0000_FFFF;
    localparam integer PPN_W       = ADDR_W - 12;
    localparam [43:0] PPN_MASK     = {{(44-PPN_W){1'b0}}, {PPN_W{1'b1}}};
    localparam [63:0] SATP_MASK    = {4'hF, 16'd0, PPN_MASK};
    localparam [63:0] CAUSE_MASK   = 64'h8000_0000_0000_001F;
    localparam [63:0] TVEC_MASK    = ~64'h3;
    localparam [63:0] EPC_MASK     = ~64'h1;
    // What a supervisor write to `sie` may touch: delegated AND implemented.
    wire [63:0] sie_wmask = mideleg & MIE_MASK;
    wire [63:0] sie_keep  = mie & ~mideleg;

    // MXL 2 (RV64) and the extension bits A, I, M, S, U -- 0, 8, 12, 18, 20.
    // The earlier literal set bit 14 in place of S and U.
    localparam [63:0] MISA_VAL = {2'b10, 36'd0, 26'h014_1101};

    // ---- read ---------------------------------------------------------------
    // addr[9:8] is the privilege a CSR requires, and addr[11:10] == 2'b11 marks
    // it read-only. Both are architectural, so the check is on the encoding
    // rather than on a per-register table.
    wire priv_ok = (priv >= addr[9:8]);
    wire ro_hit  = (addr[11:10] == 2'b11);

    always @(*) begin
        illegal = 1'b0;
        case (addr)
            C_SSTATUS:  rdata = mstatus & SSTATUS_MASK;
            C_SIE:      rdata = mie & mideleg;
            C_STVEC:    rdata = stvec;
            C_SSCRATCH: rdata = sscratch;
            C_SEPC:     rdata = sepc;
            C_SCAUSE:   rdata = scause;
            C_STVAL:    rdata = stval;
            C_SIP:      rdata = mip & mideleg;
            C_SATP:     rdata = satp;
            C_MSTATUS:  rdata = mstatus;
            C_MISA:     rdata = MISA_VAL;
            C_MEDELEG:  rdata = medeleg;
            C_MIDELEG:  rdata = mideleg;
            C_MIE:      rdata = mie;
            C_MTVEC:    rdata = mtvec;
            C_MSCRATCH: rdata = mscratch;
            C_MEPC:     rdata = mepc;
            C_MCAUSE:   rdata = mcause;
            C_MTVAL:    rdata = mtval;
            C_MIP:      rdata = mip;
            C_MCYCLE,
            C_CYCLE:    rdata = mcycle;
            C_MINSTRET,
            C_INSTRET:  rdata = minstret;
            C_TIME:     rdata = mtime;
            C_MTIMECMP: rdata = mtimecmp;
            C_MVENDORID,
            C_MARCHID,
            C_MIMPID,
            C_MHARTID:  rdata = 64'd0;
            default: begin
                rdata   = 64'd0;
                illegal = req;
            end
        endcase
        // A CSR that exists is still illegal from too low a privilege, and a
        // write to a read-only one is illegal at any privilege.
        if (req && !illegal && (!priv_ok || (ro_hit && wr_intent))) begin
            illegal = 1'b1;
        end
    end

    wire [63:0] next = (op == 2'd1) ? wdata
                     : (op == 2'd2) ? (rdata | wdata)
                                    : (rdata & ~wdata);

    // ---- write and trap -----------------------------------------------------
    reg        trap_q, deleg_q, mret_q, sret_q;
    reg [1:0]  prev_q;
    reg [63:0] pc_q, cause_q, val_q;
    always @(posedge clk) begin
        if (!resetn) begin
            trap_q <= 1'b0;
            mret_q <= 1'b0;
            sret_q <= 1'b0;
        end
        else begin
            trap_q  <= trap;
            mret_q  <= mret;
            sret_q  <= sret;
            deleg_q <= deleg;
            prev_q  <= priv;
            pc_q    <= trap_pc;
            cause_q <= trap_cause;
            val_q   <= trap_val;
        end
    end

    always @(posedge clk) begin
        // RESET THE CONTROL, NOT THE DATA. Only the registers whose value before
        // the first write changes behaviour are reset: the enables and
        // delegation because they decide where a trap goes, `satp` because it
        // turns translation on, and `mtimecmp` to all-ones or the timer fires at
        // boot. The trap vectors themselves are data -- `mtvec_nz`/`stvec_nz`
        // hold the reset, and no trap is taken until one is written. `mepc`,
        // `mcause`, `mtval` and their supervisor twins are written by the trap
        // that makes them meaningful. Together that is 640 bits of register kept
        // out of a control set.
        if (!resetn) begin
            mstatus  <= 64'd0;
            mie      <= 64'd0;
            medeleg  <= 64'd0;
            mideleg  <= 64'd0;
            satp     <= 64'd0;
            mcycle   <= 64'd0;
            minstret <= 64'd0;
            mtime    <= 64'd0;
            mtimecmp <= {64{1'b1}};
            mip_msi  <= 1'b0;
            priv     <= P_M;              // reset lands in machine mode
        end
        else begin
            mcycle <= mcycle + 64'd1;
            mtime  <= mtime + 64'd1;
            if (retire && !(wr_en && (addr == C_MINSTRET))) begin
                minstret <= minstret + 64'd1;
            end

            // EVERYTHING A TRAP WRITES LANDS ONE CYCLE AFTER THE REDIRECT, from
            // registered copies, so `trap` -- the address adder, via `misalign`
            // -- is the enable of nothing wider than a flag. Only fetch reads
            // any of it sooner: `settle` tells the wrapper to hold fetch for
            // that cycle, since `priv` decides whether the new PC is translated.
            if (trap_q) begin
                priv <= deleg_q ? P_S : P_M;
                if (deleg_q) begin
                    sepc       <= pc_q & EPC_MASK;
                    scause     <= cause_q & CAUSE_MASK;
                    stval      <= val_q;
                    mstatus[5] <= mstatus[1];        // SPIE <= SIE
                    mstatus[1] <= 1'b0;
                    mstatus[8] <= (prev_q == P_S);   // SPP: where we came from
                end
                else begin
                    mepc           <= pc_q & EPC_MASK;
                    mcause         <= cause_q & CAUSE_MASK;
                    mtval          <= val_q;
                    mstatus[7]     <= mstatus[3];    // MPIE <= MIE
                    mstatus[3]     <= 1'b0;
                    mstatus[12:11] <= prev_q;        // MPP: where we came from
                end
            end
            else if (mret_q) begin
                priv           <= mstatus[12:11];
                mstatus[3]     <= mstatus[7];
                mstatus[7]     <= 1'b1;
                mstatus[12:11] <= P_U;
            end
            else if (sret_q) begin
                priv       <= {1'b0, mstatus[8]};
                mstatus[1] <= mstatus[5];
                mstatus[5] <= 1'b1;
                mstatus[8] <= 1'b0;
            end

            // A SEPARATE `if`, NOT AN `else`. Chained, `trap` lands in the write
            // enable of all twenty registers when it actually writes five, and
            // `trap` carries `misalign` and therefore the address adder: 18
            // logic levels into the whole file. The two are already mutually
            // exclusive -- a CSR instruction is never a load, a store, an ECALL
            // or an EBREAK, so no cause above can coincide with one.
            if (wr_en) begin
                case (addr)
                    // A write through the sstatus window must not disturb the
                    // machine-only bits sharing the register.
                    C_SSTATUS:  mstatus  <= (mstatus & ~SSTATUS_MASK)
                                          | (next & SSTATUS_MASK);
                    C_SIE:      mie      <= sie_keep | (next & sie_wmask);
                    C_STVEC:    stvec    <= next & TVEC_MASK;
                    C_SSCRATCH: sscratch <= next;
                    C_SEPC:     sepc     <= next & EPC_MASK;
                    C_SCAUSE:   scause   <= next & CAUSE_MASK;
                    C_STVAL:    stval    <= next;
                    C_SATP:     satp     <= next & SATP_MASK;
                    C_MSTATUS:  mstatus  <= next & MSTATUS_MASK;
                    C_MEDELEG:  medeleg  <= next & MEDELEG_MASK;
                    C_MIDELEG:  mideleg  <= next & MIE_MASK;
                    C_MIE:      mie      <= next & MIE_MASK;
                    C_MTVEC:    mtvec    <= next & TVEC_MASK;
                    C_MSCRATCH: mscratch <= next;
                    C_MEPC:     mepc     <= next & EPC_MASK;
                    C_MCAUSE:   mcause   <= next & CAUSE_MASK;
                    C_MTVAL:    mtval    <= next;
                    C_MCYCLE:   mcycle   <= next;
                    C_MINSTRET: minstret <= next;
                    // WRITING mtimecmp CLEARS THE PENDING TIMER by definition:
                    // the interrupt is a comparison, not a latch, so there is
                    // no acknowledge and a handler that does not move the
                    // compare re-enters forever.
                    C_MTIMECMP: mtimecmp <= next;
                    C_MIP:      mip_msi  <= next[3];
                    default: ;
                endcase
            end
        end
    end

endmodule

`default_nettype wire

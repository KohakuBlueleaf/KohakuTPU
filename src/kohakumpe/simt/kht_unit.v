// kht_unit -- the SIMT PE's per-thread datapath: the wave-indexed register file,
// the active mask and its IPDOM stack, the lane array, and the writeback.
//
// It is the SIMT half of the machine. The scalar half is the base core's own
// register file and ALU, reached through custom-2/3; the per-thread half is
// here and is what an ordinary RV32I opcode addresses.
//
// THE LADDER IS PARAMETERS, NOT BRANCHES. Each gate turns one generic and is
// measured as itself, so every reported delta is a controlled difference:
//
//   G0   WAVES 1,  HAS_MASK 0, HAS_IPDOM 0    the arithmetic substrate
//   G1   WAVES 16, HAS_MASK 0, HAS_IPDOM 0    wave-indexed storage
//   G2   WAVES 16, HAS_MASK 1, HAS_IPDOM 0    active masks, tmc, predication
//   G3   WAVES 16, HAS_MASK 1, HAS_IPDOM 1    split/join, bounded stack, fault
//
// A gate that is not built elaborates none of its logic, which is what makes
// "the total is the sum of measured deltas" true rather than intended.
//
// THE MASK IS A WRITE ENABLE, NOT A DATAPATH INPUT. An inactive lane computes
// whatever it computes and its WRITE is dropped, so masking costs one enable
// per bank and nothing on the arithmetic path.
//
// A SPLIT PUSHES TWO ENTRIES AND A JOIN POPS ONE. The resume PC is always the
// popping join's own pc+4, so an entry is LANES bits and carries no PC -- exact
// for structured control flow, which SPIR-V guarantees by naming a merge block
// for every selection and loop. A DEPTH OF D THEREFORE PERMITS D/2 NESTED
// LEVELS, not D-1, and overflow is a fault rather than a wrap.

`default_nettype none

module kht_unit #(
    parameter integer LANES     = 8,
    parameter integer WAVES     = 16,
    // Derived, and in the parameter list because the port list needs it: a
    // localparam in the body is declared too late to size a port.
    parameter integer WID       = (WAVES > 1) ? $clog2(WAVES) : 1,
    parameter integer HAS_MASK  = 1,
    parameter integer HAS_IPDOM = 1,
    // G8. One butterfly serves shflxor AND bcast. 0 IS NOT BUILT, -1 is full.
    // Read `(SHFL_UNITS == 0) ? LANES`, so 0 and LANES were the SAME build and
    // the sweep measured +0 LUT across what it thought was the whole ladder.
    parameter integer SHFL_UNITS = -1,
    // G9. THREE INDEPENDENT COUNTS, EACH LEGAL AT 0, 1, 2, 4, 8. There is no
    // separate boolean: 0 units IS "not built", and the decode faults on the
    // group. A HAS_FLT gate beside a count would be two ways to say one thing,
    // and it made `mul` unbuildable without the float tier.
    //
    // FLANES < LANES is a WORKING configuration, not an area probe: the walk
    // below issues LANES/FLANES passes and the per-lane write enable places
    // them. Real GPUs provision transcendentals at 1/4 rate, which is what
    // FSFU_UNITS < FLANES is for.
    parameter integer FLANES    = 0,
    // Seed units, a subset of the FMA units. A nonzero count deepens the whole
    // tier: khs_fp32_sfu is 10 cycles and rv_fpu is 6.
    parameter integer FSFU_UNITS = 0,
    parameter integer IPDOM_D   = 8,
    parameter         VREG_PRIM = "block"
)(
    input  wire                clk,
    input  wire                resetn,

    // ---- the EX stage, combinational ----
    input  wire                x_valid,
    input  wire [WID-1:0]      x_wave,
    input  wire                x_hold,
    // The instruction is READ but must not COMMIT yet -- an LSU walk owes it
    // data. Distinct from `x_hold`, which freezes the MEM register: freezing it
    // here would keep a pending hazard asserted forever, and the hazard is what
    // the walk is waiting on to read a valid address.
    input  wire                x_defer,
    output wire                stall,

    // ---- G9: the float tier, and RV32M ----
    // Both are multi-cycle producers of the SAME per-wave pending bit and the
    // same retire slot; the multiplier is not a second mechanism.
    input  wire                x_flt,
    input  wire [3:0]          x_fop,
    input  wire                x_imul,
    input  wire [1:0]          x_mop,
    // A WAVE WITH A FLOAT IN FLIGHT MUST NOT ISSUE. This is not a scoreboard:
    // the design deletes forwarding and interlocks on the argument that with
    // W >= pipeline depth no two in-flight instructions share a wave, and a
    // multi-cycle float unit breaks that precondition. One bit per wave restores
    // it. The scheduler reads this and skips those waves.
    output wire [WAVES-1:0]    fpend,
    // The float result lands in TWO cycles and needs the write port. The core
    // holds for one cycle so the bubble reaches writeback in time.
    output wire                f_soon,
    // A MULTI-PASS INSTRUCTION HAS NOT FINISHED ISSUING. The core must freeze
    // its whole front end on this, and this unit freezes MEM and WB on it, so
    // the operands stay put while the remaining passes launch. It must NOT reach
    // `x_hold`: that is what the launch reads, and feeding it back closes a
    // combinational loop -- khs_unit's `f_pass_hold` carries the same warning.
    output wire                fpass_hold,

    // ---- the decoded per-thread operation ----
    input  wire [3:0]          x_alu,
    input  wire [4:0]          x_rd,
    // BOTH candidates for read port 1, and the select, kept SEPARATE. A vmem
    // store's data register rides in `rd`, so the caller used to hand over
    // `mem_store ? rd : rs1` -- which put a decode term and a mux IN SERIES in
    // front of the hazard comparator, four levels from the instruction word.
    // Compared in parallel and selected after, the comparators start at the
    // instruction bits directly.
    input  wire [4:0]          x_rs1,
    input  wire [4:0]          x_rs1b,
    input  wire                x_rs1_sel,
    input  wire [4:0]          x_rs2,
    input  wire                x_wen,
    input  wire                x_op2_imm,
    input  wire [31:0]         x_imm,
    // `vlaneid`: the result is a constant per lane, so it needs no read port
    // and no storage -- only a mux in front of the write.
    input  wire                x_laneid,
    // `s2v`: the scalar value rides in on x_imm, uniform in every lane by
    // construction, so the broadcast is a mux input and not a network.
    input  wire                x_smov,
    // The subgroup permute. `x_sh_idx` is the xor mask for shflxor and the
    // source lane for bcast; one network covers both.
    input  wire                x_shfl,
    input  wire                x_sh_bcast,
    input  wire [((LANES > 1) ? $clog2(LANES) : 1)-1:0] x_sh_idx,
    // A load's per-lane words, gathered outside this unit while the LSU walked
    // the lanes. Presented on the cycle the hold drops, which is the cycle this
    // unit captures its control.
    input  wire                x_ld_sel,
    input  wire [32*LANES-1:0] x_ld_data,

    // ---- divergence, from the custom-2 decoder ----
    input  wire                x_split,
    input  wire                x_join,
    input  wire                x_tmc,
    input  wire [LANES-1:0]    x_tmc_mask,

    output wire [LANES-1:0]    mask_o,
    output wire [7:0]          ipdom_hi,
    output wire                fault_o,

    // The read ports, out. The cross-lane reductions and the LSU's per-lane
    // address walk both consume them, and a hierarchical reference into this
    // instance would simulate and not synthesise.
    output wire [32*LANES-1:0] rd1_o,
    output wire [32*LANES-1:0] rd2_o,

    // Visible to a bench and to nothing in the machine. LANE 0 ONLY on the data:
    // the halt word is lane 0's a0 and nothing reads the other lanes, so a
    // preserved boundary need not carry the other seven.
    output wire                dbg_wr_valid,
    output wire [4:0]          dbg_wr_rd,
    output wire [LANES-1:0]    dbg_wr_mask,
    output wire [31:0]         dbg_wr_data
);
    localparam integer AW    = $clog2(WAVES * 32);
    localparam integer VW    = 32 * LANES;
    localparam integer LNW   = (LANES > 1) ? $clog2(LANES) : 1;

    // THE PASS COUNTS, one per feature, and the widths the submodules derive
    // from the same expressions. A count that does not divide LANES is refused
    // at elaboration inside kht_fpu and kht_imul.
    //
    // A count of 0 means the feature is not built; the `*_W` forms below stand
    // in for it so that widths and divisions stay legal in the branch that is
    // then not elaborated. A zero-width wire is an elaboration error, not a
    // trimmed one.
    localparam integer HAS_FLT  = (FLANES != 0)     ? 1 : 0;
    // THE MULTIPLY COUNT IS THE THREAD COUNT. A thread's ALU is an IM unit --
    // RV32IM is the instruction set, not an option -- so there is no separate
    // multiply width to buy, and no operand mux between the two.
    localparam integer MUL_UNITS = LANES;
    localparam integer HAS_MUL  = 1;
    localparam integer FLANES_W = (FLANES != 0)     ? FLANES : 1;
    localparam integer MUL_W    = LANES;

    // -1 IS FULL RATE and is resolved HERE, once: passed down unresolved it is
    // a negative unit count, so no seed unit is built while the decode still
    // accepts the opcode.
    localparam integer SEED_N = (FSFU_UNITS < 0) ? FLANES_W : FSFU_UNITS;
    localparam integer SEED_U = (SEED_N != 0) ? SEED_N : FLANES_W;
    localparam integer FMINU  = (SEED_U < FLANES_W) ? SEED_U : FLANES_W;
    localparam integer PSW_F  = ((LANES / FMINU) > 1) ? $clog2(LANES / FMINU) : 1;
    localparam integer PSW_M = ((LANES / MUL_W) > 1) ? $clog2(LANES / MUL_W) : 1;
    localparam integer PSW    = (PSW_F > PSW_M) ? PSW_F : PSW_M;
    // The FMA walk's own width. Comparing a pass index against a constant with
    // the SEED walk's width builds a wider decode than the FMA walk can reach.
    localparam integer PSW_A = ((LANES / FLANES_W) > 1) ? $clog2(LANES / FLANES_W) : 1;

    // The shuffle's own width and walk. SU == LANES keeps the butterfly.
    localparam integer SHFL_ON = (SHFL_UNITS != 0) ? 1 : 0;
    localparam integer HAS_SHFL = SHFL_ON;
    localparam integer SU    = (SHFL_UNITS > 0) ? SHFL_UNITS : LANES;
    localparam integer SPASS = LANES / SU;
    localparam integer SPSW  = (SPASS > 1) ? $clog2(SPASS) : 1;

    // Every wire the blocks below share, declared before any of them: xvlog
    // rejects a use-before-declare that synthesis had accepted silently.
    wire [VW-1:0]    v1_rd, v2_rd, v3_rd, vwdata, wr_data;
    // SU lanes wide, not LANES: a pass produces its own units and nothing else.
    wire [32*SU-1:0] shfl_y;
    // The shuffle walk, declared here because the register file's write port and
    // `hold_all` both read them far above the block that drives them.
    wire             shfl_hold;
    wire [LANES-1:0] shfl_pass_en;
    wire [32*FLANES_W-1:0] fpu_y;
    wire [32*MUL_W-1:0]    imul_y;
    wire             fwb_mul, fwb_seed;
    wire             fwb_v;
    wire [AW-1:0]    fwb_wa;
    wire [LANES-1:0] fwb_mask;
    wire [LANES-1:0] mask;
    wire             ip_fault;
    wire             hz_raw;
    // TWO WALKS, ONE HOLD. The float/multiply walk and the shuffle walk freeze
    // exactly the same registers, so they share the output rather than the core
    // learning about a second one.
    wire             f_pass_hold_i;
    assign fpass_hold = f_pass_hold_i || shfl_hold;
    // `x_hold` MUST be the rest of the machine's stall and never this unit's
    // own, or the stall becomes an input to itself: the MEM register freezes,
    // its destination stays live, the hazard against it never clears, and the
    // core wedges on the first back-to-back dependency. khs_unit carries
    // the same warning for the same reason.
    wire             go = x_valid && !x_hold && !hz_raw;
    // EVERY REGISTER THIS UNIT OWNS, and the read port, freeze on the pass walk
    // too: the operands must not move while the remaining passes launch.
    wire             hold_all = x_hold || fpass_hold;
    // THE MASK AND THE STACK COMMIT ONCE, and `go` alone does not say that.
    // `go` excludes only this unit's own stall, so a held instruction is `go`
    // on every cycle of the hold -- harmless for an idempotent register write,
    // fatal for a stack: a `join` sitting under another wave's `f_soon` popped
    // once per cycle until the stack underflowed and faulted. `x_defer` already
    // carries every core-level hold that is not `x_hold`, and `fpass_hold` is
    // this unit's own, so it is named here rather than routed out and back.
    wire             go_c = go && !x_defer && !fpass_hold;

    // ================= the active mask =====================================
    generate
    if (HAS_MASK == 0) begin : g_nomask
        assign mask     = {LANES{1'b1}};
        assign ip_fault = 1'b0;
        assign ipdom_hi = 8'd0;
    end else begin : g_mask
        reg [LANES-1:0] m_q [0:WAVES-1];
        integer wi;

        wire [LANES-1:0] pred;
        genvar P;
        for (P = 0; P < LANES; P = P + 1) begin : g_pred
            assign pred[P] = |v1_rd[32*P +: 32];
        end

        wire [LANES-1:0] m_now = m_q[x_wave];
        wire [LANES-1:0] t_set = m_now & pred;
        wire [LANES-1:0] f_set = m_now & ~pred;

        assign mask = m_now;

        if (HAS_IPDOM == 0) begin : g_noip
            assign ip_fault = 1'b0;
            assign ipdom_hi = 8'd0;
            always @(posedge clk) begin
                if (!resetn) begin
                    for (wi = 0; wi < WAVES; wi = wi + 1) begin
                        m_q[wi] <= {LANES{1'b1}};
                    end
                end
                else if (go_c && x_tmc) begin
                    m_q[x_wave] <= x_tmc_mask;
                end
            end
        end else begin : g_ip
            // THE STACK IS A MEMORY, NOT AN INDEXED FLOP ARRAY. As flops,
            // WAVES x IPDOM_D entries cost a wide read mux and an equally wide
            // write decoder -- the shape that cost khs_facc 29,409 LUT and
            // rv_l1's valid/dirty 701, both fixed by moving the array into
            // storage. READ_LAT 0 on distributed RAM keeps the pop
            // combinational, so a join still costs no cycle.
            //
            // ONE WORD HOLDS THE PAIR a split pushes -- {outer, false} -- so
            // the two pushes are ONE write and the single write port is enough.
            // A phase bit per wave says which half the next join takes, and the
            // depth is entries/2 while "a split costs two entries" stays true.
            localparam integer PAIRS = IPDOM_D / 2;
            localparam integer PPW   = $clog2(PAIRS + 1);
            localparam integer PKW   = $clog2(WAVES * PAIRS);

            reg [PPW-1:0]    sp    [0:WAVES-1];
            reg [WAVES-1:0]  phase;
            reg [7:0]        hi_q;
            reg              flt_q;

            wire [PPW-1:0] sp_now = sp[x_wave];
            wire           ph_now = phase[x_wave];

            wire ovf = go_c && x_split && (sp_now == PAIRS[PPW-1:0]);
            wire unf = go_c && x_join  && (sp_now == {PPW{1'b0}}) && !ph_now;

            wire [PKW-1:0] base  = x_wave * PAIRS[PKW-1:0];
            wire [PKW-1:0] wr_a  = base + sp_now;
            // Both halves of a pop come from the top pair; the phase bit picks
            // the half, so the address is the same either way.
            wire [PKW-1:0] rd_a  = base + (sp_now - 1'b1);
            wire [2*LANES-1:0] pair_q;

            kohaku_sdpram #(.WIDTH(2*LANES), .DEPTH(WAVES*PAIRS),
                            .MEM_PRIM("distributed"), .READ_LAT(0)) u_stk (
                .clk(clk),
                .wr_en(go_c && x_split && !ovf), .wr_addr(wr_a),
                .wr_data({m_now, f_set}),
                .rd_en(1'b1), .rd_addr(rd_a), .rd_data(pair_q)
            );

            assign ip_fault = flt_q;
            assign ipdom_hi = hi_q;

            always @(posedge clk) begin
                if (!resetn) begin
                    for (wi = 0; wi < WAVES; wi = wi + 1) begin
                        m_q[wi] <= {LANES{1'b1}};
                        sp[wi]  <= {PPW{1'b0}};
                    end
                    phase <= {WAVES{1'b0}};
                    hi_q  <= 8'd0;
                    flt_q <= 1'b0;
                end else if (ovf || unf) begin
                    flt_q <= 1'b1;
                end else if (go_c && x_split) begin
                    sp[x_wave]    <= sp_now + 1'b1;
                    phase[x_wave] <= 1'b0;
                    m_q[x_wave]   <= t_set;
                    if (({{(8-PPW){1'b0}}, sp_now} + 8'd1) << 1 > hi_q) begin
                        hi_q <= ({{(8-PPW){1'b0}}, sp_now} + 8'd1) << 1;
                    end
                end else if (go_c && x_join) begin
                    if (!ph_now) begin
                        m_q[x_wave]   <= pair_q[LANES-1:0];
                        phase[x_wave] <= 1'b1;
                    end else begin
                        m_q[x_wave]   <= pair_q[2*LANES-1:LANES];
                        phase[x_wave] <= 1'b0;
                        sp[x_wave]    <= sp_now - 1'b1;
                    end
                end else if (go_c && x_tmc) begin
                    m_q[x_wave] <= x_tmc_mask;
                end
            end
        end
    end
    endgenerate

    assign mask_o  = mask;
    assign fault_o = ip_fault;
    assign rd1_o   = v1_rd;
    assign rd2_o   = v2_rd;

    // ================= the register file ===================================
    wire [4:0]    rs1_e = x_rs1_sel ? x_rs1b : x_rs1;
    wire [AW-1:0] ra1 = (WAVES > 1) ? {x_wave, rs1_e} : {{(AW-5){1'b0}}, rs1_e};
    wire [AW-1:0] ra2 = (WAVES > 1) ? {x_wave, x_rs2} : {{(AW-5){1'b0}}, x_rs2};
    // `vfma` is vd = vs1*vs2 + vd: three reads against two ports, and the third
    // address is the DESTINATION, which needs no new instruction field.
    wire [AW-1:0] ra3 = (WAVES > 1) ? {x_wave, x_rd}  : {{(AW-5){1'b0}}, x_rd};

    reg  [AW-1:0]    m_wa;
    reg  [3:0]       m_alu;
    reg  [4:0]       m_rd;
    // WHICH WAVE the MEM instruction belongs to. Without it the hazard compares
    // register numbers across waves and stalls on a dependency that does not
    // exist -- correct, but it throws away the whole point of interleaving.
    reg  [WID-1:0]   m_wave;
    reg              m_flt, m_imul;
    reg  [3:0]       m_fop;
    reg  [1:0]       m_mop;
    reg              m_wen, m_valid, m_op2_imm, m_laneid, m_ld_sel, m_smov;
    reg              m_shfl, m_sh_bcast;
    reg  [LNW-1:0]   m_sh_idx;
    reg  [31:0]      m_imm;
    reg  [VW-1:0]    m_ld_data;
    reg  [LANES-1:0] m_mask;

    // ================= the writeback stage =================================
    // THE LANE ALU MUST START AT A FLIP-FLOP, NOT AT THE REGISTER FILE. With the
    // array read straight into `kht_valu` and back out to its own write port,
    // the binding cone was 494 endpoints starting at a RAMB18E2 whose
    // clock-to-out is 0.845 ns -- a third of a 2.5 ns period spent before any
    // logic. This is the same register-in-front move the scalar half and the
    // instruction window each needed, and the third time it was the answer.
    //
    // The price is a SECOND hazard distance: a write now lands two cycles after
    // its read was issued, so EX stalls against MEM and against WB.
    reg  [VW-1:0]    w1_q, w2_q, w3_q;
    reg              w_flt, w_imul;
    reg  [3:0]       w_fop;
    reg  [1:0]       w_mop;
    reg  [AW-1:0]    w_wa;
    reg  [3:0]       w_alu;
    reg  [4:0]       w_rd;
    reg  [WID-1:0]   w_wave;
    reg              w_wen, w_valid, w_op2_imm, w_laneid, w_ld_sel, w_smov;
    reg              w_shfl, w_sh_bcast;
    reg  [LNW-1:0]   w_sh_idx;
    reg  [31:0]      w_imm;
    reg  [VW-1:0]    w_ld_data;
    reg  [LANES-1:0] w_mask;

    // The third read port is `vfma`'s addend, so it follows the FLOAT count and
    // not the multiplier's -- `mul` has two sources like every integer op.
    kht_vregfile #(.LANES(LANES), .WAVES(WAVES), .PRIM(VREG_PRIM),
                   .RD3(HAS_FLT)) u_vrf (
        .clk(clk),
        .ra_en(!hold_all), .ra1(ra1), .ra2(ra2), .ra3(ra3),
        .rd1(v1_rd), .rd2(v2_rd), .rd3(v3_rd),
        // THE FLOAT RESULT WINS THE PORT, and it can because the core was told
        // two cycles ago to put a bubble here. `w_valid` is zero on that cycle
        // by construction, so this is a mux and never an arbitration.
        .we(fwb_v ? 1'b1 : (w_valid && w_wen)),
        .wa(fwb_v ? fwb_wa : w_wa),
        // A SHUFFLE PASS WRITES ONLY THE LANES IT PRODUCED. Same per-lane write
        // enable the float walk uses; constant `{LANES{1'b1}}` at full width.
        .wlane(fwb_v ? fwb_mask : (w_mask & shfl_pass_en)),
        .wd(wr_data)
    );

    always @(posedge clk) begin
        if (!resetn) begin
            w_valid <= 1'b0;
        end else if (!hold_all) begin
            w_valid    <= m_valid;
            w1_q       <= v1_rd;
            w2_q       <= v2_rd;
            w3_q       <= v3_rd;
            w_flt      <= m_flt;
            w_fop      <= m_fop;
            w_imul     <= m_imul;
            w_mop      <= m_mop;
            w_wa       <= m_wa;
            w_alu      <= m_alu;
            w_rd       <= m_rd;
            w_wave     <= m_wave;
            w_wen      <= m_wen;
            w_op2_imm  <= m_op2_imm;
            w_laneid   <= m_laneid;
            w_smov     <= m_smov;
            w_shfl     <= m_shfl;
            w_sh_bcast <= m_sh_bcast;
            w_sh_idx   <= m_sh_idx;
            w_ld_sel   <= m_ld_sel;
            w_ld_data  <= m_ld_data;
            w_imm      <= m_imm;
            w_mask     <= m_mask;
        end
    end

    always @(posedge clk) begin
        if (!resetn) begin
            m_valid <= 1'b0;
        end else if (!hold_all) begin
            // A stalled cycle inserts a BUBBLE rather than freezing the stage,
            // which is what lets the hazard against this register clear.
            m_valid   <= go && !x_defer && !x_split && !x_join && !x_tmc;
            m_wa      <= (WAVES > 1) ? {x_wave, x_rd} : {{(AW-5){1'b0}}, x_rd};
            m_alu     <= x_alu;
            m_flt     <= x_flt;
            m_fop     <= x_fop;
            m_imul    <= x_imul;
            m_mop     <= x_mop;
            m_rd      <= x_rd;
            m_wave    <= x_wave;
            m_wen     <= x_wen && (x_rd != 5'd0);
            m_op2_imm <= x_op2_imm;
            m_laneid  <= x_laneid;
            m_smov      <= x_smov;
            m_shfl      <= x_shfl;
            m_sh_bcast  <= x_sh_bcast;
            m_sh_idx    <= x_sh_idx;
            m_ld_sel  <= x_ld_sel;
            m_ld_data <= x_ld_data;
            m_imm     <= x_imm;
            m_mask    <= mask;
        end
    end

    // ================= the lane array ======================================
    // An immediate is the SAME value in every lane, so it needs no broadcast
    // network: uniform by construction rather than by analysis.
    wire [VW-1:0] opnd_b;
    genvar Q;
    generate
    for (Q = 0; Q < LANES; Q = Q + 1) begin : g_bsel
        assign opnd_b[32*Q +: 32] = w_op2_imm ? w_imm : w2_q[32*Q +: 32];
    end
    endgenerate

    wire [VW-1:0] alu_y;
    kht_valu #(.LANES(LANES)) u_alu (
        .a(w1_q), .b(opnd_b), .op(w_alu), .y(alu_y)
    );

    genvar I;
    generate
    for (I = 0; I < LANES; I = I + 1) begin : g_wsel
        // THE FLOAT RETIRE STAYS BEHIND THIS MUX, not inside it. Folded in as a
        // sixth arm it cost 22 MHz: the float needs the SHORT side of the
        // writeback, and a priority chain in front of it is the binding path.
        assign vwdata[32*I +: 32] = w_ld_sel ? w_ld_data[32*I +: 32]
                                  : w_laneid ? I[31:0]
                                  : w_smov   ? w_imm
                                  // Constant-false when the gate is off, so the
                                  // mux input is trimmed rather than left
                                  // costing LUT a build cannot use.
                                  // Unit (I mod SU) serves lane I -- a
                                  // compile-time index, so a narrow build costs
                                  // no runtime select here.
                                  : ((HAS_SHFL != 0) && w_shfl)
                                             ? shfl_y[32*(I % SU) +: 32]
                                             : alu_y[32*I +: 32];
    end
    endgenerate

    // NAMED ONCE. Written out twice -- on the port and on the probe -- the tool
    // built the LANES x 32 mux twice, and the second copy measured 238 LUT.
    // NOT the same as folding this into `vwdata`, which put the float through
    // the whole five-way priority chain and cost 22 MHz.
    //
    // THE UNITS ARE PLACED HERE, AND EVERY SOURCE SLICE IS A CONSTANT: thread i
    // is served by unit (i mod units), which is a compile-time index because
    // every count is a power of two. So a fractional-rate build costs a mux a
    // bit here and never a runtime unit select. At full rate the three arms are
    // the same slice and it collapses back to the five-input form.
    genvar WI;
    generate
    // kht_fpu carries the divide-and-fit checks, and it is not elaborated at
    // FLANES = 0 -- so seed units asked for on a float-free build would be
    // silently dropped rather than refused.
    if ((FSFU_UNITS != 0) && (FLANES == 0)) begin : g_bad_sf0
        kht_unit_requires_FLANES_nonzero_for_FSFU_UNITS u_bad ();
    end
    // A count that does not divide LANES leaves lanes unwritten by a walk that
    // still elaborates and reports an Fmax -- khs_unit records that costing a
    // bench 10 of 66.
    if ((SHFL_UNITS > 0)
        && (((LANES / SHFL_UNITS) * SHFL_UNITS != LANES)
            || (SHFL_UNITS > LANES))) begin : g_bad_su
        kht_unit_requires_SHFL_UNITS_to_divide_LANES u_bad ();
    end
    // -1 is the ONLY legal negative: it means full rate. Anything below it is a
    // caller that meant a count and got the sign wrong.
    if (SHFL_UNITS < -1) begin : g_bad_sun
        kht_unit_SHFL_UNITS_below_minus_one_is_not_a_width u_bad ();
    end
    for (WI = 0; WI < LANES; WI = WI + 1) begin : g_wrsel
        localparam integer UF = WI % FLANES_W;
        localparam integer US = WI % SEED_U;
        localparam integer UM = WI % MUL_W;
        assign wr_data[32*WI +: 32] = !fwb_v   ? vwdata[32*WI +: 32]
                                    : fwb_mul  ? imul_y[32*UM +: 32]
                                    : fwb_seed ? fpu_y[32*US +: 32]
                                               : fpu_y[32*UF +: 32];
    end
    endgenerate

    // ================= the subgroup butterfly (G8) =========================
    // LOG2(LANES) STAGES, NOT A CROSSBAR. Stage k conditionally swaps across
    // distance 2^k, so the network is O(LANES log LANES) where an all-to-all
    // select is O(LANES^2) -- the shape kht_lds' resolver pays for.
    //
    // ONE NETWORK, BOTH INSTRUCTIONS. Lane i must end up holding vs1[src_i]:
    //     shflxor   src_i = i ^ m      (m uniform)
    //     bcast     src_i = L          (L uniform)
    // so the per-lane control is src_i ^ i either way, and the butterfly routes
    // it in log2(LANES) conditional swaps.
    //
    // A LANE WHOSE SOURCE IS INACTIVE READS ITS OWN VALUE, which the ISA fixes.
    // That is resolved BEFORE the network by zeroing the control, not inside it:
    // once data has moved a stage, "was my source active" is no longer a
    // question the intermediate lanes can answer.
    //
    // A WIDTH, AND THE WALK IS THE FLOAT TIER'S: unit u on pass p produces output
    // lane p*SU + u and the register file's per-lane write enable places it, so
    // there is NO staging register here -- SIMD's khs_perm needs one only because
    // khs_vregfile has no per-element enable. A butterfly cannot be sliced (it
    // routes every lane at once); one output lane is a LANES:1 32-bit mux, and
    // that is what a unit is here.
    generate
    if (HAS_SHFL == 0) begin : g_noshfl
        assign shfl_y = {(32*SU){1'b0}};
        assign shfl_hold    = 1'b0;
        assign shfl_pass_en = {LANES{1'b1}};
    end else if (SU < LANES) begin : g_shfl_walk
        reg [SPSW-1:0] sp_q;
        // `!x_hold` and NOT `!hold_all`: the latter contains this block's own
        // hold and reading it here closes a combinational loop -- the same trap
        // `f_live` records. A STARTED WALK FINISHES under a foreign stall,
        // because `w1_q` and `w_mask` are frozen by `hold_all` either way.
        wire s_live   = w_valid && w_shfl && (!x_hold || (sp_q != {SPSW{1'b0}}));
        wire s_last   = (sp_q == (SPASS[SPSW-1:0] - 1'b1));
        assign shfl_hold = s_live && !s_last;

        always @(posedge clk) begin
            if (!resetn) begin
                sp_q <= {SPSW{1'b0}};
            end
            else if (s_live) begin
                sp_q <= s_last ? {SPSW{1'b0}} : (sp_q + 1'b1);
            end
        end

        // Lane l belongs to pass l/SU. Constant per lane, so this is a decode of
        // `sp_q` and not a comparison per lane.
        genvar LE;
        for (LE = 0; LE < LANES; LE = LE + 1) begin : g_pen
            localparam integer LP = LE / SU;
            assign shfl_pass_en[LE] = !w_shfl || (sp_q == LP[SPSW-1:0]);
        end

        genvar U;
        for (U = 0; U < SU; U = U + 1) begin : g_sunit
            // The output lane this unit owns on this pass.
            wire [LNW-1:0] olane = sp_q * SU + U;
            wire [LNW-1:0] src   = w_sh_bcast ? w_sh_idx : (olane ^ w_sh_idx);
            // An inactive source means the lane keeps its own value; folded into
            // the INDEX, so one mux rather than two.
            wire [LNW-1:0] sel   = w_mask[src] ? src : olane;
            assign shfl_y[32*U +: 32] = w1_q[32*sel +: 32];
        end
    end else begin : g_shfl
        assign shfl_hold    = 1'b0;
        assign shfl_pass_en = {LANES{1'b1}};

        wire [LNW-1:0] src  [0:LANES-1];
        wire [LNW-1:0] ctl  [0:LANES-1];
        wire [VW-1:0]  stg  [0:LNW];
        assign stg[0] = w1_q;

        genvar S, I2, K;
        // WB-stage control, because `w1_q` is the WB instruction's operand: the
        // registered read port already made EX-stage control one cycle early,
        // and the writeback stage makes it two.
        for (S = 0; S < LANES; S = S + 1) begin : g_ctl
            assign src[S] = w_sh_bcast ? w_sh_idx : (S[LNW-1:0] ^ w_sh_idx);
            assign ctl[S] = w_mask[src[S]] ? (S[LNW-1:0] ^ src[S]) : {LNW{1'b0}};
        end
        for (K = 0; K < LNW; K = K + 1) begin : g_stage
            for (I2 = 0; I2 < LANES; I2 = I2 + 1) begin : g_sw
                localparam integer PARTNER = I2 ^ (1 << K);
                assign stg[K+1][32*I2 +: 32] = ctl[I2][K]
                                             ? stg[K][32*PARTNER +: 32]
                                             : stg[K][32*I2 +: 32];
            end
        end
        assign shfl_y = stg[LNW];
    end
    endgenerate

    // ================= G9: the float tier ==================================
    generate
    if ((HAS_FLT == 0) && (HAS_MUL == 0)) begin : g_noflt
        assign fpu_y     = {(32*FLANES_W){1'b0}};
        assign imul_y    = {(32*MUL_W){1'b0}};
        assign fwb_mul   = 1'b0;
        assign fwb_seed  = 1'b0;
        assign fwb_v     = 1'b0;
        assign fwb_wa    = {AW{1'b0}};
        assign fwb_mask  = {LANES{1'b0}};
        assign fpend     = {WAVES{1'b0}};
        assign f_soon    = 1'b0;
        assign f_pass_hold_i = 1'b0;
    end else begin : g_flt
        // kht_fpu's own depth, which it checks against this. The shadow pipe
        // below MUST match it: it carries the destination and the mask that the
        // lane array does not, and if the two disagree a result lands on the
        // wrong register with no witness.
        localparam integer FLAT = (FSFU_UNITS != 0) ? 10 : 6;

        // THE WALK. A feature narrower than the thread array issues one launch
        // per pass on consecutive cycles -- the lane is II = 1, so the results
        // come back on consecutive cycles too and each claims the write port on
        // its own. `!x_hold` and NOT `!hold_all`: the latter contains this
        // block's own `fpass_hold` and reading it here closes a combinational
        // loop, which is the trap khs_unit's `f_pass_hold` records.
        // DECLARED BEFORE THE WIRE THAT READS IT: xvlog rejects the
        // use-before-declare that synthesis accepts silently.
        reg  [PSW-1:0] fp_q;

        wire w_seed = w_flt && (FSFU_UNITS != 0) && w_fop[2];
        // A STARTED WALK FINISHES. Gating the continuation on `!x_hold` too
        // means an L1 miss or an LSU walk beginning mid-walk drops `fpass_hold`,
        // releases the WB register and abandons the remaining passes -- their
        // threads then keep the destination's old value with no witness. The
        // operands are frozen by `hold_all` either way and the lane free-runs,
        // so continuing under a foreign stall is exactly correct.
        // `w_flt` is set by a float instruction whether or not the tier exists:
        // `unbuilt` faults it at EX, but the MEM register still carries it. On a
        // multiply-only build that would start a walk against units that were
        // never elaborated, so the count gates the launch as well as the decode.
        wire f_live = w_valid && ((w_flt && (HAS_FLT != 0)) || w_imul)
                   && (!x_hold || (fp_q != {PSW{1'b0}}));

        // Constants, so this is a select and never a divide.
        wire [PSW-1:0] last_pass = w_imul ? (LANES / MUL_W   - 1)
                                 : w_seed ? (LANES / SEED_U  - 1)
                                          : (LANES / FLANES_W - 1);
        wire           iss_last = (fp_q == last_pass);
        assign f_pass_hold_i = f_live && !iss_last;

        always @(posedge clk) begin
            if (!resetn) begin
                fp_q <= {PSW{1'b0}};
            end
            else if (f_live) begin
                fp_q <= iss_last ? {PSW{1'b0}} : (fp_q + 1'b1);
            end
        end

        wire f_launch = f_live && w_flt && (HAS_FLT != 0);
        wire m_launch = f_live && w_imul;

        // EACH UNIT ARRAY IS BUILT ONLY IF ITS COUNT IS NONZERO, so `mul` on a
        // float-free build and floats on a multiply-free build are both real
        // configurations rather than one gate carrying both.
        if (HAS_FLT != 0) begin : g_fpu
            kht_fpu #(.LANES(LANES), .FLANES(FLANES), .FSFU_UNITS(SEED_N),
                      .PSW(PSW_F), .ALAT(FLAT)) u_fpu (
                .clk(clk), .rst(!resetn),
                .in_valid(f_launch), .op(w_fop), .pass(fp_q[PSW_F-1:0]),
                .va(w1_q), .vb(w2_q), .vc(w3_q),
                .out_valid(), .vy(fpu_y)
            );
        end else begin : g_nofpu
            assign fpu_y = {(32*FLANES_W){1'b0}};
        end

        // SAME LATENCY, SO NO ARBITRATION. Two results can only want the write
        // port on one cycle if they were issued on one cycle, and exactly one
        // instruction issues per cycle.
        if (HAS_MUL != 0) begin : g_imul
            kht_imul #(.LANES(LANES), .MUNITS(LANES), .PSW(PSW_M),
                       .MLAT(FLAT)) u_imul (
                .clk(clk), .rst(!resetn),
                .in_valid(m_launch), .op(w_mop), .pass(fp_q[PSW_M-1:0]),
                .va(w1_q), .vb(w2_q), .vy(imul_y)
            );
        end else begin : g_noimul
            assign imul_y = {(32*MUL_W){1'b0}};
        end

        reg [FLAT:1]      fsh_v;
        reg [FLAT:1]      fsh_mul;      // which unit's result this slot carries
        reg [FLAT:1]      fsh_seed;     // and whether it walked the seed units
        reg [FLAT:1]      fsh_last;     // the pass that finishes the instruction
        reg [AW-1:0]      fsh_wa   [1:FLAT];
        reg [LANES-1:0]   fsh_mask [1:FLAT];
        reg [WID-1:0]     fsh_wave [1:FLAT];
        reg [PSW-1:0]     fsh_pass [1:FLAT];
        reg [WAVES-1:0]   fpend_q;

        integer fi;
        always @(posedge clk) begin
            if (!resetn) begin
                fsh_v   <= {FLAT{1'b0}};
                fpend_q <= {WAVES{1'b0}};
            end else begin
                // FREE-RUNNING, like the lane array it shadows: `kht_fpu` has no
                // clock enable, so gating this would desynchronise the two.
                fsh_v[1]    <= f_launch | m_launch;
                fsh_mul[1]  <= m_launch;
                fsh_seed[1] <= w_seed;
                fsh_last[1] <= iss_last;
                fsh_wa[1]   <= w_wa;
                fsh_mask[1] <= w_mask;
                fsh_wave[1] <= w_wave;
                fsh_pass[1] <= fp_q;
                for (fi = 2; fi <= FLAT; fi = fi + 1) begin
                    fsh_v[fi]    <= fsh_v[fi-1];
                    fsh_mul[fi]  <= fsh_mul[fi-1];
                    fsh_seed[fi] <= fsh_seed[fi-1];
                    fsh_last[fi] <= fsh_last[fi-1];
                    fsh_wa[fi]   <= fsh_wa[fi-1];
                    fsh_mask[fi] <= fsh_mask[fi-1];
                    fsh_wave[fi] <= fsh_wave[fi-1];
                    fsh_pass[fi] <= fsh_pass[fi-1];
                end

                // Set at ISSUE, not at launch: EX is two stages ahead of the
                // launch and the wave must already be blocked by then.
                if (go && (x_flt || x_imul)) begin
                    fpend_q[x_wave] <= 1'b1;
                end
                // ON THE LAST PASS ONLY. Clearing on the first would release the
                // wave while later passes are still writing its destination.
                if (fsh_v[FLAT] && fsh_last[FLAT]) begin
                    fpend_q[fsh_wave[FLAT]] <= 1'b0;
                end
            end
        end

        // WHICH THREADS THIS PASS OWNS. Thread i belongs to pass i/units, a
        // compile-time constant, so this is a decode of the retiring pass index
        // and not a comparator per lane.
        wire [LANES-1:0] pass_en;
        genvar PI;
        for (PI = 0; PI < LANES; PI = PI + 1) begin : g_pen
            localparam integer SF = PI / FLANES;
            localparam integer SS = PI / SEED_U;
            localparam integer SM = PI / MUL_UNITS;
            // EACH ARM AT ITS OWN WALK'S WIDTH. The other arms' upper bits are
            // don't-care under their own select, so a narrower compare is exact
            // and the decode is the size the walk can actually reach.
            assign pass_en[PI] =
                  fsh_mul[FLAT]  ? (fsh_pass[FLAT][PSW_M-1:0] == SM)
                : fsh_seed[FLAT] ? (fsh_pass[FLAT]            == SS)
                                 : (fsh_pass[FLAT][PSW_A-1:0] == SF);
        end

`ifdef KHT_FTRACE
        always @(posedge clk) if (resetn) begin
            // NOT "%0t ..." first: the runner's line filter drops output that
            // starts with a digit, so a timestamped trace vanishes silently.
            if (go && x_flt) begin
                $display("  FTRACEissue  t=%0t wave %0d rd %0d", $time, x_wave, x_rd);
            end
            if (f_launch) begin
                $display("  FTRACElaunch t=%0t op %0d a %04h b %04h c %04h",
                         $time, w_fop, w1_q[15:0], w2_q[15:0], w3_q[15:0]);
            end
            if (fsh_v[FLAT]) begin
                $display("  FTRACEwrite  t=%0t wa %0d mask %02h y %04h",
                         $time, fsh_wa[FLAT], fsh_mask[FLAT], fpu_y[15:0]);
            end
        end
`endif
        assign fwb_v    = fsh_v[FLAT];
        assign fwb_wa   = fsh_wa[FLAT];
        assign fwb_mask = fsh_mask[FLAT] & pass_en;
        assign fwb_mul  = fsh_mul[FLAT];
        assign fwb_seed = fsh_seed[FLAT];
        assign fpend    = fpend_q;
        // Two cycles of warning: the core drops `go`, which becomes an empty
        // MEM next cycle and an empty WB the cycle after -- exactly when the
        // float needs the port.
        assign f_soon   = fsh_v[FLAT-2];
    end
    endgenerate

    // A distance-1 dependency stalls rather than forwarding: a VW-wide bypass
    // mux is the widest path in the unit, against one cycle on a dependency a
    // shader compiler can usually schedule around.
    // Compare first, select after: each comparator starts at the instruction's
    // own register field instead of behind a decode-driven mux.
    // EVERYTHING THAT IS NOT A REGISTER NUMBER IS REGISTER-TO-REGISTER, so it
    // belongs off the instruction word's cone. Written as one flat AND the tool
    // spent FOUR levels here, and `stall` feeds `hold`, which every clock enable
    // in the core sits behind.
    // TWO DISTANCES, because the write lands two cycles after the read is
    // issued: a read in EX misses BOTH the write happening at the end of this
    // cycle (WB) and the one a cycle behind it (MEM).
    wire m_act = m_valid && m_wen && (m_rd != 5'd0)
              && ((WAVES == 1) || (x_wave == m_wave));
    wire w_act = w_valid && w_wen && (w_rd != 5'd0)
              && ((WAVES == 1) || (x_wave == w_wave));
    wire hz_a = (x_rs1  == m_rd);
    wire hz_b = (x_rs1b == m_rd);
    wire hz_c = (x_rs2  == m_rd);
    wire hz_d = (x_rs1  == w_rd);
    wire hz_e = (x_rs1b == w_rd);
    wire hz_f = (x_rs2  == w_rd);
    // A FLOAT READS ITS DESTINATION: `vfma` is vd = vs1*vs2 + vd, so `rd` is a
    // source and has to be compared like one. `x_rs1b` already carries `rd` for
    // the vmem-store case, so this is the same comparator under a second
    // condition rather than a fourth one. Without it a vfma reads its addend
    // from before the instruction that set it -- seen as c = 0x0400 where the
    // shader had just built 0x4000.
    assign hz_raw = x_valid
                 && ((m_act && ((x_rs1_sel ? hz_b : hz_a) || hz_c
                                || (x_flt && hz_b)))
                  || (w_act && ((x_rs1_sel ? hz_e : hz_d) || hz_f
                                || (x_flt && hz_e))));
    assign stall  = hz_raw;

    // THE FILE'S WRITE PORT, NOT THE MEM STAGE'S INTENT. A float or multiply
    // result arrives through its own retire slot, so reporting `w_valid` alone
    // makes those writes invisible -- and the core snoops this to find a0, so a
    // shader whose halt word came from a multiply reported whatever a0 held
    // before it. khs_unit's probe is the write port for the same reason.
    assign dbg_wr_valid = fwb_v ? 1'b1 : (w_valid && w_wen);
    assign dbg_wr_rd    = fwb_v ? fwb_wa[4:0] : w_rd;
    assign dbg_wr_mask  = fwb_v ? fwb_mask : w_mask;
    assign dbg_wr_data  = wr_data[31:0];

`ifndef SYNTHESIS
    // A split while a wave is half-unwound would overwrite the pair it is still
    // reading. Balanced code cannot do it -- every split has two joins -- but
    // "cannot happen" is what this project has been wrong about before, and the
    // flop version had no such window to be wrong in.
    generate
    if ((HAS_MASK != 0) && (HAS_IPDOM != 0)) begin : g_ipchk
        always @(posedge clk) begin
            if (resetn && go_c && x_split && g_mask.g_ip.phase[x_wave]) begin
                $display("%0t ERROR kht_unit: wave %0d split while half-unwound -- the top pair is overwritten",
                         $time, x_wave);
            end
        end
    end
    endgenerate

    always @(posedge clk) if (resetn && ip_fault)
        $display("%0t kht_unit: IPDOM fault -- a split at depth %0d or a join on an empty stack; depth %0d permits %0d nested levels",
                 $time, IPDOM_D, IPDOM_D, IPDOM_D / 2);
    // An all-zero mask is not a defined case for vreadfirst, and the scheduler
    // must never issue such a wave. Reasoning that it cannot happen is how this
    // project has been wrong before, so it is checked instead.
    always @(posedge clk) if (resetn && go && (mask == {LANES{1'b0}}))
        $display("%0t ERROR kht_unit: wave %0d issued with an all-zero active mask",
                 $time, x_wave);
`endif

endmodule

`default_nettype wire

// kht_core -- BUT manually refined format

`default_nettype none

module kht_core #(
    parameter integer LANES     = 8,
    parameter integer WAVES     = 16,
    parameter integer WID       = (WAVES > 1) ? $clog2(WAVES) : 1,
    parameter integer HAS_MASK  = 1,
    parameter integer HAS_IPDOM = 1,
    // G4. At 0 the banked path elaborates nothing and LDS goes down the serial
    // walk exactly as every other region does.
    parameter integer HAS_LDSBANK = 1,
    parameter integer HAS_SHFL  = 1,
    parameter integer SHFL_UNITS = 0,
    // THREE INDEPENDENT COUNTS, each legal at 0, 1, 2, 4, 8, and none derived
    // from LANES. `FLANES = LANES` was a binding, not a default: it made the
    // float width track the thread width and there was no way to say "eight
    // threads, two float units" at all. 0 IS "not built" -- there is no separate
    // HAS_FLT beside them, which is what made `mul` unbuildable without floats.
    // A nonzero count that does not divide LANES is refused at elaboration.
    parameter integer FLANES     = 0,
    parameter integer FSFU_UNITS = 0,
    parameter integer MUL_UNITS  = 0,
    parameter integer HAS_F16    = 1,
    parameter integer HAS_F32    = 1,
    parameter integer FMODEL     = 0,
    parameter integer IPDOM_D    = 8,
    parameter integer IMEM_WORDS = 2048,
    parameter integer SPAD_WORDS = 2048,
    parameter         VREG_PRIM  = "block"
)(
    input  wire        clk,
    input  wire        resetn,

    input  wire        boot_v,
    input  wire [31:0] boot_pc,
    // How many waves this kick launches. 1 is exactly the single-wave case.
    input  wire [7:0]  boot_n,
    output wire        run,
    output wire        halted,
    output wire [1:0]  cause,
    output wire [31:0] halt_word,
    output wire        pipe_empty,

    input  wire [31:0] arg,
    input  wire [7:0]  coreid,
    input  wire [15:0] wr_out,

    output wire [$clog2(IMEM_WORDS)-1:0] imem_addr,
    input  wire [31:0] imem_data,
    // Decode, read out of the window beside the instruction.
    input  wire [59:0] imem_ctrl,

    output wire [$clog2(SPAD_WORDS)-1:0] spad_addr,
    output wire [3:0]  spad_we,
    output wire [31:0] spad_wdata,
    input  wire [31:0] spad_rdata,

    // The banked LDS, one address per lane. Unused and tied off at
    // HAS_LDSBANK = 0.
    output wire                    lds_req,
    output wire                    lds_we,
    output wire [LANES-1:0]        lds_mask,
    output wire [LANES*$clog2(SPAD_WORDS)-1:0] lds_addr,
    output wire [LANES*32-1:0]     lds_wdata,
    input  wire [LANES*32-1:0]     lds_rdata,
    input  wire                    lds_done,
    input  wire [31:0]             lds_passes,

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

    output wire [31:0] dbg_retire_pc,
    output wire        dbg_retire_valid,
    output wire [LANES-1:0] dbg_mask,
    output wire [31:0] cycle_ctr,
    output wire [31:0] instret_ctr,
    // Requests the LSU issued, and gathers it issued them for: the pair IS the
    // coalescer witness, and it is counted whether or not one exists yet.
    output wire [31:0] req_ctr,
    output wire [31:0] gather_ctr
);
`include "kht_isa.vh"
`include "kht_ctrl.vh"

    localparam integer IAW = $clog2(IMEM_WORDS);
    localparam integer SAW = $clog2(SPAD_WORDS);
    localparam integer VW  = 32 * LANES;
    localparam integer LNW = (LANES > 1) ? $clog2(LANES) : 1;
    localparam integer SAW_S = $clog2(WAVES * 32);

    localparam [3:0] A_ADD = 4'd0, A_SUB = 4'd1, A_SLL = 4'd2, A_SLT = 4'd3;
    localparam [3:0] A_SLTU= 4'd4, A_XOR = 4'd5, A_SRL = 4'd6, A_SRA = 4'd7;
    localparam [3:0] A_OR  = 4'd8, A_AND = 4'd9;

    localparam [2:0] R_SPAD = 3'd1, R_CTL = 3'd2, R_LDS = 3'd4;
    localparam [2:0] R_DRAM = 3'd5, R_BAD = 3'd7;

    // Everything the blocks below share, declared before any of them: xvlog
    // rejects a use-before-declare that synthesis had accepted silently.
    wire [VW-1:0]    vt_rd1, vt_rd2;
    wire             lsu_busy;
    wire [31:0]      vs_scalar;
    wire             go;
    wire             vt_wr_v;
    wire [4:0]       vt_wr_rd;
    wire [LANES-1:0] vt_wr_mask;
    reg  [31:0]      ctl_rd;
    reg  [31:0]      cyc_q, ret_q;
    reg  [31:0]      a0_q;
    // rv_l1 acts on `flush` in L_IDLE only and asserts if it is raised later,
    // so the request is a ONE-CYCLE PULSE and the wait is split into "busy has
    // risen" and "busy has fallen" -- busy does not rise in the pulse cycle.
    localparam [2:0] H_RUN = 3'd0, H_FLUSH = 3'd1, H_RISE = 3'd2;
    localparam [2:0] H_FALL = 3'd3, H_DONE = 3'd4;
    reg  [2:0]       hst;

    // ================= wave state ==========================================
    // `nxt` IS the architectural PC and the fetch pointer at once, one per wave.
    // A separate committed copy bought nothing: nothing reads it, because a
    // redirect rewrites this array directly.
    reg  [31:0]      nxt   [0:WAVES-1];
    reg  [WAVES-1:0] live;
    // WHICH WAVE IS BEING FETCHED, AS A REGISTER. The round-robin pick is a
    // WAVES-deep priority chain -- five LUT levels, measured -- and it sat
    // between `live` and the window's address pins. It is a function of `live`
    // and of itself, both registers, so it belongs on a flop-to-flop path and
    // the address mux gets to start from a flop.
    reg  [WID-1:0]   cur_q;
    wire [WID-1:0]   pick_n;
    // G9. A wave with a float in flight is not runnable. NOT a scoreboard: the
    // machine deletes forwarding and interlocks on the argument that with
    // WAVES >= pipeline depth no two in-flight instructions share a wave, and a
    // 15-cycle float unit breaks that precondition -- this restores it.
    wire [WAVES-1:0] fpend;
    wire [WAVES-1:0] rdy = live & ~fpend;
    wire             f_soon;
    // A feature narrower than the thread array is still issuing. It stays OUT of
    // `base_hold`: that is what kht_unit's launch reads, and routing this into it
    // closes a combinational loop.
    wire             fpass_hold;

    // REPLICATED. The round-robin pick is five LUT levels of priority chain and
    // then drives 267 loads -- every slice of the per-wave PC mux and every
    // one-hot decode. Measured 0.426 ns of logic against 1.01 ns of route, so
    // the net is 70% of its own cone.
    (* max_fanout = 32 *) wire [WID-1:0] cur;
    wire             any_live = |live;

    wire [WAVES-1:0] boot_mask, one_hot;
    generate
    if (WAVES == 1) begin : g_one_w
        assign boot_mask = 1'b1;
        assign one_hot   = 1'b1;
    end else begin : g_many_w
        // Python-style multi line branching statement
        // ? are matched across line, : are also matched
        // global () is needed
        wire [7:0] bn = (
            (boot_n == 8'd0)        ? 8'd1
            : (boot_n > WAVES[7:0]) ? WAVES[7:0]
            : boot_n
        );
        assign boot_mask = ~({WAVES{1'b1}} << bn);
        assign one_hot   = {{(WAVES-1){1'b0}}, 1'b1} << f2_wave;
    end
    endgenerate

    generate
    if (WAVES == 1) begin : g_one
        assign cur    = 1'b0;
        assign pick_n = 1'b0;
    end else begin : g_rr
        reg [WID-1:0] pick;
        integer k;
        wire [WID-1:0] base = cur_q + 1'b1;
        always @(*) begin
            pick = base;
            // ALWAYS PROPER BEGIN AND END
            // This can avoid tons of bug due to bad scoping
            for (k = WAVES - 1; k >= 0; k = k - 1) begin
                if (rdy[(base + k[WID-1:0])]) begin
                    pick = base + k[WID-1:0];
                end
            end
        end
        assign pick_n = pick;
        assign cur    = cur_q;
    end
    endgenerate

    wire        hold;
    wire        base_hold, lsu_need, lsu_want, warm_stall, vt_stall, s_hz;
    wire        per_lane;
    wire        go_nh, rdir_nh, kill_ev;
    wire [WAVES-1:0] nsel, rsel;
    reg  [3:0]  warm_q;

    reg  [31:0]    f1_pc, f2_pc;
    (* max_fanout = 24 *) reg [WID-1:0] f1_wave, f2_wave;
    reg            f1_valid, f2_valid;
    reg [31:0]     i2;
    reg [59:0]     c2;

    // HELD, THE WINDOW MUST RE-PRESENT THE ADDRESS IN FLIGHT. `f1` is frozen by
    // the same `hold`, so letting the address advance would fetch a word the
    // resumed `f2 <= f1` no longer names -- an instruction silently skipped on
    // every hazard, which is what this cost before the register existed.
    wire [31:0] fetch_pc = hold ? f1_pc : nxt[cur];
    assign imem_addr = fetch_pc[IAW+1:2];

    // NOT `fetch_pc + 4` SHARED ACROSS THE WAVES. `nsel` is one-hot on `cur`, so
    // one incrementer behind the array mux is equivalent -- and measured +15 LUT
    // and a NEW binding path, i2 -> nxt at 12 levels and 4 CARRY8: the tool
    // already shares these chains, and the mux in front of one is not free.

    wire [31:0] redir_pc;
    wire        redir;

    // TWO fetches are wrong-path on a redirect: the one sitting in `f1` and the
    // one being issued this cycle. From the next cycle `nxt` already holds the
    // target. Both are wrong-path only if they belong to the redirecting wave --
    // under interleaving the instruction behind a branch usually belongs to a
    // different wave and must survive.
    wire wave_ends;
    wire kill_f1  = kill_ev && (f1_wave == f2_wave);
    wire kill_new = kill_ev && (cur     == f2_wave);

    // WHICH wave's pointer moves, decided WITHOUT `hold`. `rsel` is the
    // redirected wave and `nsel` the fetched one; they are the same wave on a
    // taken branch, and `rsel` wins because it is the mux's select.
    genvar gw;
    generate
    for (gw = 0; gw < WAVES; gw = gw + 1) begin : g_nsel
        assign nsel[gw] = (cur == gw[WID-1:0]);
    end
    endgenerate
    assign rsel = one_hot & {WAVES{rdir_nh}};

    integer wf;
    always @(posedge clk) begin
        if (!resetn) begin
            f1_valid <= 1'b0;
            f2_valid <= 1'b0;
            cur_q    <= {WID{1'b0}};
            for (wf = 0; wf < WAVES; wf = wf + 1) begin
                nxt[wf] <= 32'd0;
            end
        end else if (boot_v) begin
            f1_valid <= 1'b0;
            f2_valid <= 1'b0;
            cur_q    <= {WID{1'b0}};
            for (wf = 0; wf < WAVES; wf = wf + 1) begin
                nxt[wf] <= boot_pc;
            end
        end else if (!hold) begin
            // The window's output belongs to f1's address, so the two advance
            // together or the instruction and the PC naming it come apart.
            f2_valid <= f1_valid && !kill_f1;
            f2_pc    <= f1_pc;
            f2_wave  <= f1_wave;
            i2       <= imem_data;
            c2       <= imem_ctrl;

            // `rdy[cur]`, not `any_live`: the pick ran against a `rdy` that is
            // one cycle stale, so this is where a wave that ended -- or took a
            // float in flight -- in the meantime is refused a fetch slot.
            f1_valid <= rdy[cur] && !kill_new;
            f1_pc    <= nxt[cur];
            f1_wave  <= cur;
            //no matter HOW SMALL the statement is, any block that support begin-end should HAVE begin-end
            if (WAVES > 1) begin
                cur_q <= pick_n;
            end
            // `rdy[cur]` GATES THE INCREMENT, not just the fetch. Advancing a
            // pointer on a cycle that issued no fetch skips an instruction, and
            // it was invisible until G9: before `fpend` the only unready wave
            // was a DEAD one, whose pointer nobody reads again. A wave blocked
            // on a float comes back, and came back one instruction late for
            // every cycle it waited.
            for (wf = 0; wf < WAVES; wf = wf + 1) begin
                if ((nsel[wf] && rdy[cur]) || rsel[wf]) begin
                    nxt[wf] <= rsel[wf] ? redir_pc : (nxt[wf] + 32'd4);
                end
            end
        end
    end

    wire [31:0] instr = i2;
    wire [59:0] ictl  = c2;
    // Widening the control word and leaving the ports behind has already cost
    // one synthesis run to an out-of-range part-select. `$bits` is SystemVerilog
    // and this file is Verilog-2001, so the literal is checked instead.
    initial if (KHT_CW != 60) begin
        // Python style formatting for long call
        $display(
            "ERROR kht_core: kht_ctrl.vh says KHT_CW=%0d; the ports here, in kht_predec and in kht_pe all carry 60",
            KHT_CW
        );
    end

    // ================= decode ==============================================
    wire [6:0] opc = instr[6:0];
    wire [2:0] f3  = instr[14:12];
    wire [6:0] f7  = instr[31:25];
    wire [4:0] rd  = instr[11:7];
    // REPLICATED. Both address a 512-entry scalar file AND cross into kht_unit
    // for the vector file and the hazard: `rs2` measured fanout 355 with
    // 0.413 ns of route, the largest single item on the binding path after the
    // window's own clock-to-out.
    (* max_fanout = 16 *) wire [4:0] rs1 = instr[19:15];
    (* max_fanout = 16 *) wire [4:0] rs2 = instr[24:20];

    wire [31:0] imm_i = {{20{instr[31]}}, instr[31:20]};
    wire [31:0] imm_s = {{20{instr[31]}}, instr[31:25], instr[11:7]};
    wire [31:0] imm_u = {instr[31:12], 12'd0};
    //python style long value
    wire [31:0] imm_j = {
        {11{instr[31]}}, instr[31], instr[19:12], instr[20], instr[30:21], 1'b0
    };

    // UNPACKED, NOT DECODED. Everything below comes out of the window beside
    // the instruction, computed once when the image landed. It used to be
    // computed here, between the window and this core's own registers, and four
    // of the thirteen levels to the per-wave PC's clock enable were spent
    // producing `mem_store` alone. See kht_ctrl.vh.
    wire is_salu   = ictl[C_SALU];
    wire is_smov   = ictl[C_SMOV];
    wire is_div    = ictl[C_DIV];
    wire is_sub    = ictl[C_SUB];
    wire is_vmem   = ictl[C_VMEM];

    wire is_split  = ictl[C_SPLIT];
    wire is_join   = ictl[C_JOIN];
    wire is_tmc    = ictl[C_TMC];

    wire is_s2v    = ictl[C_S2V];
    wire is_rdctl  = ictl[C_RDCTL];

    wire is_ballot = ictl[C_BALLOT];
    wire is_vrf    = ictl[C_VRF];
    wire is_redux  = ictl[C_REDUX];
    wire is_shfl   = ictl[C_SHFL];
    wire is_bcast  = ictl[C_BCAST];
    wire is_lanid  = ictl[C_LANID];

    // scale and width stay raw funct7 slices: wires, not logic.
    wire [1:0] mem_scale = f7[3:2];
    wire [1:0] mem_width = f7[1:0];
    wire       mem_lin   = ictl[C_MEMLIN];
    wire       mem_store = ictl[C_MEMST];

    wire is_sbeqz = ictl[C_SBEQZ];
    wire is_sbr   = ictl[C_SBR];
    wire is_simm  = ictl[C_SIMM];

    wire is_lui   = ictl[C_LUI];
    wire is_auipc = ictl[C_AUIPC];
    wire is_jal   = ictl[C_JAL];
    wire is_jalr  = ictl[C_JALR];
    wire is_load  = ictl[C_LOAD];
    wire is_store = ictl[C_STORE];
    wire is_flt   = ictl[C_FLT];
    wire is_imul  = ictl[C_IMUL] && (MUL_UNITS != 0);

    // THE VECTOR READ IS REGISTERED, so `vt_rd1` in EX belongs to the MEM-stage
    // instruction. Anything that consumes it IN EX -- the split predicate and
    // the three vector-to-scalar paths -- must be held until the read has
    // caught up with its own address AND with the write ahead of it.
    // PER-LANE ACCESSES OWE IT TOO, gate or no gate. The serial walk used to be
    // safe for free -- `lsu_run` is a register and bought the cycle -- but
    // `ea_all_q` is a register BEHIND the vector read, so the walk must wait for
    // it as well. One cycle per memory instruction, not per lane.
    wire ex_reads_v = ictl[C_EXRDV] || per_lane;
    // One cycle for the registered vector read; a reduction owes LNW more for
    // its pipelined tree to drain. The operands are held stable throughout, so
    // the pipeline simply fills.
    // 2 for a per-lane access: one for the registered vector read, one for
    // `ea_all_q` behind it. 4 with the banked LDS, which then needs one more for
    // the per-lane region bits and one for their reduction -- deciding
    // LDS-versus-DRAM is every lane's address and region, far too deep to also
    // start a walk in the same cycle.
    //
    // COMPARE FIRST, SELECT AFTER. A muxed `warm_need` fed a 4-bit comparator,
    // putting the mux AND the compare on the window's cone into `hold`; the
    // three counts are constants, so their tests are pure decodes of a register
    // and only the 3:1 select is left downstream of the instruction.
    wire w_ge1 = (warm_q >= 4'd1);
    wire w_ge2 = (warm_q >= 4'd2);
    wire w_ge4 = (warm_q >= 4'd4);
    // LNW + 2: one for the registered vector read, one for the registered tree
    // LEAVES, and LNW for the pipelined levels above them.
    wire w_gered = (warm_q >= (LNW[3:0] + 4'd2));
    // python style nested branching
    wire warm_ok = (
        is_redux ? w_gered
        : ictl[C_PERLANE] ? (
            (HAS_LDSBANK != 0) ? w_ge4 : w_ge2
        )
        : w_ge1
    );
    assign warm_stall = ex_reads_v && f2_valid && !warm_ok;

    always @(posedge clk) begin
        if (!resetn) begin
            warm_q <= 4'd0;
        end else if (go) begin
            warm_q <= 4'd0;
        // example on long if-else chain, ALL of them need begin-end
        // also eample on long if condition, still, python style multi line
        end else if (
            f2_valid
            && !vt_stall
            && (warm_q != 4'hF)
        ) begin
            warm_q <= warm_q + 4'd1;
        end
    end

    wire [3:0] alu_op = ictl[C_ALU0+3:C_ALU0];

    // nested inline logic
    wire vt_wen    = (
        ictl[C_VTWEN]
        && (rd != 5'd0)
        && (
            (HAS_SHFL != 0) 
            || !(is_shfl || is_bcast)
        )
    );
    wire vt_op2imm = ictl[C_VTOP2I];

    // Without the butterfly, or without the float tier, these ENCODE with no
    // datapath behind them, so a build that lacks one faults rather than
    // returning a plausible wrong answer.
    // The multiplier shares the float tier's retire slot and pending bit but is
    // its OWN unit count: at MUL_UNITS = 0 a multiply faults rather than
    // returning whatever the ordinary ALU made of a funct7 it does not know,
    // and it is buildable on a machine with no float units at all.
    // A seed is funct7[2] inside the float group, so a build without the seeds
    // FAULTS on one rather than returning whatever FMA made of the operands.
    wire is_seed_op = is_flt && ictl[C_FOP0+2];
    // HAS_MASK AND HAS_IPDOM BELONG HERE TOO: g_nomask has no `m_q` and g_noip
    // handles only `x_tmc`, so tmc/split/join DECODED, reached the unit, wrote
    // nothing and did not fault -- the one outcome this expression prevents.
    // The stack lives INSIDE the mask generate, so split/join need both gates.
    // A BCAST SOURCE LANE ABOVE THE THREAD COUNT MUST FAULT, NOT ALIAS.
    // `x_sh_idx` is rs2[LNW-1:0], so `bcast vd, vs1, 5` on a 4-lane build read
    // lane 1 -- the same binary answering differently on a narrower machine.
    // `shflxor` is not on the list: its operand is an xor MASK, and folding is
    // its definition rather than an aliasing accident.
    wire bad_lane = is_bcast && (rs2 >= LANES);
    // THE FORMAT BIT IS funct7[3] inside the float group. A format the build does
    // not carry is an absent encoding, exactly as an absent group is -- not the
    // other format's conversion applied to the wrong bits.
    wire is_half_op = is_flt && ictl[C_FOP0+3];
    // for nested logic, proper leveling can ensure ppl UNDERSTAND the bracket level
    wire unbuilt = (
        bad_lane
        || ((HAS_SHFL == 0) && (is_shfl || is_bcast))
        || ((FLANES == 0) && is_flt)
        || ((HAS_F16 == 0) && is_half_op)
        || ((HAS_F32 == 0) && is_flt && !is_half_op)
        || ((MUL_UNITS == 0) && ictl[C_IMUL])
        || ((FSFU_UNITS == 0) && is_seed_op)
        || ((HAS_MASK == 0) && is_tmc)
        // THIS one is very bad and not-readable in original form
        // with explicit nested shape + separate the mask/ipdom and split/join condition
        // it is now readable and clear
        || (
            ((HAS_MASK == 0) || (HAS_IPDOM == 0)) 
            && (is_split || is_join)
        )
    );

    wire illegal = ictl[C_ILLEGAL] || unbuilt;

    // ================= the scalar file =====================================
    // Per WAVE: each wave has its own uniform values, exactly as GCN's SGPRs
    // are per wavefront.
    // 33 BITS: {zero, value}. The branch's zero test is STORED, not computed --
    // as a 32-bit reduce in front of the per-wave PC's clock enable it was the
    // 1,024-endpoint cone that held the PE at 182 MHz.
    reg [32:0] sfile [0:WAVES*32-1];

    // THE REGISTER IS IN FRONT OF THE ALU, NOT BEHIND IT.
    //
    // Read and ALU in one cycle is 11 levels, and the measured budget is nine:
    // reading one 512-entry distributed RAM out of a block RAM's output costs
    // 2.15 ns of a 3.34 ns period before the ALU starts.
    //
    //   cycle N    read sfile -> operands -> a1_q / a2_q / aop_q      (this)
    //   cycle N+1  ALU on REGISTERS -> sfile[aad_q] <= {zero, result}
    //
    // Behind the ALU instead, the same two stages leave the read AND the ALU in
    // one cone and only move the write. In front, each cone starts at a
    // register and neither is close to the limit.
    //
    // FORWARDING IS GONE, and that is the point rather than a cost: at distance
    // 1 the result does not exist yet in either arrangement, so this interlocks
    // instead -- the same rule the vector half already states. Distance 2 needs
    // nothing at all, because the file was written at the end of N+1. The
    // forward comparator it deletes was the late input to the L1 tag cone.
    reg [31:0]      a1_q, a2_q, afast_q;
    reg [3:0]       aop_q;
    reg             ause_q, aval_q;
    reg [SAW_S-1:0] aad_q;

    wire [SAW_S-1:0] sa1 = (WAVES > 1) ? {f2_wave, rs1} : {rs1};
    wire [SAW_S-1:0] sa2 = (WAVES > 1) ? {f2_wave, rs2} : {rs2};
    wire [SAW_S-1:0] sad = (WAVES > 1) ? {f2_wave, rd}  : {rd};

    wire [32:0] sq1 = sfile[sa1];
    wire [31:0] srd1 = sq1[31:0];
    wire [31:0] srd2 = sfile[sa2][31:0];
    wire        srz1 = sq1[32];

    // s0 READS ZERO, like x0. `s_wen` already refuses to write it, so without
    // this it is simply never initialised -- X in RTL against the model's 0,
    // and every shader that builds a constant from s0 diverges silently.
    wire [31:0] sv1 = (rs1 == 5'd0) ? 32'd0 : srd1;
    wire [31:0] sv2 = (rs2 == 5'd0) ? 32'd0 : srd2;

    // ONLY a real scalar read stalls. An ordinary RV32I opcode has its operands
    // in the VECTOR file, so comparing its rs1/rs2 here would stall it against a
    // scalar write it never reads -- on most instructions, constantly.
    assign s_hz = (
        aval_q
        && f2_valid
        // IN some disgust situation, this kind of operation may be needed
        // but best practice is you do assign on some temporary wire for each component to avoid this shape
        && (
            (
                ictl[C_RDS1]
                && (rs1 != 5'd0)
                && (aad_q == sa1)
            ) || (
                ictl[C_RDS2]
                && (rs2 != 5'd0)
                && (aad_q == sa2)
            )
        )
    );

    // ONE SCALAR ALU. custom-2 and custom-3 used to build a case statement each
    // and mux between them -- two adders, two shifters, and an extra mux level
    // after the slowest thing in the cone. The operation is predecoded onto one
    // encoding, so this is a single datapath with a muxed operand.
    wire [3:0]  s_op   = ictl[C_SOP0+3:C_SOP0];
    wire [31:0] s_op2  = is_simm ? imm_i : sv2;
    wire [31:0] s_fast = is_rdctl ? ctl_rd : vs_scalar;
    wire       use_alu = is_salu || is_simm;

    // FOUR CLASSES, NOT ONE CASE. Written as a single case statement the tool
    // folded the shifter into the ADDER's carry chain: the shift amount sat in
    // front of all 32 bits of carry it has nothing to do with.
    (* max_fanout = 32 *) wire [4:0] a_sh = a2_q[4:0];
    wire [31:0] s_add = (aop_q == A_SUB) ? (a1_q - a2_q) : (a1_q + a2_q);
    wire        s_lt  = ($signed(a1_q) < $signed(a2_q));
    wire        s_ltu = (a1_q < a2_q);

    // THE ONLY begin-end "justification situation" is case + one one stmt
    // (only when each stmt is indeed simple enough)
    // BUT, it shoule have one more indent level inside case
    reg [31:0] s_shf;
    always @(*) begin
        case (aop_q)
            A_SLL:   s_shf = a1_q << a_sh;
            A_SRA:   s_shf = $signed(a1_q) >>> a_sh;
            default: s_shf = a1_q >> a_sh;
        endcase
    end

    reg [31:0] s_log;
    always @(*) begin
        case (aop_q)
            A_XOR:   s_log = a1_q ^ a2_q;
            A_OR:    s_log = a1_q | a2_q;
            default: s_log = a1_q & a2_q;
        endcase
    end

    // FIVE WAYS IN TWO STEPS. Collapsing SLT/SLTU to make it four, and folding
    // `s_fast` in to make one five-way, each LOST 9.4 MHz: Vivado maps THIS form
    // as LUT6 + MUXF7, and a MUXF7 is dedicated silicon at 0.067 ns where
    // another routed LUT level is 0.22.
    reg [31:0] salu_r;
    always @(*) begin
        case (aop_q)
            A_SLL, A_SRL, A_SRA: salu_r = s_shf;
            A_SLT:               salu_r = {31'd0, s_lt};
            A_SLTU:              salu_r = {31'd0, s_ltu};
            A_XOR, A_OR, A_AND:  salu_r = s_log;
            default:             salu_r = s_add;
        endcase
    end

    wire [31:0] sres = ause_q ? salu_r : afast_q;

// -----------------------------------------------------------------------------
// Below this line the formatting is Claude's, applied from the rules the half
// above demonstrates.
// -----------------------------------------------------------------------------

    wire s_wen = (
        ictl[C_SWEN]
        && (rd != 5'd0)
        && f2_valid
        && !hold
        && (hst == H_RUN)
    );

    always @(posedge clk) begin
        if (!resetn) begin
            aval_q <= 1'b0;
        end else begin
            aval_q  <= s_wen;
            aad_q   <= sad;
            a1_q    <= sv1;
            a2_q    <= s_op2;
            aop_q   <= s_op;
            ause_q  <= use_alu;
            afast_q <= s_fast;
        end
        if (aval_q) begin
            sfile[aad_q] <= {(sres == 32'd0), sres};
        end
    end

    // ================= the control slots ===================================
    always @(*) begin
        case (rs2)
            5'd0:    ctl_rd = arg;
            5'd1:    ctl_rd = {24'd0, coreid};
            5'd2:    ctl_rd = cyc_q;
            5'd3:    ctl_rd = ret_q;
            5'd4:    ctl_rd = {16'd0, wr_out};
            5'd5:    ctl_rd = {{(32-WID){1'b0}}, f2_wave};
            default: ctl_rd = 32'd0;
        endcase
    end

    // ================= the per-thread half =================================
    wire [31:0]      vt_wdata;
    wire [LANES-1:0] mask;
    wire             ip_fault;
    wire [7:0]       ip_hi;

    wire [31:0] vt_imm = (
        is_lui     ? imm_u
        : is_auipc ? (f2_pc + imm_u)
        : is_jal   ? (f2_pc + 32'd4)
        : is_jalr  ? (f2_pc + 32'd4)
        : is_store ? imm_s
        : imm_i
    );

    kht_unit #(
        .LANES      (LANES),
        .WAVES      (WAVES),
        .WID        (WID),
        .HAS_MASK   (HAS_MASK),
        .HAS_IPDOM  (HAS_IPDOM),
        .HAS_SHFL   (HAS_SHFL),
        .SHFL_UNITS (SHFL_UNITS),
        .FLANES     (FLANES),
        .FSFU_UNITS (FSFU_UNITS),
        .MUL_UNITS  (MUL_UNITS),
        .HAS_F16    (HAS_F16),
        .HAS_F32    (HAS_F32),
        .FMODEL     (FMODEL),
        .IPDOM_D    (IPDOM_D),
        .VREG_PRIM  (VREG_PRIM)
    ) u_vt (
        .clk    (clk),
        .resetn (resetn),
        // The REST of the machine's stall, never the combined one.
        .x_valid (f2_valid && !lsu_busy),
        .x_wave  (f2_wave),
        .x_hold  (base_hold),
        // EVERY CORE-LEVEL HOLD THAT IS NOT `base_hold` MUST DEFER THE COMMIT.
        // kht_unit's `go` deliberately excludes this core's `hold`, so a held
        // instruction is re-committed on every cycle of the hold. That is
        // idempotent for an integer write and CATASTROPHIC for a float: the
        // held vfmul re-launched the lane array every cycle, `f_soon` never
        // cleared, and the whole machine wedged with 16 waves runnable.
        // `s2v` and `shflxor` are the same class one step milder -- they would
        // write from the stale read the stall exists to avoid.
        .x_defer    (lsu_need || s_hz || f_soon),
        .stall      (vt_stall),
        .x_flt      (is_flt),
        .x_fop      (ictl[C_FOP0+3:C_FOP0]),
        .x_imul     (is_imul),
        .x_mop      (ictl[C_MOP0+1:C_MOP0]),
        .fpend      (fpend),
        .f_soon     (f_soon),
        .fpass_hold (fpass_hold),
        // A vmem STORE's data register rides in the `rd` field -- there is no
        // fourth register field for it -- so read port 1 serves it, exactly as
        // the SIMD tier's `vst` does.
        .x_alu      (alu_op),
        .x_rd       (rd),
        .x_rs1      (rs1),
        .x_rs1b     (rd),
        .x_rs1_sel  (mem_store),
        .x_rs2      (rs2),
        .x_wen      (vt_wen),
        .x_op2_imm  (vt_op2imm),
        .x_imm      (is_s2v ? sv1 : vt_imm),
        .x_laneid   (is_lanid),
        .x_smov     (is_s2v),
        // shflxor takes its xor mask from a SCALAR register; bcast takes its
        // source lane from the same field as an immediate.
        .x_shfl     (is_shfl || is_bcast),
        .x_sh_bcast (is_bcast),
        .x_sh_idx   (is_bcast ? rs2[LNW-1:0] : sv2[LNW-1:0]),
        // A load's data is gathered lane by lane into ld_buf while the
        // instruction is held; it is presented on the cycle the hold drops,
        // which is the cycle kht_unit captures its control.
        .x_ld_sel   (is_load || (is_vmem && !mem_store)),
        .x_ld_data  (ld_buf),
        .x_split    (is_split && f2_valid && !warm_stall),
        // `x_defer` does not reach split/join/tmc -- they are gated by `go`
        // inside the unit -- so `tmc`, which reads a scalar, needs its own.
        .x_join     (is_join && f2_valid),
        .x_tmc      (is_tmc && f2_valid && !s_hz),
        .x_tmc_mask (sv1[LANES-1:0]),

        .mask_o     (mask),
        .ipdom_hi   (ip_hi),
        .fault_o    (ip_fault),
        .rd1_o      (vt_rd1),
        .rd2_o      (vt_rd2),

        .dbg_wr_valid (vt_wr_v),
        .dbg_wr_rd    (vt_wr_rd),
        .dbg_wr_mask  (vt_wr_mask),
        .dbg_wr_data  (vt_wdata)
    );

    // A COMMITTED COPY OF a0 IN LANE 0, which is what ecall reports. Reading it
    // from the ecall's own rs1 would report x0: SYSTEM has no source operand,
    // and the halt word is a value the program left behind rather than one it
    // hands over. Lane 0 only, and only when lane 0 was active, because that is
    // exactly what the golden model records.
    always @(posedge clk) begin
        if (!resetn) begin
            a0_q <= 32'd0;
        end else if (vt_wr_v && (vt_wr_rd == 5'd10) && vt_wr_mask[0]) begin
            a0_q <= vt_wdata;
        end
    end

    // ---- the cross-lane reductions, and the one v->s selector -------------
    // A TREE, NOT A CHAIN. As a sequential loop this is LANES chained 32-bit
    // operations, and OOC synthesis of kht_core measured what that costs: 44
    // logic levels, 9 CARRY8, and the whole core at 71.7 MHz against kht_unit's
    // 324. The unit-only ladder could never see it -- the reductions live here.
    // LANES is a power of two throughout (the LDS banks and the butterfly both
    // assume it), which is what makes the tree exact.
    //
    // THE IDENTITY PER OPERATION. An inactive lane contributes it, so min must
    // start at the largest signed value and max at the smallest -- starting
    // both at zero clamps every result against 0, which is invisible whenever
    // the data happens to straddle it.
    wire red_add = (f7 == KHT_SUB_REDUXADD);
    wire red_max = (f7 == KHT_SUB_REDUXMAX);
    wire red_min = (f7 == KHT_SUB_REDUXMIN);
    wire red_and = (f7 == KHT_SUB_REDUXAND);
    wire red_mm  = red_max || red_min;

    wire [31:0] red_ident = (
        red_and   ? {32{1'b1}}
        : red_min ? 32'h7FFF_FFFF
        : red_max ? 32'h8000_0000
        : 32'd0
    );

    // ONE COMPARE A NODE: max and min differ only in which side it picks. As
    // five case arms the tool built two comparators and a five-way mux a node,
    // and the tree measured 1,290 LUT.
    function [31:0] rnode;
        input [31:0] a;
        input [31:0] b;
        reg         pick_a;
        reg [31:0]  mm, lg;
        begin
            pick_a = ($signed(a) < $signed(b)) ^ red_max;
            mm     = pick_a ? a : b;
            lg     = red_and ? (a & b) : (a | b);
            rnode  = red_add ? (a + b) : (red_mm ? mm : lg);
        end
    endfunction

    // AND THE ARITHMETIC TREE IS PIPELINED. As pure combinational logic the
    // tree became the binding path itself -- 21 levels, 6 CARRY8, 154 MHz -- so
    // each level is registered and a reduction is held LNW extra cycles. That
    // is architecturally free: `redux` is a rare instruction and it already
    // stalls for its operand, so the cost is cycles nobody spends in a loop.
    //
    // Only the ARITHMETIC tree needs it. `vreadfirst` is a tree of 32-bit
    // MUXES and `ballot` is one OR per lane; neither is deep.
    // THE LEAVES ARE REGISTERED TOO, for the same reason the levels are: they
    // take `vt_rd1` straight off the vector register file's block RAM, so the
    // first `rnode` adder started 0.845 ns into a 2.5 ns period. One more cycle
    // on an instruction that already stalls for its operand.
    wire [VW-1:0]    rleaf_c;
    reg  [VW-1:0]    rleaf_q;
    wire [VW-1:0]    rin  [0:LNW];
    reg  [VW-1:0]    rq   [1:LNW];
    wire [VW-1:0]    flvl [0:LNW];
    wire [LANES-1:0] fvld [0:LNW];
    wire [LANES-1:0] bal_r;

    genvar RI, RK;
    generate
    for (RI = 0; RI < LANES; RI = RI + 1) begin : g_rleaf
        assign rleaf_c[32*RI +: 32] = mask[RI] ? vt_rd1[32*RI +: 32] : red_ident;
        assign flvl[0][32*RI +: 32] = vt_rd1[32*RI +: 32];
        assign fvld[0][RI]          = mask[RI];
        assign bal_r[RI]            = mask[RI] && (|vt_rd1[32*RI +: 32]);
    end
    for (RK = 0; RK < LNW; RK = RK + 1) begin : g_rlvl
        for (RI = 0; RI < LANES; RI = RI + 1) begin : g_rn
            if (RI < (LANES >> (RK + 1))) begin : g_live
                wire [31:0] nd = rnode(
                    rin[RK][32*(2*RI)   +: 32],
                    rin[RK][32*(2*RI+1) +: 32]
                );
                always @(posedge clk) begin
                    rq[RK+1][32*RI +: 32] <= nd;
                end
                // vreadfirst prefers the LEFT subtree, so the lowest ACTIVE
                // lane wins by construction rather than by ordering a loop.
                assign fvld[RK+1][RI] = fvld[RK][2*RI] | fvld[RK][2*RI+1];
                assign flvl[RK+1][32*RI +: 32] = (
                    fvld[RK][2*RI]
                    ? flvl[RK][32*(2*RI)   +: 32]
                    : flvl[RK][32*(2*RI+1) +: 32]
                );
            end else begin : g_dead
                always @(posedge clk) begin
                    rq[RK+1][32*RI +: 32] <= 32'd0;
                end
                assign flvl[RK+1][32*RI +: 32] = 32'd0;
                assign fvld[RK+1][RI]          = 1'b0;
            end
        end
        assign rin[RK+1] = rq[RK+1];
    end
    endgenerate

    always @(posedge clk) begin
        rleaf_q <= rleaf_c;
    end
    assign rin[0] = rleaf_q;

    assign vs_scalar = (
        is_ballot  ? {{(32-LANES){1'b0}}, bal_r}
        : is_vrf   ? flvl[LNW][31:0]
        : rin[LNW][31:0]
    );

    // ================= the LSU, lane-serialising ===========================
    // THREE PHASES PER LANE: 0 computes and REGISTERS the address, 1 issues the
    // request from that register, 2 takes the word.
    //
    // Phase 0 exists for timing and it is the assembled PE's binding path that
    // bought it: `ea` combinational into `l1_addr` put the 32-bit address adder,
    // rv_l1's tag compare and `stall` in ONE cycle, and `stall` feeds `hold`,
    // which gates every register in the core. rv_l1 already separates
    // `probe_addr` (EX, starts the tag read) from `addr` (MEM, does the
    // compare) -- wiring both to the same wire threw that away.
    //
    // Phase 2 exists for correctness: `l1_rdata` is the data for the access one
    // cycle earlier, and rv_l1 drops it the moment a NEW request misses, so the
    // word must be taken on a cycle with no request in flight. The coalescer
    // replaces the walk entirely.
    reg  [LNW-1:0] ln_q;
    reg  [1:0]     ph_q;
    reg  [31:0]    ea_r;
    reg  [2:0]     rgn_r;
    // REGISTERED IN PHASE 0 WITH THE ADDRESS. `mask` is a 16:1 mux over the
    // per-wave mask array, so a combinational `lane_on` put that mux, `l1_req`,
    // rv_l1's `stall` and `hold` in one cone -- the binding path at 337.6 MHz.
    // `ln_q` and `mask` are both stable across a lane's three phases.
    reg            lon_r;
    reg  [31:0]    wd_r;
    reg            lsu_run, lsu_done, lsu_drain, lds_wait;
    reg  [VW-1:0]  ld_buf;
    // `lsu_done` is what stops the walk restarting. Without it the finished
    // walk drops `hold`, the same instruction is still in f2, `per_lane` is
    // still true, and it starts again -- a load that never retires and a PC
    // that never advances.
    assign lsu_busy = lsu_run || lsu_drain || lds_wait;

    // EVERY LANE'S ADDRESS AT ONCE, AND REGISTERED. Indexing `vt_rd1`/`vt_rd2`
    // per lane made `ea` start at the VECTOR register file's block RAM -- 0.845
    // ns of a 2.5 ns period spent before the adder, and the binding cone into
    // rv_l1's tag, the region decode and the sticky fault all at once. From a
    // flop the walk indexes 8 registered addresses instead.
    //
    // The instruction is HELD for the whole walk, so these are stable and only
    // `ln_q` moves. G4 already built exactly these adders for its resolver, so
    // with the banked LDS on this is shared rather than added.
    wire [VW-1:0] ea_all_c;
    reg  [VW-1:0] ea_all_q;
    // TWO ADDERS A LANE AND A MUX BEHIND THEM, and that is the cheaper form:
    // selecting the OPERANDS instead, so one chain a lane serves both tiers,
    // measured +41 LUT. The tool already shares what there is to share.
    genvar EG;
    generate
    for (EG = 0; EG < LANES; EG = EG + 1) begin : g_ea
        wire [31:0] off_g = (
            mem_lin ? {{(32-LNW){1'b0}}, EG[LNW-1:0]} : vt_rd2[32*EG +: 32]
        );
        assign ea_all_c[32*EG +: 32] = (
            is_vmem
            ? (sv1 + (off_g << mem_scale))
            : (vt_rd1[32*EG +: 32] + (is_store ? imm_s : imm_i))
        );
    end
    endgenerate

    always @(posedge clk) begin
        ea_all_q <= ea_all_c;
    end

    wire [31:0] ea = ea_all_q[32*ln_q +: 32];

    // THE STORE DATA, SELECTED ONCE. Written out in both the banked path and
    // the walk, the tool builds the wide select AND a second pair of LANES:1
    // muxes -- the same duplication that cost 238 LUT on the writeback probe.
    wire [VW-1:0] st_all;
    genvar SG;
    generate
    for (SG = 0; SG < LANES; SG = SG + 1) begin : g_stsel
        assign st_all[32*SG +: 32] = (
            mem_store ? vt_rd1[32*SG +: 32] : vt_rd2[32*SG +: 32]
        );
    end
    endgenerate

    wire lane_on = mask[ln_q];
    assign per_lane = ictl[C_PERLANE] && f2_valid;
    // Split so `hold` can take the shallow half: it ORs `warm_stall` anyway, so
    // `A || (B && !A)` collapses and the walk's own gate stays off that cone.
    assign lsu_want = per_lane && !lsu_done && !lsu_busy;
    // `!s_hz` too: the walk latches its base from `sv1`, and starting it during
    // the scalar interlock would latch the value the interlock exists to wait
    // for. `hold` ORs `s_hz` separately, so this stays off that cone.
    assign lsu_need = lsu_want && !warm_stall && !s_hz;

    // ---- the region decoder, the one this PE has -------------------------
    function [2:0] region_of;
        input [31:0] a;
        begin
            if (a[31]) begin
                region_of = R_DRAM;
            end else begin
                case (a[30:28])
                    3'd1:    region_of = R_SPAD;
                    3'd2:    region_of = R_CTL;
                    3'd4:    region_of = R_LDS;
                    default: region_of = R_BAD;
                endcase
            end
        end
    endfunction

    wire [2:0] rgn = region_of(ea);

    // ---- G4: the banked LDS path -----------------------------------------
    // Every active lane's address at once, which the serial walk never needs
    // and the resolver cannot work without. Elaborated only when the gate is on,
    // so HAS_LDSBANK = 0 keeps exactly one address adder.
    // Sampled only once the vector read has caught up: before that `ea_g` is
    // built from the PREVIOUS instruction's operands, which is what made the
    // region fault latch on every gather.
    // TWO STAGES, and it costs nothing: a per-lane access already waits two
    // cycles for `warm_q`, so the per-lane region bits are registered in the
    // first and reduced in the second. As one cycle it was LANES address adders
    // and LANES region decodes AND-reduced -- 12 levels for a single endpoint.
    wire [LANES-1:0] lds_ok;
    reg  [LANES-1:0] lds_ok_q;
    reg  all_lds_q;
    always @(posedge clk) begin
        if (!resetn) begin
            lds_ok_q  <= {LANES{1'b0}};
            all_lds_q <= 1'b0;
        end else begin
            lds_ok_q  <= lds_ok & {LANES{(warm_q != 4'd0)}};
            all_lds_q <= (
                go ? 1'b0
                : (per_lane && (&lds_ok_q) && (mask != {LANES{1'b0}}))
            );
        end
    end

    generate
    if (HAS_LDSBANK == 0) begin : g_nolds
        assign lds_ok    = {LANES{1'b0}};
        assign lds_req   = 1'b0;
        assign lds_we    = 1'b0;
        assign lds_mask  = {LANES{1'b0}};
        assign lds_addr  = {(LANES*SAW){1'b0}};
        assign lds_wdata = {(LANES*32){1'b0}};
    end else begin : g_ldsb
        // `ea_all_q` IS the registration this used to do for itself: the
        // resolver ranks LANES addresses against each other, so leaving them
        // combinational put the adders AND the ranking between the instruction
        // window and kht_lds' own state.
        reg  [LANES-1:0]     lds_mask_q;
        always @(posedge clk) begin
            lds_mask_q <= mask;
        end
        assign lds_mask = lds_mask_q;

        genvar G;
        for (G = 0; G < LANES; G = G + 1) begin : g_lane
            assign lds_addr[SAW*G +: SAW] = ea_all_q[32*G + 2 +: SAW];
            assign lds_wdata[32*G +: 32]  = st_all[32*G +: 32];
            // An inactive lane must not veto the path, so it reads as agreeing.
            assign lds_ok[G] = (
                !mask[G] || (region_of(ea_all_q[32*G +: 32]) == R_LDS)
            );
        end
        assign lds_we   = mem_store || is_store;
        // From the REGISTERED decision. `all_lds` is LANES address adders and
        // LANES region decodes AND-reduced; combinationally into the walk's
        // start it was the assembled PE's binding path.
        assign lds_req  = lsu_need && all_lds_q && !l1_stall && !vt_stall;
    end
    endgenerate

    // COMBINATIONAL, and that is what it is for: `probe_addr` only addresses
    // rv_l1's tag RAM, so the adder ends at a memory input rather than at a
    // comparator, and the tag is read a cycle before the compare needs it.
    assign l1_probe = ea;
    // REGISTERED. The compare and `stall` start from a flop, not from the adder.
    assign l1_req   = lsu_run && (ph_q == 2'd1) && lon_r && (rgn_r == R_DRAM);
    assign l1_we    = mem_store || is_store;
    assign l1_be    = 4'hF;
    assign l1_addr  = ea_r;
    // An RV32I store's data is rs2 and its base is rs1; a vmem store's data is
    // read on port 1 above and its base is scalar. Registered with the address,
    // so the vector file's block RAM is off this cone too.
    assign l1_wdata = wd_r;
    assign l1_flush = (hst == H_FLUSH);
    assign l1_inval = 1'b0;

    // A multi-line condition inside a ternary is the shape the rules call out, so
    // the condition gets its own named wire and the ternary stays one line.
    wire spad_st = (
        lsu_run
        && (ph_q == 2'd1)
        && lon_r
        && (rgn_r == R_LDS)
        && (mem_store || is_store)
    );

    assign spad_addr  = ea_r[SAW+1:2];
    assign spad_we    = spad_st ? 4'hF : 4'd0;
    assign spad_wdata = wd_r;

    reg [31:0] req_q, gat_q;
    wire lane_last = (ln_q == (LANES[LNW-1:0] - 1'b1));

    wire is_ld_any = ictl[C_LDANY];

    always @(posedge clk) begin
        if (!resetn) begin
            lsu_run   <= 1'b0;
            lsu_drain <= 1'b0;
            lsu_done  <= 1'b0;
            ph_q      <= 2'd0;
            lds_wait  <= 1'b0;
            ln_q      <= {LNW{1'b0}};
            req_q     <= 32'd0;
            gat_q     <= 32'd0;
        end else begin
            if (go) begin
                lsu_done <= 1'b0;
            end

            if (lsu_drain) begin
                lsu_drain <= 1'b0;
                lsu_done  <= 1'b1;
            end else if (lds_wait) begin
                if (lds_done) begin
                    lds_wait  <= 1'b0;
                    lsu_drain <= 1'b1;
                    if (is_ld_any) begin
                        ld_buf <= lds_rdata;
                    end
                end
            end else if (lsu_need && !l1_stall && !vt_stall) begin
                gat_q <= gat_q + 32'd1;
                // Every active lane at once, as many passes as the banks need.
                if (all_lds_q) begin
                    lds_wait <= 1'b1;
                end else begin
                    lsu_run <= 1'b1;
                    ph_q    <= 2'd0;
                    ln_q    <= {LNW{1'b0}};
                end
            end else if (lsu_run) begin
                case (ph_q)
                    // Compute and REGISTER this lane's address; the tag read
                    // starts from `l1_probe` on the same cycle.
                    2'd0: begin
                        ea_r  <= ea;
                        rgn_r <= rgn;
                        lon_r <= lane_on;
                        wd_r  <= st_all[32*ln_q +: 32];
                        ph_q  <= 2'd1;
                    end
                    // The access, from the registered address. A miss holds
                    // here, so when this phase ends the word exists.
                    2'd1: begin
                        if (!l1_stall) begin
                            if (lon_r) begin
                                req_q <= req_q + 32'd1;
                            end
                            // A store has nothing to collect.
                            if (is_ld_any) begin
                                ph_q <= 2'd2;
                            end else begin
                                ph_q <= 2'd0;
                                if (lane_last) begin
                                    lsu_run   <= 1'b0;
                                    lsu_drain <= 1'b1;
                                end
                                ln_q <= ln_q + 1'b1;
                            end
                        end
                    end
                    // Take the word on a cycle with NO request in flight, so no
                    // new miss can displace it.
                    default: begin
                        if (lon_r) begin
                            ld_buf[32*ln_q +: 32] <= (
                                (rgn_r == R_DRAM) ? l1_rdata : spad_rdata
                            );
                        end
                        ph_q <= 2'd0;
                        if (lane_last) begin
                            lsu_run   <= 1'b0;
                            lsu_drain <= 1'b1;
                        end
                        ln_q <= ln_q + 1'b1;
                    end
                endcase
            end
        end
    end

    // Passes and lane-walks are both "one access issued", so the witness stays
    // one number whichever path served the instruction.
    assign req_ctr    = req_q + lds_passes;
    assign gather_ctr = gat_q;

    // ================= halt, PC update, counters ===========================
    // A HALT FLUSHES BEFORE IT COMPLETES. The unit's completion is the host's
    // only sequencing point, and a write-back L1 holds a shader's stores in
    // dirty lines that no one else will ever push out -- so without this the
    // shader retires, the host reads DRAM, and finds it unchanged. rv_l1's
    // flush is not finished until every writeback has been ACKNOWLEDGED, which
    // is what makes the completion mean the data is in memory.
    reg        halt_q;
    reg [1:0]  cause_q;
    reg [31:0] hword_q;

    // THE REGION FAULT IS REGISTERED, and it costs no cycle. Computing it
    // combinationally put the 32-bit address adder and the region decode
    // between the instruction window and the halt FSM -- the assembled PE's
    // binding path at 18 levels and 4 CARRY8. Every per-lane instruction is
    // already held at least one cycle by `lsu_need`, so a registered check is
    // sampled on a cycle that exists anyway. Sticky until retire, which also
    // makes it STRICTER than the combinational form: that one only ever saw the
    // lane the walk happened to be on when `go` fired.
    reg  bad_q;
    // ONLY WHILE THE WALK IS PRESENTING A REAL ADDRESS. `ea` is built from the
    // registered vector read, so on an instruction's first EX cycle it still
    // holds the PREVIOUS instruction's operands and decodes as BAD. Sampling
    // that latched a fault on every gather.
    wire bad_now = lsu_run && (ph_q == 2'd0) && lane_on && (rgn == R_BAD);

    always @(posedge clk) begin
        if (!resetn) begin
            bad_q <= 1'b0;
        end else if (go) begin
            bad_q <= 1'b0;
        end else begin
            bad_q <= bad_q || bad_now;
        end
    end

    wire fault = illegal || ip_fault || bad_q;
    // A halted core retires nothing more: the instruction already in the window
    // behind an ecall would otherwise retire too and overwrite the cause and
    // the halt word with its own.
    assign go_nh = f2_valid && (hst == H_RUN);
    assign go  = go_nh && !hold;
    wire ecall = ictl[C_ECALL];
    wire ebrk  = ictl[C_EBRK];

    // A STORED BIT, NOT A 32-BIT COMPARE. Reading sv1 here put a 32-bit reduce
    // in front of the per-wave PC's clock enable -- the 512-endpoint, 9-level
    // cone that bound the PE at 267.7 MHz.
    wire sv1_zero = (rs1 == 5'd0) ? 1'b1 : srz1;
    wire sbr_take = is_sbeqz ? sv1_zero : !sv1_zero;

    // Every non-sequential PC the core has: an unconditional jump, an indirect
    // jump (wave-wide, so lane 0's value is the target by construction), and a
    // taken uniform branch. A per-thread conditional branch is not on the list
    // because the encoding refuses it.
    // A FLOAT REDIRECTS ITS OWN WAVE TO THE NEXT INSTRUCTION, and that is the
    // whole of how `fpend` is made to work. `fpend` blocks FETCH, but the front
    // end is three deep, so two instructions of that wave are already in flight
    // when the float issues -- measured as three dependent vfma launching in
    // consecutive cycles with stale addends. Treating the float as a redirect
    // kills exactly those two and rewinds `nxt` to the float's own pc+4, so the
    // wave resumes there once its result has landed. The machinery is the
    // branch's, unchanged.
    wire br_take = (
        is_jal
        || is_jalr
        || (is_sbr && sbr_take)
        || is_flt
        || is_imul
    );

    assign rdir_nh  = go_nh && br_take;
    assign kill_ev  = go_nh && (br_take || wave_ends);
    assign redir    = rdir_nh && !hold;
    // A MULTI-CYCLE UNIT REDIRECTS TO THE NEXT INSTRUCTION, not to a branch
    // target. Adding `is_imul` to `br_take` without adding it here sent every
    // multiply to `f2_pc + imm_i` -- an R-type's imm field is funct7|rs2, so
    // `mul x10, x6, x8` jumped forty bytes and skipped nine instructions.
    assign redir_pc = (
        (is_flt || is_imul) ? (f2_pc + 32'd4)
        : is_jal            ? (f2_pc + imm_j)
        : is_jalr           ? ((vt_rd1[31:0] + imm_i) & ~32'd1)
        : (f2_pc + imm_i)
    );

    // `lsu_busy` is a REGISTER, so on the cycle a walk is decided it is still
    // low. Without `lsu_need` in `hold` the instruction retires in that cycle,
    // the fetch advances, and the walk reads its width, its scale and its base
    // off the NEXT instruction -- a store to a nonsense address. It stays OUT of
    // `base_hold` because that one freezes kht_unit's MEM register.
    assign base_hold = l1_stall || lsu_busy;
    // `f_soon` reserves the vector file's write port for a float result landing
    // in two cycles: dropping `go` now empties writeback exactly then, so the
    // float takes the port by mux rather than by arbitration. One cycle per
    // float instruction against fifteen of latency.
    assign hold = (
        base_hold
        || vt_stall
        || warm_stall
        || lsu_want
        || s_hz
        || f_soon
        || fpass_hold
    );

    // A FAULT kills the whole unit; an ecall or ebreak retires ONE WAVE. The
    // difference matters once more than one is live: a shader is finished when
    // its last wave is, and a fault is a property of the program rather than of
    // the wave that happened to hit it.
    assign wave_ends = fault || ecall || ebrk;

    // Hold-free, for the same reason the PC update is: `hold` was reaching the
    // halt FSM's reset pin through `go` and then two more levels of next-state.
    wire fault_nh = go_nh && fault;
    wire ecl_nh   = go_nh && !fault && (ecall || ebrk);
    wire last_nh  = ((live & ~one_hot) == {WAVES{1'b0}});

    always @(posedge clk) begin
        if (!resetn) begin
            live    <= {WAVES{1'b0}};
            halt_q  <= 1'b0;
            hst     <= H_RUN;
            cause_q <= 2'd0;
            hword_q <= 32'd0;
            cyc_q   <= 32'd0;
            ret_q   <= 32'd0;
        end else begin
            // Not every arm is one simple statement, so the whole case takes
            // begin/end rather than mixing the two shapes.
            case (hst)
                H_FLUSH: begin
                    hst <= H_RISE;
                end
                H_RISE: begin
                    if (l1_flush_busy) begin
                        hst <= H_FALL;
                    end
                end
                H_FALL: begin
                    if (!l1_flush_busy) begin
                        hst    <= H_DONE;
                        halt_q <= 1'b1;
                    end
                end
                default: ;
            endcase

            if (boot_v) begin
                // `k_op` is the wave count, so op 1 is exactly today's single
                // wave and op N launches N. The flit already carries it.
                live   <= boot_mask;
                halt_q <= 1'b0;
                hst    <= H_RUN;
                cyc_q  <= 32'd0;
                ret_q  <= 32'd0;
            end else if (!hold) begin
                if (go_nh) begin
                    ret_q <= ret_q + 32'd1;
                end
                if (fault_nh) begin
                    live    <= {WAVES{1'b0}};
                    hst     <= H_FLUSH;
                    cause_q <= 2'd3;
                    hword_q <= f2_pc;
                end else if (ecl_nh) begin
                    live[f2_wave] <= 1'b0;
                    cause_q <= ebrk ? 2'd2 : 2'd1;
                    // The LAST wave to retire flushes and completes the unit.
                    if (last_nh) begin
                        hst <= H_FLUSH;
                    end
                end
            end
            if (|live) begin
                cyc_q <= cyc_q + 32'd1;
            end
        end
    end

    assign run          = |live;
    assign halted       = halt_q;
    assign cause        = cause_q;
    // The FAULT word only. ecall and ebreak report the committed a0, read when
    // the completion is built rather than latched at the halt: the instruction
    // before an ecall writes a0 one cycle after it retires, so a latch here
    // captures the value before it exists.
    assign halt_word    = (cause_q == 2'd3) ? hword_q : a0_q;
    // f1 TOO: with two fetches in flight, a completion built while f1 still
    // carries one reports a unit that has not finished executing.
    assign pipe_empty   = !f1_valid && !f2_valid && !lsu_busy;
    assign cycle_ctr    = cyc_q;
    assign instret_ctr  = ret_q;
    assign dbg_retire_pc    = f2_pc;
    assign dbg_retire_valid = go;
    assign dbg_mask         = mask;

endmodule

`default_nettype wire

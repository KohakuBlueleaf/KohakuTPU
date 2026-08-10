// Sixteen ALUs, the register file they read, and the four VMODE topologies.
//
// docs/compute/vector-core.md s7.1: a mode is a factorisation W x D = 16.
//
//   FLAT  16 x 1   16 results/cycle     D2  8 x 2   8/cycle
//   D4     4 x 4    4 results/cycle     TREE 8+4+2+1 + accumulator
//
// GROUP g STAGE t IS ALU g*D + t, and only stage 0 may read a vector register:
// a later stage runs 14*t cycles behind, so a V operand there would need a
// 24-bit delay line per stage. Every chained kernel in isa/vector.md s4 sources
// C, S or K past stage 0, so the rule costs nothing and the sequencer faults on
// it rather than reading the wrong element.
//
// THE DATAPATH NOW RELIES ON THAT RULE rather than merely permitting it: an ALU
// that is never a chain head in a mode has no register-file path in that mode.
// A stage-t>0 SRC_V, which vec_core rejects with F_VSRC, reads a wrong lane
// here instead of a wrong element -- both are wrong, and neither is reachable.
//
// SLOT ASSIGNMENT. Striping is by lane -- slice s holds elements s, s+16, ... --
// and a D-deep mode retires W per cycle, so group g at phase p drives slot
// p*W + g. Over D phases the 16 slices of a chunk are covered exactly once.

`default_nettype none

module vec_lanes #(
    parameter integer MODEL   = 1,
    parameter         RF_PRIM = "block"
)(
    input  wire         clk,
    input  wire         rst,
    input  wire [1:0]   mode,

    input  wire         ls_we,
    input  wire [6:0]   ls_waddr,
    input  wire [383:0] ls_wdata,
    input  wire [6:0]   ls_raddr,
    input  wire         ls_ractive,
    output wire [383:0] ls_rdata,

    input  wire         iss_valid,
    input  wire [1:0]   iss_phase,
    input  wire [6:0]   iss_ra,
    input  wire [6:0]   iss_rb,
    input  wire [6:0]   iss_rc,
    input  wire [6:0]   iss_wa,
    input  wire [1:0]   iss_pm,
    input  wire [1:0]   iss_pr,
    input  wire [2:0]   iss_chunk,
    input  wire         iss_is_cmp,
    input  wire         iss_tail,
    input  wire [15:0]  iss_tmask,   // VL tail: slots past VL must not write

    input  wire [19:0]  st_op,
    input  wire [7:0]   st_sa,
    input  wire [7:0]   st_sb,
    input  wire [7:0]   st_sc,
    input  wire [95:0]  st_ka,
    input  wire [95:0]  st_kb,
    input  wire [95:0]  st_kc,

    input  wire         red_init,
    input  wire [2:0]   red_kind,

    input  wire [1:0]   p_rd_sel,
    output wire [127:0] p_rd_bits,

    output wire [23:0]  red_result,
    output wire         red_valid,
    output wire         pipe_empty,

    // One pulse per retiring beat, naming the register it wrote, so a
    // sequencer can run a pending-write scoreboard against a 14-deep lane.
    output wire         wb_fire,
    output wire [3:0]   wb_vreg
);
    localparam [1:0] M_FLAT = 2'd0, M_D2 = 2'd1, M_D4 = 2'd2, M_TREE = 2'd3;
    localparam [2:0] R_MAX = 3'd1, R_MIN = 3'd2, R_SUMSQ = 3'd3, R_DOT = 3'd4,
                     R_EXPSUM = 3'd5;
    localparam [4:0] OP_ADD = 5'd3, OP_MUL = 5'd5, OP_MAX = 5'd8, OP_MIN = 5'd9,
                     OP_EXP2 = 5'd16;
    localparam [1:0] SRC_V = 2'd0, SRC_C = 2'd2;

    localparam [23:0] E8_ZERO = 24'h000000;
    localparam [23:0] E8_PINF = 24'h7F8000;
    localparam [23:0] E8_NINF = 24'hFF8000;

    // d_tail rides just BELOW d_valid so every field under it keeps its index;
    // d_valid stays at MW-1, which is what meta_any and tailv reach for.
    localparam integer MW = 51;

    // ---------------------------------------------------------- declarations
    wire [383:0] rf_a, rf_b, rf_c;
    wire [383:0] alu_out;
    wire [15:0]  alu_ovld, alu_pred;
    wire         d_tail_q;
    wire [15:0]  pmask_now;

    reg  [15:0]  rf_we;
    wire [383:0] rf_wdata;
    reg  [6:0]   rf_waddr;
    reg  [383:0] alu_a, alu_b, alu_c;
    reg  [79:0]  alu_op;
    reg  [15:0]  alu_iv;

    reg          d_valid, d_tail, d_is_cmp;
    reg  [1:0]   d_phase;
    reg  [2:0]   d_kind;
    reg  [1:0]   q_pr, q_pm;
    reg  [6:0]   q_wa;
    reg  [2:0]   q_chunk;
    reg  [15:0]  q_tmask;

    reg  [23:0]  acc [0:15];
    reg  [3:0]   acc_rd;
    wire [3:0]   acc_wr;

    reg  [MW-1:0] meta [1:56];
    reg  [15:0]   tailv;
    reg  [127:0]  preg [0:3];

    // TREE writes its leaves back 8 per phase, the same slot width as D2.
    wire [4:0] wid = (mode == M_FLAT) ? 5'd16
                   : (mode == M_D2) || (mode == M_TREE) ? 5'd8 : 5'd4;

    // ONE SET OF LOOP VARIABLES PER always BLOCK. Sharing module-level
    // integers between two `always @(*)` blocks is a simulation race: each
    // re-trigger clobbers the other's counters mid-iteration.
    integer oj;
    integer wg, wsl;
    integer pg;
    integer mi, mj;
    integer ai, pq;

    // ================================================== register file
    wire [6:0] rd_a_addr = ls_ractive ? ls_raddr : iss_ra;
    assign ls_rdata = rf_a;

    genvar s;
    generate
    for (s = 0; s < 16; s = s + 1) begin : g_rf
        vec_regfile #(.AW(7), .DW(24), .PRIM(RF_PRIM)) u_rf (
            .clk(clk),
            .wr_en(rf_we[s]), .wr_addr(rf_waddr), .wr_data(rf_wdata[s*24 +: 24]),
            .ra_addr(rd_a_addr), .rb_addr(iss_rb), .rc_addr(iss_rc),
            .ra_data(rf_a[s*24 +: 24]),
            .rb_data(rf_b[s*24 +: 24]),
            .rc_data(rf_c[s*24 +: 24])
        );
    end
    endgenerate

    generate
    for (s = 0; s < 16; s = s + 1) begin : g_alu
        vec_alu #(.MODEL(MODEL)) u_alu (
            .clk(clk), .rst(rst),
            .in_valid(alu_iv[s]), .op(alu_op[s*5 +: 5]),
            .a(alu_a[s*24 +: 24]), .b(alu_b[s*24 +: 24]), .c(alu_c[s*24 +: 24]),
            .out_valid(alu_ovld[s]), .out(alu_out[s*24 +: 24]),
            .out_pred(alu_pred[s])
        );
    end
    endgenerate

    // ================================================== issue -> ALU stage
    // READ_LAT is 1, so operands present this cycle belong to last cycle's
    // address, and everything that selects among them lags with it.
    always @(posedge clk) begin
        if (rst) begin
            d_valid <= 1'b0; d_phase <= 2'd0; d_tail <= 1'b0;
            d_is_cmp <= 1'b0; d_kind <= 3'd0;
            q_pr <= 2'd0; q_pm <= 2'd0; q_wa <= 7'd0; q_chunk <= 3'd0;
            q_tmask <= 16'd0;
        end else begin
            d_valid  <= iss_valid;
            d_phase  <= iss_phase;
            d_tail   <= iss_tail;
            d_is_cmp <= iss_is_cmp;
            d_kind   <= red_kind;
            q_pr     <= iss_pr;
            q_pm     <= iss_pm;
            q_wa     <= iss_wa;
            q_chunk  <= iss_chunk;
            q_tmask  <= iss_tmask;
        end
    end

    vec_delay #(.W(4), .D(14)) u_accd (.clk(clk), .d(acc_rd), .q(acc_wr));
    vec_delay #(.W(1), .D(56)) u_tq   (.clk(clk), .d(d_tail), .q(d_tail_q));

    wire [4:0] comb_op = (d_kind == R_MAX) ? OP_MAX
                       : (d_kind == R_MIN) ? OP_MIN : OP_ADD;
    // "half" means one element per leaf and two phases per chunk. EXPSUM is
    // half-rate for the same reason DOT is: the leaf consumes one slot, not two.
    wire       half    = (d_kind == R_SUMSQ) || (d_kind == R_DOT)
                      || (d_kind == R_EXPSUM);
    wire [4:0] l0_op   = d_tail                 ? comb_op
                       : (d_kind == R_EXPSUM)   ? OP_EXP2
                       : half                   ? OP_MUL : comb_op;

    // ================================================== phase window
    // A beat reads the CONTIGUOUS slices [ph*W, ph*W+W-1] and gives entry j to
    // every ALU of group j, so the phase select is the WINDOW's -- once -- not
    // each ALU's: entry j<4 reaches only {j,j+4,j+8,j+12}, j in 4..7 {j,j+8}.
    wire       w8 = (mode == M_D2) || (mode == M_TREE);
    wire [1:0] wk = (mode == M_D4) ? d_phase : {w8 & d_phase[0], 1'b0};

    wire [383:0] wva, wvb, wvc;
    generate
    for (s = 0; s < 16; s = s + 1) begin : g_win
        if (s < 4) begin : g_w4
            assign wva[s*24 +: 24] = (wk == 2'd0) ? rf_a[(s   )*24 +: 24]
                                  : (wk == 2'd1) ? rf_a[(s+ 4)*24 +: 24]
                                  : (wk == 2'd2) ? rf_a[(s+ 8)*24 +: 24]
                                                 : rf_a[(s+12)*24 +: 24];
            assign wvb[s*24 +: 24] = (wk == 2'd0) ? rf_b[(s   )*24 +: 24]
                                  : (wk == 2'd1) ? rf_b[(s+ 4)*24 +: 24]
                                  : (wk == 2'd2) ? rf_b[(s+ 8)*24 +: 24]
                                                 : rf_b[(s+12)*24 +: 24];
            assign wvc[s*24 +: 24] = (wk == 2'd0) ? rf_c[(s   )*24 +: 24]
                                  : (wk == 2'd1) ? rf_c[(s+ 4)*24 +: 24]
                                  : (wk == 2'd2) ? rf_c[(s+ 8)*24 +: 24]
                                                 : rf_c[(s+12)*24 +: 24];
        end else if (s < 8) begin : g_w2
            assign wva[s*24 +: 24] = wk[1] ? rf_a[(s+8)*24 +: 24] : rf_a[s*24 +: 24];
            assign wvb[s*24 +: 24] = wk[1] ? rf_b[(s+8)*24 +: 24] : rf_b[s*24 +: 24];
            assign wvc[s*24 +: 24] = wk[1] ? rf_c[(s+8)*24 +: 24] : rf_c[s*24 +: 24];
        end else begin : g_w1
            assign wva[s*24 +: 24] = rf_a[s*24 +: 24];
            assign wvb[s*24 +: 24] = rf_b[s*24 +: 24];
            assign wvc[s*24 +: 24] = rf_c[s*24 +: 24];
        end
    end
    endgenerate

    // ================================================== operand wiring
    wire [383:0] nx_a, nx_b, nx_c;
    wire [79:0]  nx_op;
    wire [15:0]  nx_iv;

    generate
    for (s = 0; s < 16; s = s + 1) begin : g_src
        // ALU s is group s/D stage s%D and D is 1, 2 or 4, so BOTH indices are
        // a per-mode constant. Deriving them at runtime instead cost 3,404 LUT.
        localparam integer G2 = s / 2, T2 = s % 2;
        localparam integer G4 = s / 4, T4 = s % 4;

        wire [1:0] sa = (mode == M_FLAT) ? st_sa[1:0]
                      : (mode == M_D2)   ? st_sa[T2*2 +: 2] : st_sa[T4*2 +: 2];
        wire [1:0] sb = (mode == M_FLAT) ? st_sb[1:0]
                      : (mode == M_D2)   ? st_sb[T2*2 +: 2] : st_sb[T4*2 +: 2];
        wire [1:0] sc = (mode == M_FLAT) ? st_sc[1:0]
                      : (mode == M_D2)   ? st_sc[T2*2 +: 2] : st_sc[T4*2 +: 2];

        // ONLY A CHAIN HEAD MAY SOURCE A VECTOR REGISTER (s8 above; vec_core
        // raises F_VSRC otherwise), so an ALU that is never a head in a mode
        // needs no window entry for it -- eight of the sixteen become wires.
        wire [23:0] va, vb, vc;
        if (T4 == 0) begin : g_v4
            assign va = (mode == M_FLAT) ? wva[s*24 +: 24]
                      : (mode == M_D2)   ? wva[G2*24 +: 24] : wva[G4*24 +: 24];
            assign vb = (mode == M_FLAT) ? wvb[s*24 +: 24]
                      : (mode == M_D2)   ? wvb[G2*24 +: 24] : wvb[G4*24 +: 24];
            assign vc = (mode == M_FLAT) ? wvc[s*24 +: 24]
                      : (mode == M_D2)   ? wvc[G2*24 +: 24] : wvc[G4*24 +: 24];
        end else if (T2 == 0) begin : g_v2
            assign va = (mode == M_FLAT) ? wva[s*24 +: 24] : wva[G2*24 +: 24];
            assign vb = (mode == M_FLAT) ? wvb[s*24 +: 24] : wvb[G2*24 +: 24];
            assign vc = (mode == M_FLAT) ? wvc[s*24 +: 24] : wvc[G2*24 +: 24];
        end else begin : g_v1
            assign va = wva[s*24 +: 24];
            assign vb = wvb[s*24 +: 24];
            assign vc = wvc[s*24 +: 24];
        end

        wire [23:0] ka = (mode == M_FLAT) ? st_ka[23:0]
                       : (mode == M_D2)   ? st_ka[T2*24 +: 24] : st_ka[T4*24 +: 24];
        wire [23:0] kb = (mode == M_FLAT) ? st_kb[23:0]
                       : (mode == M_D2)   ? st_kb[T2*24 +: 24] : st_kb[T4*24 +: 24];
        wire [23:0] kc = (mode == M_FLAT) ? st_kc[23:0]
                       : (mode == M_D2)   ? st_kc[T2*24 +: 24] : st_kc[T4*24 +: 24];

        // Stage 0 of a chain: takes the beat's valid and no chained operand.
        wire head = (mode == M_FLAT) ? 1'b1
                  : (mode == M_D2)   ? (T2 == 0) : (T4 == 0);

        wire [23:0] up;
        wire        upv;
        if (s == 0) begin : g_h0
            assign up = 24'd0;  assign upv = 1'b0;
        end else begin : g_hn
            assign up = alu_out[(s-1)*24 +: 24];  assign upv = alu_ovld[s-1];
        end
        wire [23:0] chain = head ? 24'd0 : up;

        assign nx_a[s*24 +: 24] = (sa == SRC_V) ? va : (sa == SRC_C) ? chain : ka;
        assign nx_b[s*24 +: 24] = (sb == SRC_V) ? vb : (sb == SRC_C) ? chain : kb;
        assign nx_c[s*24 +: 24] = (sc == SRC_V) ? vc : (sc == SRC_C) ? chain : kc;
        assign nx_op[s*5 +: 5]  = (mode == M_FLAT) ? st_op[4:0]
                                : (mode == M_D2)   ? st_op[T2*5 +: 5]
                                                   : st_op[T4*5 +: 5];
        assign nx_iv[s] = head ? d_valid : upv;
    end
    endgenerate

    // A sibling drives BOTH b and c, so one wiring serves every node op: ADD
    // reads a+c, MAX/MIN read a and b, MUL reads a*b.
    always @(*) begin
        alu_a  = nx_a;  alu_b = nx_b;  alu_c = nx_c;
        alu_op = nx_op; alu_iv = nx_iv;

        if (mode == M_TREE) begin
            for (oj = 0; oj < 8; oj = oj + 1) begin
                if (d_tail) begin
                    alu_a[oj*24 +: 24] = acc[2*oj];
                    alu_b[oj*24 +: 24] = acc[2*oj+1];
                    alu_c[oj*24 +: 24] = acc[2*oj+1];
                end else if (half) begin
                    alu_a[oj*24 +: 24] = wva[oj*24 +: 24];
                    alu_b[oj*24 +: 24] = wvb[oj*24 +: 24];
                    alu_c[oj*24 +: 24] = wvb[oj*24 +: 24];
                end else begin
                    alu_a[oj*24 +: 24] = rf_a[(2*oj)*24 +: 24];
                    alu_b[oj*24 +: 24] = rf_a[(2*oj+1)*24 +: 24];
                    alu_c[oj*24 +: 24] = rf_a[(2*oj+1)*24 +: 24];
                end
                alu_op[oj*5 +: 5] = l0_op;
                alu_iv[oj]        = d_valid;
            end
            for (oj = 0; oj < 7; oj = oj + 1) begin
                alu_a[(8+oj)*24 +: 24] = alu_out[(2*oj)*24   +: 24];
                alu_b[(8+oj)*24 +: 24] = alu_out[(2*oj+1)*24 +: 24];
                alu_c[(8+oj)*24 +: 24] = alu_out[(2*oj+1)*24 +: 24];
                alu_op[(8+oj)*5 +: 5]  = comb_op;
                alu_iv[8+oj]           = alu_ovld[2*oj];
            end
            alu_a[15*24 +: 24] = alu_out[14*24 +: 24];
            alu_b[15*24 +: 24] = acc[acc_rd];
            alu_c[15*24 +: 24] = acc[acc_rd];
            alu_op[15*5 +: 5]  = comb_op;
            alu_iv[15]         = alu_ovld[14] && !d_tail_q;
        end
    end

    // ================================================== metadata pipeline
    // Loaded beside the ALU input, so tap 14*D is valid the cycle that chain
    // retires: FLAT 14, D2 28, D4 and TREE 56.
    assign p_rd_bits = preg[p_rd_sel];
    assign pmask_now = preg[q_pr][q_chunk*16 +: 16];

    wire [MW-1:0] meta_in = {d_valid, d_tail, d_is_cmp, q_pr, q_wa, d_phase,
                             pmask_now, q_pm, q_chunk, q_tmask};

    always @(posedge clk) begin
        if (rst) begin
            for (mi = 1; mi <= 56; mi = mi + 1) meta[mi] <= {MW{1'b0}};
            tailv <= 16'd0;
        end else begin
            meta[1] <= meta_in;
            for (mi = 2; mi <= 56; mi = mi + 1) meta[mi] <= meta[mi-1];
            tailv <= {tailv[14:0], meta[56][MW-1]};
        end
    end

    // TREE taps the LEAF latency, not the tree's: a leaf retires at 14 exactly
    // like FLAT, and the tree's own result never travels this path -- it leaves
    // on red_result, gated by alu_ovld[14] and d_tail_q.
    wire          red_wb = (red_kind == R_EXPSUM);
    wire [MW-1:0] wb = (mode == M_FLAT) || (mode == M_TREE) ? meta[14]
                     : (mode == M_D2)   ? meta[28] : meta[56];

    // The ENABLE decode is taken one stage early and registered. `mode` is a
    // register in vec_core, and mode -> width -> slice index -> a 16-way
    // enable decode -> the register file's write port is the longest path in
    // the assembled machine (229 MHz). Measured standalone it hides, because
    // there `mode` is an input with an ideal driver.
    wire [MW-1:0] wbp = (mode == M_FLAT) || (mode == M_TREE) ? meta[13]
                      : (mode == M_D2)   ? meta[27] : meta[55];

    wire        p_valid = wbp[MW-1];
    wire        p_tail  = wbp[49];
    wire        p_cmp   = wbp[48];
    wire [6:0]  p_wa    = wbp[45:39];
    wire [1:0]  p_ph    = wbp[38:37];
    wire [15:0] p_pmask = wbp[36:21];
    wire [1:0]  p_pm    = wbp[20:19];
    wire [15:0] p_tmask = wbp[15:0];

    wire        wb_valid = wb[MW-1];
    wire        wb_tail  = wb[49];
    wire        wb_cmp   = wb[48];
    wire [1:0]  wb_pr    = wb[47:46];
    wire [6:0]  wb_wa    = wb[45:39];
    wire [1:0]  wb_ph    = wb[38:37];
    wire [15:0] wb_pmask = wb[36:21];
    wire [1:0]  wb_pm    = wb[20:19];
    wire [2:0]  wb_chunk = wb[18:16];
    wire [15:0] wb_tmask = wb[15:0];

    // ================================================== write-back
    // Group g's result leaves ALU g*D + D-1 and lands on slice wb_ph*W + g.
    wire [4:0] wwid = wid;

    reg [15:0] nx_we;
    reg [6:0]  nx_wa;
    reg [1:0]  md_r;
    reg        use_ls;

    // Decoded one stage early and registered: `alu_out` is only valid at the
    // retire cycle, but what selects among its slices need not be.
    always @(*) begin
        nx_we  = 16'd0;
        nx_wa  = p_wa;
        if (ls_we) begin
            nx_we = 16'hFFFF;
            nx_wa = ls_waddr;
        end else if (p_valid && !p_cmp && !p_tail
                     && ((mode != M_TREE) || red_wb)) begin
            for (wg = 0; wg < 16; wg = wg + 1) begin
                if (wg < wwid) begin
                    wsl = p_ph * wwid + wg;
                    nx_we[wsl] = ((p_pm == 2'd0) ? 1'b1
                               :  (p_pm == 2'd1) ? p_pmask[wsl]
                                                 : ~p_pmask[wsl]) & p_tmask[wsl];
                end
            end
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            rf_we <= 16'd0; rf_waddr <= 7'd0; use_ls <= 1'b0; md_r <= M_FLAT;
        end else begin
            rf_we    <= nx_we;
            rf_waddr <= nx_wa;
            md_r     <= mode;
            use_ls   <= ls_we;
        end
    end

    // Slice d's producer is a function of the MODE ALONE, so this is a 3:1
    // dressed as a 16:1: carrying a runtime ALU index here cost 1,089 LUT.
    generate
    for (s = 0; s < 16; s = s + 1) begin : g_wsel
        localparam integer P_D2 = (s % 8) * 2 + 1;
        localparam integer P_D4 = (s % 4) * 4 + 3;
        // TREE writes back its LEAVES, and leaf s mod 8 fed slice s. Below 8
        // that is the FLAT wire already, so only the top eight cost anything.
        localparam integer P_TR = s % 8;
        assign rf_wdata[s*24 +: 24] =
            use_ls              ? ls_wdata[s*24 +: 24]
          : (md_r == M_FLAT)    ? alu_out[s*24 +: 24]
          : (md_r == M_D2)      ? alu_out[P_D2*24 +: 24]
          : (md_r == M_TREE)    ? alu_out[P_TR*24 +: 24]
                                : alu_out[P_D4*24 +: 24];
    end
    endgenerate

    // Indexed BY SLICE, not by group: the same permutation g_wsel uses, so the
    // comparator source and the phase test are both mode constants.
    wire [15:0] pred_sl, pred_inph;
    generate
    for (s = 0; s < 16; s = s + 1) begin : g_psel
        localparam integer Q_D2 = (s % 8) * 2 + 1;
        localparam integer Q_D4 = (s % 4) * 4 + 3;
        localparam [1:0]   H_D2 = s / 8;
        localparam [1:0]   H_D4 = s / 4;
        assign pred_sl[s]   = (mode == M_FLAT) ? alu_pred[s]
                            : (mode == M_D2)   ? alu_pred[Q_D2] : alu_pred[Q_D4];
        assign pred_inph[s] = (mode == M_FLAT) ? 1'b1
                            : (mode == M_D2)   ? (wb_ph == H_D2) : (wb_ph == H_D4);
    end
    endgenerate

    always @(posedge clk) begin
        if (rst) begin
            for (pq = 0; pq < 4; pq = pq + 1) preg[pq] <= 128'd0;
        end else if (wb_valid && (mode != M_TREE) && wb_cmp) begin
            for (pg = 0; pg < 16; pg = pg + 1)
                if (pred_inph[pg] && wb_tmask[pg])
                    preg[wb_pr][wb_chunk*16 + pg] <= pred_sl[pg];
        end
    end

    // ================================================== reduction state
    // s7.3: a 14-deep ALU turns acc = acc + x into a 14-cycle recurrence, so a
    // single accumulator would run at II=14. Sixteen rotating partials break it.
    wire [23:0] ident = (red_kind == R_MAX) ? E8_NINF
                      : (red_kind == R_MIN) ? E8_PINF : E8_ZERO;

    always @(posedge clk) begin
        if (rst) begin
            for (ai = 0; ai < 16; ai = ai + 1) acc[ai] <= E8_ZERO;
            acc_rd <= 4'd0;
        end else if (red_init) begin
            for (ai = 0; ai < 16; ai = ai + 1) acc[ai] <= ident;
            acc_rd <= 4'd0;
        end else begin
            if (alu_ovld[14] && !d_tail_q) acc_rd <= acc_rd + 4'd1;
            if (alu_ovld[15]) acc[acc_wr] <= alu_out[15*24 +: 24];
        end
    end

    assign wb_fire = wb_valid && !wb_cmp && !wb_tail
                  && ((mode != M_TREE) || red_wb);
    assign wb_vreg = wb_wa[6:3];

    assign red_result = alu_out[14*24 +: 24];
    assign red_valid  = (mode == M_TREE) && alu_ovld[14] && d_tail_q;

    // Empty only when nothing is left anywhere: the metadata line covers the
    // chain, `tailv` the accumulator's 14 cycles behind the tree.
    reg meta_any;
    always @(*) begin
        meta_any = |tailv;
        for (mj = 1; mj <= 56; mj = mj + 1) meta_any = meta_any | meta[mj][MW-1];
    end
    assign pipe_empty = !meta_any && !d_valid && !iss_valid;

endmodule

`default_nettype wire

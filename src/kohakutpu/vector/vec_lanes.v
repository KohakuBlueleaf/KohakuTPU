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
// SLOT ASSIGNMENT. Striping is by lane -- slice s holds elements s, s+16, ... --
// and a D-deep mode retires W per cycle, so group g at phase p drives slot
// p*W + g. Over D phases the 16 slices of a chunk are covered exactly once.

`default_nettype none

module vec_lanes #(
    parameter integer MODEL   = 1,
    parameter         RF_PRIM = "distributed"
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
    localparam [2:0] R_MAX = 3'd1, R_MIN = 3'd2, R_SUMSQ = 3'd3, R_DOT = 3'd4;
    localparam [4:0] OP_ADD = 5'd3, OP_MUL = 5'd5, OP_MAX = 5'd8, OP_MIN = 5'd9;
    localparam [1:0] SRC_V = 2'd0, SRC_C = 2'd2;

    localparam [23:0] E8_ZERO = 24'h000000;
    localparam [23:0] E8_PINF = 24'h7F8000;
    localparam [23:0] E8_NINF = 24'hFF8000;

    localparam integer MW = 50;

    // ---------------------------------------------------------- declarations
    wire [383:0] rf_a, rf_b, rf_c;
    wire [383:0] alu_out;
    wire [15:0]  alu_ovld, alu_pred;
    wire         d_tail_q;
    wire [15:0]  pmask_now;

    reg  [15:0]  rf_we;
    reg  [383:0] rf_wdata;
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

    wire [2:0] dep = (mode == M_FLAT) ? 3'd1 : (mode == M_D2) ? 3'd2 : 3'd4;
    wire [4:0] wid = (mode == M_FLAT) ? 5'd16 : (mode == M_D2) ? 5'd8 : 5'd4;

    // ONE SET OF LOOP VARIABLES PER always BLOCK. Sharing module-level
    // integers between two `always @(*)` blocks is a simulation race: each
    // re-trigger clobbers the other's counters mid-iteration.
    integer og, ot, oj, on_, osl;
    integer wg, wsl, wn;
    integer pg;
    integer mi, mj;
    integer ai, pq;

    reg [4:0]  op_t;
    reg [1:0]  sa_t, sb_t, sc_t;
    reg [23:0] ka_t, kb_t, kc_t, chain_t;
    reg [3:0]  gsl;

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
    wire       half    = (d_kind == R_SUMSQ) || (d_kind == R_DOT);
    wire [4:0] l0_op   = (half && !d_tail) ? OP_MUL : comb_op;

    // ================================================== operand wiring
    // A sibling drives BOTH b and c, so one wiring serves every node op: ADD
    // reads a+c, MAX/MIN read a and b, MUL reads a*b.
    always @(*) begin
        alu_a  = 384'd0; alu_b = 384'd0; alu_c = 384'd0;
        alu_op = 80'd0;  alu_iv = 16'd0;

        if (mode == M_TREE) begin
            for (oj = 0; oj < 8; oj = oj + 1) begin
                if (d_tail) begin
                    alu_a[oj*24 +: 24] = acc[2*oj];
                    alu_b[oj*24 +: 24] = acc[2*oj+1];
                    alu_c[oj*24 +: 24] = acc[2*oj+1];
                end else if (half) begin
                    osl = d_phase * 8 + oj;
                    alu_a[oj*24 +: 24] = rf_a[osl*24 +: 24];
                    alu_b[oj*24 +: 24] = rf_b[osl*24 +: 24];
                    alu_c[oj*24 +: 24] = rf_b[osl*24 +: 24];
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
        end else begin
            for (og = 0; og < 16; og = og + 1) begin
                if (og < wid) begin
                    gsl = d_phase * wid + og;
                    for (ot = 0; ot < 4; ot = ot + 1) begin
                        if (ot < dep) begin
                            on_  = og * dep + ot;
                            op_t = st_op[ot*5 +: 5];
                            sa_t = st_sa[ot*2 +: 2];
                            sb_t = st_sb[ot*2 +: 2];
                            sc_t = st_sc[ot*2 +: 2];
                            ka_t = st_ka[ot*24 +: 24];
                            kb_t = st_kb[ot*24 +: 24];
                            kc_t = st_kc[ot*24 +: 24];
                            chain_t = (ot == 0) ? 24'd0
                                                : alu_out[(on_-1)*24 +: 24];

                            alu_a[on_*24 +: 24] = (sa_t == SRC_V) ? rf_a[gsl*24 +: 24]
                                                : (sa_t == SRC_C) ? chain_t : ka_t;
                            alu_b[on_*24 +: 24] = (sb_t == SRC_V) ? rf_b[gsl*24 +: 24]
                                                : (sb_t == SRC_C) ? chain_t : kb_t;
                            alu_c[on_*24 +: 24] = (sc_t == SRC_V) ? rf_c[gsl*24 +: 24]
                                                : (sc_t == SRC_C) ? chain_t : kc_t;

                            alu_op[on_*5 +: 5] = op_t;
                            alu_iv[on_] = (ot == 0) ? d_valid : alu_ovld[on_-1];
                        end
                    end
                end
            end
        end
    end

    // ================================================== metadata pipeline
    // Loaded beside the ALU input, so tap 14*D is valid the cycle that chain
    // retires: FLAT 14, D2 28, D4 and TREE 56.
    assign p_rd_bits = preg[p_rd_sel];
    assign pmask_now = preg[q_pr][q_chunk*16 +: 16];

    wire [MW-1:0] meta_in = {d_valid, d_is_cmp, q_pr, q_wa, d_phase,
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

    wire [MW-1:0] wb = (mode == M_FLAT) ? meta[14]
                     : (mode == M_D2)   ? meta[28] : meta[56];

    wire        wb_valid = wb[49];
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
    wire [2:0] wdep = dep;
    wire [4:0] wwid = wid;

    always @(*) begin
        rf_we    = 16'd0;
        rf_wdata = 384'd0;
        rf_waddr = wb_wa;
        if (ls_we) begin
            rf_we    = 16'hFFFF;
            rf_wdata = ls_wdata;
            rf_waddr = ls_waddr;
        end else if (wb_valid && (mode != M_TREE) && !wb_cmp) begin
            for (wg = 0; wg < 16; wg = wg + 1) begin
                if (wg < wwid) begin
                    wsl = wb_ph * wwid + wg;
                    wn  = wg * wdep + (wdep - 1);
                    rf_wdata[wsl*24 +: 24] = alu_out[wn*24 +: 24];
                    rf_we[wsl] = ((wb_pm == 2'd0) ? 1'b1
                               :  (wb_pm == 2'd1) ? wb_pmask[wsl]
                                                  : ~wb_pmask[wsl]) & wb_tmask[wsl];
                end
            end
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            for (pq = 0; pq < 4; pq = pq + 1) preg[pq] <= 128'd0;
        end else if (wb_valid && (mode != M_TREE) && wb_cmp) begin
            for (pg = 0; pg < 16; pg = pg + 1)
                if ((pg < wwid) && wb_tmask[wb_ph * wwid + pg])
                    preg[wb_pr][wb_chunk*16 + wb_ph*wwid + pg] <=
                        alu_pred[pg * wdep + (wdep - 1)];
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

    assign wb_fire = wb_valid && (mode != M_TREE) && !wb_cmp;
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

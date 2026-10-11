// V2 lanes: sixteen ALUs, the banked register file, and the operand network.
//
// REGISTER FILE. 32 registers x 8 chunks x 16 lanes x 24 bit, in two banks by
// CHUNK PARITY (bank = chunk bit 0). Each bank has ONE write port and four
// read copies (a, b, c for math; d for pack), all block RAM. A streaming op
// writes chunk i on beat i, so math alternates banks every cycle, and so does
// an unpack: an unpack write is offered one cycle ahead and taken when math
// leaves that bank free next cycle (`mw_bank_next`), after which the two run
// out of phase and both write every cycle, whatever registers they name.
//
// OPERAND NETWORK, two registered stages between the read and the ALU input:
//   s1  bank OR, pair-merge:  P = sel_k(A, B) -> a,  Z = sel_k(B, A)
//   s2  lane crossbar on Z (xor k | rot k | bcast lane | identity) -> b, and
//       -> c when `xc`; then the S/K constant selects.
// A plain op has merge off, so P = A and Z = B. Chunk addressing (base and
// stride per operand) lives in the issuing core: it is only an address.

`default_nettype none

module v2_lanes #(
    parameter integer MODEL    = 1,
    parameter integer PIPE_MUX = 1,
    parameter integer HAS_EXPD = 1,
    parameter integer TAU      = 8,
    parameter integer XNET     = 1     // v2_xbar: 0 one 16:1 select, 1 two 4:1 levels
)(
    input  wire         clk,
    input  wire         rst,

    // ---- math issue, one beat a cycle ------------------------------------
    input  wire         iss_valid,
    input  wire         iss_last,     // last beat of its instruction
    input  wire [4:0]   iss_op,       // vec_alu numbering
    input  wire [7:0]   iss_ra, iss_rb, iss_rc,   // {vreg, chunk}
    input  wire [1:0]   iss_sa, iss_sb, iss_sc,   // 0 V, 1 S, 3 K
    input  wire [23:0]  iss_ka, iss_kb, iss_kc,
    input  wire [2:0]   iss_xm,       // 0 none 1 xor 2 rot 3 bcast 4 merge
    input  wire [3:0]   iss_xk,       // xor/rot amount, merge bit; bcast lane
    input  wire         iss_xc,       // the crossbar output also feeds c
    input  wire [7:0]   iss_wa,
    input  wire         iss_wr,       // writes a vector register
    input  wire         iss_cmp,
    input  wire [1:0]   iss_pr, iss_pm,
    input  wire [15:0]  iss_tmask,
    input  wire         iss_expd,

    // ---- unpack write, offered one cycle ahead ----------------------------
    input  wire         uw_we,
    input  wire [7:0]   uw_wa,
    input  wire [383:0] uw_wd,
    output wire [1:0]   mw_bank_next, // math writes bank b next cycle

    // ---- pack read port, READ_LAT 1 + one bank-select register ------------
    input  wire [7:0]   pk_ra,
    output reg  [383:0] pk_rd,        // answers pk_ra two cycles later

    // ---- write-back report ------------------------------------------------
    output wire         wb_last,      // an instruction's last beat retired
    output wire [4:0]   wb_vreg,
    output wire         wb_wr,
    output wire         wb_cmp,
    output wire [1:0]   wb_pr,
    output wire         pipe_empty,

    // ---- EXP2D overflow sticky -------------------------------------------
    input  wire         stk_clr,
    output reg  [15:0]  sticky
);
    localparam integer ALAT = 14 + ((PIPE_MUX != 0) ? 1 : 0);
    localparam [2:0] XM_MRG = 3'd4;
    localparam [1:0] SRC_V = 2'd0, SRC_S = 2'd1, SRC_K = 2'd3;

    genvar l, b;
    integer i;

    // {vreg, chunk} -> a bank's word address; the bank is chunk bit 0.
    function [6:0] bad;
        input [7:0] a;
        bad = {a[7:3], a[2:1]};
    endfunction

    // ================================================== register file
    reg  [15:0]  bw_we [0:1];
    reg  [6:0]   bw_wa [0:1];
    wire [383:0] bw_wd [0:1];
    // Read copies a, b, c, d of each bank. A copy whose address names the
    // other bank resets its output latch (BRAM RSTRAM), so a port's two banks
    // combine by OR, which folds into the next mux: no bank-select LUTs.
    wire [7:0]   rf_ad [0:3];
    assign rf_ad[0] = iss_ra; assign rf_ad[1] = iss_rb;
    assign rf_ad[2] = iss_rc; assign rf_ad[3] = pk_ra;
    wire [383:0] rf_q [0:7];              // [copy*2 + bank]
    genvar p;
    generate
    for (b = 0; b < 2; b = b + 1) begin : g_bank
        for (p = 0; p < 4; p = p + 1) begin : g_copy
            for (l = 0; l < 16; l = l + 1) begin : g_lane
                xpm_memory_sdpram #(
                    .ADDR_WIDTH_A(7), .ADDR_WIDTH_B(7),
                    .WRITE_DATA_WIDTH_A(24), .READ_DATA_WIDTH_B(24), .BYTE_WRITE_WIDTH_A(24),
                    .MEMORY_SIZE(24 * 128), .MEMORY_PRIMITIVE("block"),
                    .CLOCKING_MODE("common_clock"), .READ_LATENCY_B(1),
                    .WRITE_MODE_B("read_first"),
                    .MEMORY_INIT_FILE("none"), .USE_MEM_INIT(0), .ECC_MODE("no_ecc"),
                    .AUTO_SLEEP_TIME(0), .CASCADE_HEIGHT(0), .SIM_ASSERT_CHK(0),
                    .WAKEUP_TIME("disable_sleep")
                ) u_rf (
                    .clka(clk), .ena(bw_we[b][l]), .wea(1'b1), .addra(bw_wa[b]),
                    .dina(bw_wd[b][l*24 +: 24]),
                    .clkb(clk), .enb(1'b1), .addrb(bad(rf_ad[p])),
                    .doutb(rf_q[p*2 + b][l*24 +: 24]),
                    .rstb(rf_ad[p][0] != (b == 1)), .regceb(1'b1),
                    .injectsbiterra(1'b0), .injectdbiterra(1'b0),
                    .sbiterrb(), .dbiterrb(), .sleep(1'b0));
            end
        end
    end
    endgenerate

    // Pack's converter starts from a flop rather than a BRAM.
    always @(posedge clk) begin
        pk_rd <= rf_q[6] | rf_q[7];
    end

    // ================================================== s1: bank select, merge
    reg         q1_v, q1_last, q1_wr, q1_cmp, q1_expd, q1_xc;
    reg  [4:0]  q1_op;
    reg  [2:0]  q1_xm;
    reg  [3:0]  q1_xk;
    reg  [1:0]  q1_sa, q1_sb, q1_sc, q1_pr, q1_pm;
    reg  [23:0] q1_ka, q1_kb, q1_kc;
    reg  [7:0]  q1_wa;
    reg  [15:0] q1_tm;
    reg  [15:0] q1_swap;            // per lane: merge takes b as P, a as Z

    always @(posedge clk) begin
        if (rst) begin
            q1_v <= 1'b0;
        end else begin
            q1_v <= iss_valid;
        end
        q1_last <= iss_last; q1_wr <= iss_wr; q1_cmp <= iss_cmp; q1_expd <= iss_expd;
        q1_xc <= iss_xc; q1_op <= iss_op; q1_xm <= iss_xm; q1_xk <= iss_xk;
        q1_sa <= iss_sa; q1_sb <= iss_sb; q1_sc <= iss_sc;
        q1_pr <= iss_pr; q1_pm <= iss_pm;
        q1_ka <= iss_ka; q1_kb <= iss_kb; q1_kc <= iss_kc;
        q1_wa <= iss_wa; q1_tm <= iss_tmask;
        for (i = 0; i < 16; i = i + 1) begin
            // Lane i's own bit k picks which operand it reduces.
            q1_swap[i] <= (iss_xm == XM_MRG) && ((i[3:0] & iss_xk) != 4'd0);
        end
    end

    wire [383:0] va = rf_q[0] | rf_q[1];
    wire [383:0] vb = rf_q[2] | rf_q[3];

    reg  [383:0] s2_p, s2_z, s2_c0, s2_c1;
    reg          q2_kc_on;
    reg          q2_v, q2_last, q2_wr, q2_cmp, q2_expd, q2_xc;
    reg  [4:0]   q2_op;
    reg  [1:0]   q2_sa, q2_sb, q2_sc, q2_pr, q2_pm;
    reg  [23:0]  q2_ka, q2_kb, q2_kc;
    reg  [7:0]   q2_wa;
    reg  [15:0]  q2_tm;

    always @(posedge clk) begin
        for (i = 0; i < 16; i = i + 1) begin
            if (q1_swap[i]) begin
                s2_p[i*24 +: 24] <= vb[i*24 +: 24];
                s2_z[i*24 +: 24] <= va[i*24 +: 24];
            end else begin
                s2_p[i*24 +: 24] <= va[i*24 +: 24];
                s2_z[i*24 +: 24] <= vb[i*24 +: 24];
            end
        end
        s2_c0 <= rf_q[4];
        s2_c1 <= rf_q[5];
        q2_kc_on <= (q1_sc != SRC_V);
        if (rst) begin
            q2_v <= 1'b0;
        end else begin
            q2_v <= q1_v;
        end
        q2_last <= q1_last; q2_wr <= q1_wr; q2_cmp <= q1_cmp; q2_expd <= q1_expd;
        q2_xc <= q1_xc; q2_op <= q1_op;
        q2_sa <= q1_sa; q2_sb <= q1_sb; q2_sc <= q1_sc;
        q2_pr <= q1_pr; q2_pm <= q1_pm;
        q2_ka <= q1_ka; q2_kb <= q1_kb; q2_kc <= q1_kc;
        q2_wa <= q1_wa; q2_tm <= q1_tm;
    end

    // ================================================== s2: crossbar, constants
    wire [383:0] xq;
    v2_xbar #(.XNET(XNET), .W(24)) u_xb (
        .clk(clk), .xm(q1_xm), .xk(q1_xk), .z(s2_z), .xq(xq));

    reg  [383:0] alu_a, alu_b, alu_c;
    reg  [4:0]   alu_op;
    reg          alu_iv;
    always @(posedge clk) begin
        for (i = 0; i < 16; i = i + 1) begin
            alu_a[i*24 +: 24] <= (q2_sa == SRC_V) ? s2_p[i*24 +: 24] : q2_ka;
            alu_b[i*24 +: 24] <= (q2_sb == SRC_V) ? xq[i*24 +: 24]   : q2_kb;
            alu_c[i*24 +: 24] <= q2_kc_on ? q2_kc
                               : q2_xc ? xq[i*24 +: 24] : (s2_c0[i*24 +: 24] | s2_c1[i*24 +: 24]);
        end
        alu_op <= q2_op;
        if (rst) begin
            alu_iv <= 1'b0;
        end else begin
            alu_iv <= q2_v;
        end
    end

    // ================================================== ALUs
    wire [383:0] alu_out;
    wire [15:0]  alu_ovld, alu_pred, alu_ovf;
    generate
    for (l = 0; l < 16; l = l + 1) begin : g_alu
        v2_alu #(.MODEL(MODEL), .PIPE_MUX(PIPE_MUX), .HAS_EXPD(HAS_EXPD), .TAU(TAU)) u_alu (
            .clk(clk), .rst(rst),
            .in_valid(alu_iv), .op(alu_op),
            .a(alu_a[l*24 +: 24]), .b(alu_b[l*24 +: 24]), .c(alu_c[l*24 +: 24]),
            .out_valid(alu_ovld[l]), .out(alu_out[l*24 +: 24]),
            .out_pred(alu_pred[l]), .out_ovf(alu_ovf[l])
        );
    end
    endgenerate

    // ================================================== metadata pipeline
    // Loaded beside alu_a, one register ahead of the ALU's own input
    // register, so meta[MD] with MD = ALAT + 1 is the retire cycle.
    localparam integer MD = ALAT + 1;
    reg [127:0] preg [0:3];
    wire [15:0] pmask_now = preg[q2_pr][q2_wa[2:0]*16 +: 16];
    localparam integer MW = 1 + 1 + 1 + 1 + 1 + 2 + 2 + 16 + 16 + 8;
    reg [MW-1:0] meta [1:MD];
    wire [MW-1:0] meta_in = {q2_v, q2_last, q2_wr, q2_cmp, q2_expd, q2_pr, q2_pm,
                             pmask_now, q2_tm, q2_wa};
    integer mi;
    always @(posedge clk) begin
        meta[1] <= meta_in;
        for (mi = 2; mi <= MD; mi = mi + 1) begin
            meta[mi] <= meta[mi-1];
        end
        if (rst) begin
            for (mi = 1; mi <= MD; mi = mi + 1) begin
                meta[mi][MW-1] <= 1'b0;
            end
        end
    end

    wire [MW-1:0] pm_ = meta[MD-1];       // one cycle before retire
    wire [MW-1:0] wm_ = meta[MD];         // the retire cycle
    wire          p_v   = pm_[MW-1];
    wire          p_wr  = pm_[MW-3];
    wire          p_cmp = pm_[MW-4];
    wire [1:0]    p_pm  = pm_[40 +: 2];
    wire [15:0]   p_pk  = pm_[24 +: 16];
    wire [15:0]   p_tm  = pm_[8 +: 16];
    wire [7:0]    p_wa  = pm_[0 +: 8];

    wire          w_v    = wm_[MW-1];
    wire          w_last = wm_[MW-2];
    wire          w_wr   = wm_[MW-3];
    wire          w_cmp  = wm_[MW-4];
    wire          w_expd = wm_[MW-5];
    wire [1:0]    w_pr   = wm_[42 +: 2];
    wire [15:0]   w_tm   = wm_[8 +: 16];
    wire [7:0]    w_wa   = wm_[0 +: 8];

    wire p_write = p_v & p_wr & ~p_cmp;
    assign mw_bank_next = {p_write & p_wa[0], p_write & ~p_wa[0]};

    // ================================================== write ports
    wire [15:0] p_we = ((p_pm == 2'd0) ? 16'hFFFF : (p_pm == 2'd1) ? p_pk : ~p_pk) & p_tm;
    reg  [1:0]   use_u;
    reg  [383:0] uw_q;
    always @(posedge clk) begin
        uw_q <= uw_wd;
        for (i = 0; i < 2; i = i + 1) begin
            if (rst) begin
                bw_we[i] <= 16'd0;
                use_u[i] <= 1'b0;
            end else if (p_write && (p_wa[0] == i[0])) begin
                bw_we[i] <= p_we;
                bw_wa[i] <= bad(p_wa);
                use_u[i] <= 1'b0;
            end else if (uw_we && (uw_wa[0] == i[0])) begin
                bw_we[i] <= 16'hFFFF;
                bw_wa[i] <= bad(uw_wa);
                use_u[i] <= 1'b1;
            end else begin
                bw_we[i] <= 16'd0;
            end
        end
    end
    assign bw_wd[0] = use_u[0] ? uw_q : alu_out;
    assign bw_wd[1] = use_u[1] ? uw_q : alu_out;

    // ================================================== predicates, sticky
    always @(posedge clk) begin
        // RESET-RISK: unreset like v1's; a program writes a predicate first.
        if (w_v && w_cmp) begin
            for (i = 0; i < 16; i = i + 1) begin
                if (w_tm[i]) begin
                    preg[w_pr][w_wa[2:0]*16 + i] <= alu_pred[i];
                end
            end
        end
        if (rst || stk_clr) begin
            sticky <= 16'd0;
        end else if (w_v && w_expd) begin
            sticky <= sticky | (alu_ovf & w_tm);
        end
    end

    assign wb_last = w_v & w_last;
    assign wb_vreg = w_wa[7:3];
    assign wb_wr   = w_wr & ~w_cmp;
    assign wb_cmp  = w_cmp;
    assign wb_pr   = w_pr;

    reg meta_any;
    always @(*) begin
        meta_any = 1'b0;
        for (mi = 1; mi <= MD; mi = mi + 1) begin
            meta_any = meta_any | meta[mi][MW-1];
        end
    end
    assign pipe_empty = !meta_any && !iss_valid && !q1_v && !q2_v && !alu_iv;

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (!rst && uw_we && p_write && (uw_wa[0] == p_wa[0])) begin
            $display("V2_WB_COLLISION %m t=%0t", $time);
        end
    end
`endif

endmodule

`default_nettype wire

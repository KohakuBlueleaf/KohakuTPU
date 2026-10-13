// v2_lanes under a quiescence gate, idle while nothing issues, unpacks or
// reads ahead; docs/models.md. Settling: the metadata line drains (ALAT + 1),
// its last register-file write is read back and crosses the operand network
// into sixteen v2_alus, whose own gates (SETTLE 32) then have to saturate:
// about 56 edges.

`default_nettype none

module v2_lanes #(
    parameter integer MODEL     = 1,
    parameter integer PIPE_MUX  = 1,
    parameter integer HAS_EXPD  = 1,
    parameter integer TAU       = 8,
    parameter integer XNET      = 1,
    parameter integer RF_COPIES = 4,
    parameter         TAB_PRIM  = "block",
    parameter integer RW        = 5
)(
    input  wire          clk,
    input  wire          rst,
    input  wire          iss_valid,
    input  wire          iss_last,
    input  wire [4:0]    iss_op,
    input  wire [RW+2:0] iss_ra, iss_rb, iss_rc,
    input  wire [1:0]    iss_sa, iss_sb, iss_sc,
    input  wire [23:0]   iss_ka, iss_kb, iss_kc,
    input  wire [2:0]    iss_xm,
    input  wire [3:0]    iss_xk,
    input  wire          iss_xc,
    input  wire [RW+2:0] iss_wa,
    input  wire          iss_wr,
    input  wire          iss_cmp,
    input  wire [1:0]    iss_pr, iss_pm,
    input  wire [15:0]   iss_tmask,
    input  wire          iss_expd,
    input  wire          uw_we,
    input  wire [RW+2:0] uw_wa,
    input  wire [383:0]  uw_wd,
    output wire [1:0]    mw_bank_next,
    input  wire [RW+2:0] pk_ra,
    input  wire          pk_v,
    output wire [383:0]  pk_rd,
    input  wire          pre_v,
    input  wire [RW+2:0] pre_rc,
    input  wire          iss_ch,
    output wire          wb_last,
    output wire [RW-1:0] wb_vreg,
    output wire          wb_wr,
    output wire          wb_cmp,
    output wire [1:0]    wb_pr,
    output wire          pipe_empty,
    input  wire          stk_clr,
    output wire [15:0]   sticky
);
    // Seven register addresses ({vreg, chunk}, RW + 3 bits) and 506 other bits.
    localparam integer W = 506 + 7 * (RW + 3);

    wire gclk;
    vlt_qgate #(.W(W), .SETTLE(96)) u_gate (
        .clk(clk),
        .in({rst, iss_valid, iss_last, iss_op, iss_ra, iss_rb, iss_rc,
             iss_sa, iss_sb, iss_sc, iss_ka, iss_kb, iss_kc, iss_xm, iss_xk, iss_xc,
             iss_wa, iss_wr, iss_cmp, iss_pr, iss_pm, iss_tmask, iss_expd,
             uw_we, uw_wa, uw_wd, pk_ra, pk_v, pre_v, pre_rc, iss_ch, stk_clr}),
        .idle(!iss_valid && !uw_we && !pre_v), .gclk(gclk)
    );

    v2_lanes__rtl #(
        .MODEL(MODEL), .PIPE_MUX(PIPE_MUX), .HAS_EXPD(HAS_EXPD), .TAU(TAU),
        .XNET(XNET), .RF_COPIES(RF_COPIES), .TAB_PRIM(TAB_PRIM), .RW(RW)
    ) u_rtl (
        .clk(gclk), .rst(rst),
        .iss_valid(iss_valid), .iss_last(iss_last), .iss_op(iss_op),
        .iss_ra(iss_ra), .iss_rb(iss_rb), .iss_rc(iss_rc),
        .iss_sa(iss_sa), .iss_sb(iss_sb), .iss_sc(iss_sc),
        .iss_ka(iss_ka), .iss_kb(iss_kb), .iss_kc(iss_kc),
        .iss_xm(iss_xm), .iss_xk(iss_xk), .iss_xc(iss_xc),
        .iss_wa(iss_wa), .iss_wr(iss_wr), .iss_cmp(iss_cmp),
        .iss_pr(iss_pr), .iss_pm(iss_pm), .iss_tmask(iss_tmask), .iss_expd(iss_expd),
        .uw_we(uw_we), .uw_wa(uw_wa), .uw_wd(uw_wd), .mw_bank_next(mw_bank_next),
        .pk_ra(pk_ra), .pk_v(pk_v), .pk_rd(pk_rd),
        .pre_v(pre_v), .pre_rc(pre_rc), .iss_ch(iss_ch),
        .wb_last(wb_last), .wb_vreg(wb_vreg), .wb_wr(wb_wr), .wb_cmp(wb_cmp),
        .wb_pr(wb_pr), .pipe_empty(pipe_empty),
        .stk_clr(stk_clr), .sticky(sticky)
    );
endmodule

`default_nettype wire

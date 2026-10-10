// vec_lanes under a quiescence gate, idle while nothing issues, loads or
// starts a reduction; docs/models.md. Settling is long: the metadata line's
// last write-back (~62 cycles) then ripples through up to five chained ALUs,
// whose own gates (inside) then have to saturate.

`default_nettype none

`ifdef VEC_NO_PIPE_MUX
  `define VLT_VL_PM 0
`else
  `define VLT_VL_PM 1
`endif

module vec_lanes #(
    parameter integer MODEL    = 1,
    parameter integer VLMAX    = 128,
    parameter         RF_PRIM  = "block",
    parameter integer RF_PAD   = 24,
    parameter integer RF_PACK  = 1,
    parameter integer PIPE_MUX = `VLT_VL_PM,
    parameter integer LA       = 4,
    parameter integer CW       = $clog2(VLMAX / 16),
    parameter integer RW       = 4 + CW
)(
    input  wire          clk,
    input  wire          rst,
    input  wire [1:0]    mode,
    input  wire          ls_we,
    input  wire [1:0]    ls_pm,
    input  wire [1:0]    ls_pr,
    input  wire [RW-1:0] ls_waddr,
    input  wire [383:0]  ls_wdata,
    input  wire [RW-1:0] ls_raddr,
    input  wire          ls_ractive,
    input  wire          ls_rsel,
    output wire [383:0]  ls_rdata,
    input  wire          iss_valid,
    input  wire [1:0]    iss_phase,
    input  wire [RW-1:0] iss_ra,
    input  wire [RW-1:0] iss_rb,
    input  wire [RW-1:0] iss_rc,
    input  wire [RW-1:0] iss_wa,
    input  wire [1:0]    iss_pm,
    input  wire [1:0]    iss_pr,
    input  wire [CW-1:0] iss_chunk,
    input  wire          iss_is_cmp,
    input  wire          iss_tail,
    input  wire [15:0]   iss_tmask,
    input  wire [19:0]   st_op,
    input  wire [7:0]    st_sa,
    input  wire [7:0]    st_sb,
    input  wire [7:0]    st_sc,
    input  wire [95:0]   st_ka,
    input  wire [95:0]   st_kb,
    input  wire [95:0]   st_kc,
    input  wire          red_init,
    input  wire [2:0]    red_kind,
    input  wire [1:0]    p_rd_sel,
    output wire [VLMAX-1:0] p_rd_bits,
    output wire [23:0]   red_result,
    output wire          red_valid,
    output wire          pipe_empty,
    output wire          ls_quiet,
    output wire          wb_ahead,
    output wire          wb_fire,
    output wire [3:0]    wb_vreg
);
    localparam integer W = 1 + 2 + 1 + 2 + 2 + RW + 384 + RW + 1 + 1
                         + 1 + 2 + 4*RW + 2 + 2 + CW + 1 + 1 + 16
                         + 20 + 3*8 + 3*96 + 1 + 3 + 2;

    wire gclk;
    vlt_qgate #(.W(W), .SETTLE(255)) u_gate (
        .clk(clk),
        .in({rst, mode, ls_we, ls_pm, ls_pr, ls_waddr, ls_wdata, ls_raddr, ls_ractive,
             ls_rsel, iss_valid, iss_phase, iss_ra, iss_rb, iss_rc, iss_wa, iss_pm, iss_pr,
             iss_chunk, iss_is_cmp, iss_tail, iss_tmask, st_op, st_sa, st_sb, st_sc,
             st_ka, st_kb, st_kc, red_init, red_kind, p_rd_sel}),
        .idle(!iss_valid && !ls_we && !red_init), .gclk(gclk)
    );

    vec_lanes__rtl #(
        .MODEL(MODEL), .VLMAX(VLMAX), .RF_PRIM(RF_PRIM), .RF_PAD(RF_PAD),
        .RF_PACK(RF_PACK), .PIPE_MUX(PIPE_MUX), .LA(LA), .CW(CW), .RW(RW)
    ) u_rtl (
        .clk(gclk), .rst(rst), .mode(mode),
        .ls_we(ls_we), .ls_pm(ls_pm), .ls_pr(ls_pr), .ls_waddr(ls_waddr),
        .ls_wdata(ls_wdata), .ls_raddr(ls_raddr), .ls_ractive(ls_ractive),
        .ls_rsel(ls_rsel), .ls_rdata(ls_rdata),
        .iss_valid(iss_valid), .iss_phase(iss_phase), .iss_ra(iss_ra), .iss_rb(iss_rb),
        .iss_rc(iss_rc), .iss_wa(iss_wa), .iss_pm(iss_pm), .iss_pr(iss_pr),
        .iss_chunk(iss_chunk), .iss_is_cmp(iss_is_cmp), .iss_tail(iss_tail),
        .iss_tmask(iss_tmask),
        .st_op(st_op), .st_sa(st_sa), .st_sb(st_sb), .st_sc(st_sc),
        .st_ka(st_ka), .st_kb(st_kb), .st_kc(st_kc),
        .red_init(red_init), .red_kind(red_kind),
        .p_rd_sel(p_rd_sel), .p_rd_bits(p_rd_bits),
        .red_result(red_result), .red_valid(red_valid),
        .pipe_empty(pipe_empty), .ls_quiet(ls_quiet), .wb_ahead(wb_ahead),
        .wb_fire(wb_fire), .wb_vreg(wb_vreg)
    );
endmodule

`undef VLT_VL_PM

`default_nettype wire

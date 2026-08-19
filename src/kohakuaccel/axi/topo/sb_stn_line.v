// One station on a LINE. There is no root: every station is the same module,
// with local managers, local subordinates, and a left and a right neighbour.

//   to_right <- mux2(from_left,  inject)
//   to_left  <- mux2(from_right, inject)
//   eject    <- mux2(from_left,  from_right)

// A flit carries dst_stn, so each hop decides for itself: mine, or keep going.
// NOTHING here scales with the product of port counts.

`default_nettype none

module sb_stn_line #(
    parameter integer FW    = 512,
    parameter integer AW    = 40,
    parameter integer TAGW  = 4,
    parameter integer NSTN  = 4,               // stations on the line
    parameter integer STN   = 0,               // this station's index
    parameter integer NM    = 1,               // local managers
    parameter integer NQ    = 2,               // local subordinates
    parameter integer STNW  = (NSTN <= 1) ? 1 : $clog2(NSTN),
    parameter integer PORTW = (NQ   <= 1) ? 1 : $clog2(NQ),
    parameter integer SRCW  = (NM   <= 1) ? 1 : $clog2(NM),
    parameter integer STATS = 0,
    // Parameters, not localparams: the link ports use them, and a port cannot
    // see a localparam declared after it.
    parameter integer RQW = STNW + PORTW + STNW + SRCW + TAGW + 3
                            + AW + 8 + 3 + FW + FW/8,
    parameter integer RSW = STNW + SRCW + TAGW + 2 + 2 + FW
)(
    input  wire clk,
    input  wire rst,

    // ---- local managers in ------------------------------------------------
    input  wire [NM-1:0]         nm_req_valid,
    output wire [NM-1:0]         nm_req_ready,
    input  wire [NM*STNW-1:0]    nm_req_dstn,
    input  wire [NM*PORTW-1:0]   nm_req_dport,
    input  wire [NM*TAGW-1:0]    nm_req_tag,
    input  wire [NM-1:0]         nm_req_wr,
    input  wire [NM-1:0]         nm_req_head,
    input  wire [NM-1:0]         nm_req_last,
    input  wire [NM*AW-1:0]      nm_req_addr,
    input  wire [NM*8-1:0]       nm_req_len,
    input  wire [NM*3-1:0]       nm_req_size,
    input  wire [NM*FW-1:0]      nm_req_data,
    input  wire [NM*(FW/8)-1:0]  nm_req_strb,

    // ---- local subordinates out (broadcast payload, per-port valid) --------
    output wire [NQ-1:0]         ns_req_valid,
    input  wire [NQ-1:0]         ns_req_ready,
    // {src_stn, src_port} packed: the NSU treats it as opaque and echoes it,
    // so sb_nsu needs no knowledge of the line at all.
    output wire [STNW+SRCW-1:0]  ns_req_src,
    output wire [TAGW-1:0]       ns_req_tag,
    output wire                  ns_req_wr,
    output wire                  ns_req_head,
    output wire                  ns_req_last,
    output wire [AW-1:0]         ns_req_addr,
    output wire [7:0]            ns_req_len,
    output wire [2:0]            ns_req_size,
    output wire [FW-1:0]         ns_req_data,
    output wire [FW/8-1:0]       ns_req_strb,

    // ---- local subordinate responses in ------------------------------------
    input  wire [NQ-1:0]         ns_rsp_valid,
    output wire [NQ-1:0]         ns_rsp_ready,
    input  wire [NQ*(STNW+SRCW)-1:0] ns_rsp_dst,
    input  wire [NQ*TAGW-1:0]    ns_rsp_tag,
    input  wire [NQ-1:0]         ns_rsp_wr,
    input  wire [NQ-1:0]         ns_rsp_last,
    input  wire [NQ*2-1:0]       ns_rsp_resp,
    input  wire [NQ*FW-1:0]      ns_rsp_data,

    // ---- local manager responses out ---------------------------------------
    output wire [NM-1:0]         nm_rsp_valid,
    input  wire [NM-1:0]         nm_rsp_ready,
    output wire [TAGW-1:0]       nm_rsp_tag,
    output wire                  nm_rsp_wr,
    output wire                  nm_rsp_last,
    output wire [1:0]            nm_rsp_resp,
    output wire [FW-1:0]         nm_rsp_data,

    // ---- neighbours: packed flits, one stream each way per class -----------
    input  wire                  lf_req_valid,
    output wire                  lf_req_ready,
    input  wire [RQW-1:0]        lf_req_pay,
    output wire                  lt_req_valid,
    input  wire                  lt_req_ready,
    output wire [RQW-1:0]        lt_req_pay,
    input  wire                  lf_rsp_valid,
    output wire                  lf_rsp_ready,
    input  wire [RSW-1:0]        lf_rsp_pay,
    output wire                  lt_rsp_valid,
    input  wire                  lt_rsp_ready,
    output wire [RSW-1:0]        lt_rsp_pay,

    input  wire                  rf_req_valid,
    output wire                  rf_req_ready,
    input  wire [RQW-1:0]        rf_req_pay,
    output wire                  rt_req_valid,
    input  wire                  rt_req_ready,
    output wire [RQW-1:0]        rt_req_pay,
    input  wire                  rf_rsp_valid,
    output wire                  rf_rsp_ready,
    input  wire [RSW-1:0]        rf_rsp_pay,
    output wire                  rt_rsp_valid,
    input  wire                  rt_rsp_ready,
    output wire [RSW-1:0]        rt_rsp_pay,

    output wire [31:0]           stat_inj_flits,
    output wire [31:0]           stat_inj_wait
);
    localparam [1:0] D_LOC = 2'd0, D_LEFT = 2'd1, D_RIGHT = 2'd2;

    // The hub indexes ITS OWN ports; PORTW/SRCW are line-wide and may be wider.
    // Slice, or a 1-manager station's 1-bit hub dst reads a 2-bit field.
    localparam integer HDW = (NQ <= 1) ? 1 : $clog2(NQ);
    localparam integer HSW = (NM <= 1) ? 1 : $clog2(NM);

    // Mine, or which way onward. This is the whole routing algorithm.
    function [1:0] route;
        input [STNW-1:0] ds;
        begin
            if (ds == STN[STNW-1:0])     route = D_LOC;
            else if (ds <  STN[STNW-1:0]) route = D_LEFT;
            else                          route = D_RIGHT;
        end
    endfunction

    // ===================================================== REQ: inject
    wire [NM*RQW-1:0] inj_pay;
    wire [NM*2-1:0]   inj_dst;

    genvar g;
    generate
    for (g = 0; g < NM; g = g + 1) begin : g_inj
        localparam [SRCW-1:0] MY_PORT = g;
        assign inj_pay[g*RQW +: RQW] = {
            nm_req_dstn [g*STNW  +: STNW],
            nm_req_dport[g*PORTW +: PORTW],
            STN[STNW-1:0],
            MY_PORT,
            nm_req_tag  [g*TAGW  +: TAGW],
            nm_req_wr[g], nm_req_head[g], nm_req_last[g],
            nm_req_addr [g*AW    +: AW],
            nm_req_len  [g*8     +: 8],
            nm_req_size [g*3     +: 3],
            nm_req_data [g*FW    +: FW],
            nm_req_strb [g*(FW/8) +: FW/8] };
        assign inj_dst[g*2 +: 2] = route(nm_req_dstn[g*STNW +: STNW]);
    end
    endgenerate

    wire [2:0]       ij_valid, ij_ready;
    wire [RQW-1:0]   ij_pay;

    sb_hub #(.NSRC(NM), .NDST(3), .PW(RQW), .STATS(STATS)) u_inj (
        .clk(clk), .rst(rst),
        .i_valid(nm_req_valid), .i_ready(nm_req_ready), .i_last(nm_req_last),
        .i_dst(inj_dst), .i_pay(inj_pay),
        .o_valid(ij_valid), .o_ready(ij_ready), .o_pay(ij_pay),
        .stat_flits(stat_inj_flits), .stat_wait(stat_inj_wait)
    );

    // ============================================ REQ: arriving from a link
    // A flit is for this station or it keeps going the way it was already
    // travelling -- an arrival never reverses direction on a line.
    wire [STNW-1:0] lf_dstn = lf_req_pay[RQW-STNW +: STNW];
    wire [STNW-1:0] rf_dstn = rf_req_pay[RQW-STNW +: STNW];
    wire lf_mine = (lf_dstn == STN[STNW-1:0]);
    wire rf_mine = (rf_dstn == STN[STNW-1:0]);

    wire lf_loc_v = lf_req_valid &&  lf_mine;
    wire lf_thr_v = lf_req_valid && !lf_mine;
    wire rf_loc_v = rf_req_valid &&  rf_mine;
    wire rf_thr_v = rf_req_valid && !rf_mine;
    wire lf_loc_r, lf_thr_r, rf_loc_r, rf_thr_r;
    assign lf_req_ready = lf_mine ? lf_loc_r : lf_thr_r;
    assign rf_req_ready = rf_mine ? rf_loc_r : rf_thr_r;

    wire rq_last_lf = lf_req_pay[FW/8 + FW + 3 + 8 + AW];
    wire rq_last_rf = rf_req_pay[FW/8 + FW + 3 + 8 + AW];

    // to_right: from_left passing through, or an injection heading right
    sb_hub #(.NSRC(2), .NDST(1), .PW(RQW)) u_rq_right (
        .clk(clk), .rst(rst),
        .i_valid({lf_thr_v, ij_valid[D_RIGHT]}),
        .i_ready({lf_thr_r, ij_ready[D_RIGHT]}),
        .i_last ({rq_last_lf, ij_pay[FW/8 + FW + 3 + 8 + AW]}),
        .i_dst(2'b00), .i_pay({lf_req_pay, ij_pay}),
        .o_valid(rt_req_valid), .o_ready(rt_req_ready), .o_pay(rt_req_pay)
    );

    sb_hub #(.NSRC(2), .NDST(1), .PW(RQW)) u_rq_left (
        .clk(clk), .rst(rst),
        .i_valid({rf_thr_v, ij_valid[D_LEFT]}),
        .i_ready({rf_thr_r, ij_ready[D_LEFT]}),
        .i_last ({rq_last_rf, ij_pay[FW/8 + FW + 3 + 8 + AW]}),
        .i_dst(2'b00), .i_pay({rf_req_pay, ij_pay}),
        .o_valid(lt_req_valid), .o_ready(lt_req_ready), .o_pay(lt_req_pay)
    );

    // ================================================= REQ: eject to NQ subs
    wire [PORTW-1:0] ej_dst_lf = lf_req_pay[RQW-STNW-PORTW +: PORTW];
    wire [PORTW-1:0] ej_dst_rf = rf_req_pay[RQW-STNW-PORTW +: PORTW];
    wire [PORTW-1:0] ej_dst_ij = ij_pay   [RQW-STNW-PORTW +: PORTW];

    // The hub carries the WHOLE flit through, dst_stn included. Unpack every
    // field or the ones below it shift by STNW bits.
    wire [STNW-1:0]  ej_dstn_unused;
    wire [PORTW-1:0] ej_dport_unused;

    sb_hub #(.NSRC(3), .NDST(NQ), .PW(RQW)) u_rq_eject (
        .clk(clk), .rst(rst),
        .i_valid({rf_loc_v, lf_loc_v, ij_valid[D_LOC]}),
        .i_ready({rf_loc_r, lf_loc_r, ij_ready[D_LOC]}),
        .i_last ({rq_last_rf, rq_last_lf, ij_pay[FW/8 + FW + 3 + 8 + AW]}),
        .i_dst({ej_dst_rf[HDW-1:0], ej_dst_lf[HDW-1:0], ej_dst_ij[HDW-1:0]}),
        .i_pay({rf_req_pay, lf_req_pay, ij_pay}),
        .o_valid(ns_req_valid), .o_ready(ns_req_ready),
        .o_pay({ej_dstn_unused, ej_dport_unused, ns_req_src, ns_req_tag,
                ns_req_wr, ns_req_head, ns_req_last, ns_req_addr,
                ns_req_len, ns_req_size, ns_req_data, ns_req_strb})
    );

    // ===================================================== RSP: collect local
    wire [NQ*RSW-1:0] col_pay;
    wire [NQ*2-1:0]   col_dst;

    generate
    for (g = 0; g < NQ; g = g + 1) begin : g_col
        assign col_pay[g*RSW +: RSW] = {
            ns_rsp_dst[g*(STNW+SRCW) +: STNW+SRCW],
            ns_rsp_tag[g*TAGW +: TAGW],
            ns_rsp_wr[g], ns_rsp_last[g],
            ns_rsp_resp[g*2  +: 2],
            ns_rsp_data[g*FW +: FW] };
        assign col_dst[g*2 +: 2] =
            route(ns_rsp_dst[g*(STNW+SRCW) + SRCW +: STNW]);
    end
    endgenerate

    wire [2:0]     cl_valid, cl_ready;
    wire [RSW-1:0] cl_pay;

    sb_hub #(.NSRC(NQ), .NDST(3), .PW(RSW)) u_rs_col (
        .clk(clk), .rst(rst),
        .i_valid(ns_rsp_valid), .i_ready(ns_rsp_ready), .i_last(ns_rsp_last),
        .i_dst(col_dst), .i_pay(col_pay),
        .o_valid(cl_valid), .o_ready(cl_ready), .o_pay(cl_pay)
    );

    wire [STNW-1:0] lfs_dstn = lf_rsp_pay[RSW-STNW +: STNW];
    wire [STNW-1:0] rfs_dstn = rf_rsp_pay[RSW-STNW +: STNW];
    wire lfs_mine = (lfs_dstn == STN[STNW-1:0]);
    wire rfs_mine = (rfs_dstn == STN[STNW-1:0]);

    wire lfs_loc_v = lf_rsp_valid &&  lfs_mine;
    wire lfs_thr_v = lf_rsp_valid && !lfs_mine;
    wire rfs_loc_v = rf_rsp_valid &&  rfs_mine;
    wire rfs_thr_v = rf_rsp_valid && !rfs_mine;
    wire lfs_loc_r, lfs_thr_r, rfs_loc_r, rfs_thr_r;
    assign lf_rsp_ready = lfs_mine ? lfs_loc_r : lfs_thr_r;
    assign rf_rsp_ready = rfs_mine ? rfs_loc_r : rfs_thr_r;

    // last sits ABOVE resp: {..., wr, last, resp[2], data[FW]}. Reading bit FW
    // picks resp[0], so the hub never releases its grant and locks forever.
    localparam integer RS_LAST = FW + 2;
    wire rs_last_lf = lf_rsp_pay[RS_LAST];
    wire rs_last_rf = rf_rsp_pay[RS_LAST];
    wire rs_last_cl = cl_pay[RS_LAST];

    sb_hub #(.NSRC(2), .NDST(1), .PW(RSW)) u_rs_right (
        .clk(clk), .rst(rst),
        .i_valid({lfs_thr_v, cl_valid[D_RIGHT]}),
        .i_ready({lfs_thr_r, cl_ready[D_RIGHT]}),
        .i_last ({rs_last_lf, rs_last_cl}),
        .i_dst(2'b00), .i_pay({lf_rsp_pay, cl_pay}),
        .o_valid(rt_rsp_valid), .o_ready(rt_rsp_ready), .o_pay(rt_rsp_pay)
    );

    sb_hub #(.NSRC(2), .NDST(1), .PW(RSW)) u_rs_left (
        .clk(clk), .rst(rst),
        .i_valid({rfs_thr_v, cl_valid[D_LEFT]}),
        .i_ready({rfs_thr_r, cl_ready[D_LEFT]}),
        .i_last ({rs_last_rf, rs_last_cl}),
        .i_dst(2'b00), .i_pay({rf_rsp_pay, cl_pay}),
        .o_valid(lt_rsp_valid), .o_ready(lt_rsp_ready), .o_pay(lt_rsp_pay)
    );

    wire [SRCW-1:0] es_dst_lf = lf_rsp_pay[RSW-STNW-SRCW +: SRCW];
    wire [SRCW-1:0] es_dst_rf = rf_rsp_pay[RSW-STNW-SRCW +: SRCW];
    wire [SRCW-1:0] es_dst_cl = cl_pay    [RSW-STNW-SRCW +: SRCW];

    wire [STNW-1:0] es_dstn_unused;
    wire [SRCW-1:0] es_dport_unused;

    sb_hub #(.NSRC(3), .NDST(NM), .PW(RSW)) u_rs_eject (
        .clk(clk), .rst(rst),
        .i_valid({rfs_loc_v, lfs_loc_v, cl_valid[D_LOC]}),
        .i_ready({rfs_loc_r, lfs_loc_r, cl_ready[D_LOC]}),
        .i_last ({rs_last_rf, rs_last_lf, rs_last_cl}),
        .i_dst({es_dst_rf[HSW-1:0], es_dst_lf[HSW-1:0], es_dst_cl[HSW-1:0]}),
        .i_pay({rf_rsp_pay, lf_rsp_pay, cl_pay}),
        .o_valid(nm_rsp_valid), .o_ready(nm_rsp_ready),
        .o_pay({es_dstn_unused, es_dport_unused, nm_rsp_tag, nm_rsp_wr,
                nm_rsp_last, nm_rsp_resp, nm_rsp_data})
    );
endmodule

`default_nettype wire

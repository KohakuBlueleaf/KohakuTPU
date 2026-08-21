// Four stations on a LINE, one per SLR. No root: station 1 happens to hold the
// managers, and every station is the same module.

// The flit format is UNIFORM across the line -- STNW/PORTW/SRCW are set here,
// not derived per station, or a hop would parse a neighbour's flit wrong.

`default_nettype none

module sb_line4 #(
    parameter integer FW    = 512,
    parameter integer AW    = 40,
    parameter integer MAXW  = 512,
    parameter integer MAXID = 4,
    parameter integer NM    = 3,               // managers, all on station 1
    parameter integer NQ    = 2,               // subordinates per station
    parameter integer NS    = 4 * NQ,
    parameter integer TAGW  = 4,
    parameter integer OST   = 4,
    parameter integer STORE_FWD    = 1,
    parameter integer LUT_PER_BRAM = 0,
    parameter integer TIMEOUT      = 0,
    parameter integer PIPE  = 4,
    parameter integer CRED  = 16,
    parameter integer STNW  = 2,
    parameter integer PORTW = 1,
    parameter integer SRCW  = 2,
    // 1 gives every station its own fabric clock and crosses inside the link.
    // At 0 all four bus clocks must be driven by the same net.
    parameter integer LINK_CDC = 0,
    // With managers only on MGR_STN, REQ flows outward and RSP inward, so half
    // the streams are dead. LINK_FULL=0 omits them, halving cross-SLR wires.
    parameter integer MGR_STN   = 1,
    parameter integer LINK_FULL = 1,
    // Port 0's slave width. Defaults to the flit width; set it independently to
    // test a wide slave behind a narrow fabric.
    parameter integer WIDE_DW   = FW,
    // Packed width of the station/port indices; both are 2 in every deployed
    // shape. A parameter, not a localparam, so the SEG_* overrides can size.
    parameter integer DSTW_P    = (STNW > PORTW) ? STNW : PORTW,
    // At 0, the uniform 64 KB map below, which cannot express the mesh's 1 TB
    // S_AXI_MEM window. sb_nmu semantics: XLT == BASE passes through, 0 strips.
    parameter integer SEG_OVERRIDE = 0,
    // Plain 0, not a replication: IPI reads a `{N{1'b0}}` default back as a
    // bit string it cannot convert (IP_Flow 19-594).
    parameter [NS*AW-1:0]     SEG_BASE_P  = 0,
    parameter [NS*AW-1:0]     SEG_MASK_P  = 0,
    parameter [NS*AW-1:0]     SEG_XLT_P   = 0,
    parameter [NS*DSTW_P-1:0] SEG_DST_P   = 0,
    parameter [NS*DSTW_P-1:0] SEG_DPORT_P = 0,
    parameter [NS-1:0]        SEG_VLD_P   = {NS{1'b1}},
    // Per-station overrides; each defaults to the line-wide scalar above. Only
    // shim-private settings appear here -- anything in the flit is line-global.
    parameter integer OST0 = OST, OST1 = OST, OST2 = OST, OST3 = OST,
    parameter integer SFW0 = STORE_FWD, SFW1 = STORE_FWD,
    parameter integer SFW2 = STORE_FWD, SFW3 = STORE_FWD,
    parameter integer LPB0 = LUT_PER_BRAM, LPB1 = LUT_PER_BRAM,
    parameter integer LPB2 = LUT_PER_BRAM, LPB3 = LUT_PER_BRAM,
    parameter integer TMO0 = TIMEOUT, TMO1 = TIMEOUT,
    parameter integer TMO2 = TIMEOUT, TMO3 = TIMEOUT
)(
    input  wire bus_clk0,   input wire bus_rst0,
    input  wire bus_clk1,   input wire bus_rst1,
    input  wire bus_clk2,   input wire bus_rst2,
    input  wire bus_clk3,   input wire bus_rst3,
    input  wire clk_ctrl,   input wire aresetn_ctrl,
    input  wire clk_xdma,   input wire aresetn_xdma,
    input  wire clk_s0,     input wire aresetn_s0,
    input  wire clk_s1,     input wire aresetn_s1,
    input  wire clk_s2,     input wire aresetn_s2,
    input  wire clk_s3,     input wire aresetn_s3,
    // ONE PER STATION. Each SLR's DDR4 controller runs on its own ui_clk, so a
    // single clk_ddr would force a crossing IP in front of three of them.
    input  wire clk_ddr0,   input wire aresetn_ddr0,
    input  wire clk_ddr1,   input wire aresetn_ddr1,
    input  wire clk_ddr2,   input wire aresetn_ddr2,
    input  wire clk_ddr3,   input wire aresetn_ddr3,

    input  wire [NM*MAXID-1:0]    mp_awid,
    input  wire [NM*AW-1:0]       mp_awaddr,
    input  wire [NM*8-1:0]        mp_awlen,
    input  wire [NM*3-1:0]        mp_awsize,
    input  wire [NM*2-1:0]        mp_awburst,
    input  wire [NM-1:0]          mp_awvalid,
    output wire [NM-1:0]          mp_awready,
    input  wire [NM*MAXW-1:0]     mp_wdata,
    input  wire [NM*(MAXW/8)-1:0] mp_wstrb,
    input  wire [NM-1:0]          mp_wlast,
    input  wire [NM-1:0]          mp_wvalid,
    output wire [NM-1:0]          mp_wready,
    output wire [NM*MAXID-1:0]    mp_bid,
    output wire [NM*2-1:0]        mp_bresp,
    output wire [NM-1:0]          mp_bvalid,
    input  wire [NM-1:0]          mp_bready,
    input  wire [NM*MAXID-1:0]    mp_arid,
    input  wire [NM*AW-1:0]       mp_araddr,
    input  wire [NM*8-1:0]        mp_arlen,
    input  wire [NM*3-1:0]        mp_arsize,
    input  wire [NM*2-1:0]        mp_arburst,
    input  wire [NM-1:0]          mp_arvalid,
    output wire [NM-1:0]          mp_arready,
    output wire [NM*MAXID-1:0]    mp_rid,
    output wire [NM*MAXW-1:0]     mp_rdata,
    output wire [NM*2-1:0]        mp_rresp,
    output wire [NM-1:0]          mp_rlast,
    output wire [NM-1:0]          mp_rvalid,
    input  wire [NM-1:0]          mp_rready,

    output wire [NS*MAXID-1:0]    sp_awid,
    output wire [NS*AW-1:0]       sp_awaddr,
    output wire [NS*8-1:0]        sp_awlen,
    output wire [NS*3-1:0]        sp_awsize,
    output wire [NS*2-1:0]        sp_awburst,
    output wire [NS-1:0]          sp_awvalid,
    input  wire [NS-1:0]          sp_awready,
    output wire [NS*MAXW-1:0]     sp_wdata,
    output wire [NS*(MAXW/8)-1:0] sp_wstrb,
    output wire [NS-1:0]          sp_wlast,
    output wire [NS-1:0]          sp_wvalid,
    input  wire [NS-1:0]          sp_wready,
    input  wire [NS*MAXID-1:0]    sp_bid,
    input  wire [NS*2-1:0]        sp_bresp,
    input  wire [NS-1:0]          sp_bvalid,
    output wire [NS-1:0]          sp_bready,
    output wire [NS*MAXID-1:0]    sp_arid,
    output wire [NS*AW-1:0]       sp_araddr,
    output wire [NS*8-1:0]        sp_arlen,
    output wire [NS*3-1:0]        sp_arsize,
    output wire [NS*2-1:0]        sp_arburst,
    output wire [NS-1:0]          sp_arvalid,
    input  wire [NS-1:0]          sp_arready,
    input  wire [NS*MAXID-1:0]    sp_rid,
    input  wire [NS*MAXW-1:0]     sp_rdata,
    input  wire [NS*2-1:0]        sp_rresp,
    input  wire [NS-1:0]          sp_rlast,
    input  wire [NS-1:0]          sp_rvalid,
    output wire [NS-1:0]          sp_rready,

    output wire [31:0]            stat_decerr
);
    localparam integer RQW  = STNW + PORTW + STNW + SRCW + TAGW + 3
                              + AW + 8 + 3 + FW + FW/8;
    localparam integer RSW  = STNW + SRCW + TAGW + 2 + 2 + FW;
    localparam integer NSUS = STNW + SRCW;      // the NSU's opaque src field

    // Station i owns addresses whose top 4 bits are i; bits [18:16] pick the
    // port. One 64K window per endpoint, so the map is NSEG masked compares.
    localparam [AW-1:0] MSK  = {{(AW-16){1'b1}}, 16'd0};
    localparam integer   NSEG = 4 * NQ;
    // sb_nmu slices BOTH segment tables at DSTW, so station and port indices
    // must be packed at the SAME width; the station narrows them again.
    localparam integer   DPW  = DSTW_P;

    // SEG_DST carries the STATION and SEG_DPORT the local port: the NMU does
    // not route, it labels, and each station's route() picks the direction.
    // The unused argument is not decoration: plain Verilog rejects a function
    // with no input, and xvlog enforces it even where xsim does not.
    function [NSEG*AW-1:0] mk_base;
        input integer unused;
        integer k;
        begin
            mk_base = {(NSEG*AW){1'b0}};
            for (k = 0; k < NSEG; k = k + 1)
                mk_base[k*AW +: AW] = ((k / NQ) << (AW - 4))
                                    | ((k % NQ) << 16);
        end
    endfunction
    function [NSEG*DPW-1:0] mk_stn;
        input integer unused;
        integer k;
        begin
            mk_stn = {(NSEG*DPW){1'b0}};
            for (k = 0; k < NSEG; k = k + 1)
                mk_stn[k*DPW +: DPW] = (k / NQ);
        end
    endfunction
    function [NSEG*DPW-1:0] mk_prt;
        input integer unused;
        integer k;
        begin
            mk_prt = {(NSEG*DPW){1'b0}};
            for (k = 0; k < NSEG; k = k + 1)
                mk_prt[k*DPW +: DPW] = (k % NQ);
        end
    endfunction

    // At SEG_OVERRIDE=0 L_XLT == L_BASE, so sb_nmu's XLT_ID folds the rewrite
    // away and this is bit-for-bit the map that shipped before the override.
    localparam [NSEG*AW-1:0]  L_BASE = SEG_OVERRIDE ? SEG_BASE_P : mk_base(0);
    localparam [NSEG*AW-1:0]  L_MASK = SEG_OVERRIDE ? SEG_MASK_P : {NSEG{MSK}};
    localparam [NSEG*AW-1:0]  L_XLT  = SEG_OVERRIDE ? SEG_XLT_P  : mk_base(0);
    localparam [NSEG*DPW-1:0] L_STN  = SEG_OVERRIDE ? SEG_DST_P   : mk_stn(0);
    localparam [NSEG*DPW-1:0] L_PRT  = SEG_OVERRIDE ? SEG_DPORT_P : mk_prt(0);
    localparam [NSEG-1:0]     L_VLD  = SEG_OVERRIDE ? SEG_VLD_P   : {NSEG{1'b1}};

    // v5's shape: ports 0,1 on the station's mesh clock, port 2 on DDR, the
    // rest on ctrl. A station spanning three domains is the case that matters.
    wire [3:0] bclk  = {bus_clk3, bus_clk2, bus_clk1, bus_clk0};
    wire [3:0] brst  = {bus_rst3, bus_rst2, bus_rst1, bus_rst0};
    wire [3:0] mclk  = {clk_s3, clk_s2, clk_s1, clk_s0};
    wire [3:0] mrstn = {aresetn_s3, aresetn_s2, aresetn_s1, aresetn_s0};
    wire [3:0] dclk  = {clk_ddr3, clk_ddr2, clk_ddr1, clk_ddr0};
    wire [3:0] drstn = {aresetn_ddr3, aresetn_ddr2, aresetn_ddr1, aresetn_ddr0};

    function integer port_dom;
        input integer p;
        begin
            if (p < 2)       port_dom = 0;      // mesh
            else if (p == 2) port_dom = 1;      // ddr
            else             port_dom = 2;      // ctrl
        end
    endfunction

    // Per-station flit interfaces. Index 0..3 is the station.
    wire [3:0]         st_lf_rqv, st_lf_rqr, st_lt_rqv, st_lt_rqr;
    wire [3:0]         st_rf_rqv, st_rf_rqr, st_rt_rqv, st_rt_rqr;
    wire [4*RQW-1:0]   st_lf_rqp, st_lt_rqp, st_rf_rqp, st_rt_rqp;
    wire [3:0]         st_lf_rsv, st_lf_rsr, st_lt_rsv, st_lt_rsr;
    wire [3:0]         st_rf_rsv, st_rf_rsr, st_rt_rsv, st_rt_rsr;
    wire [4*RSW-1:0]   st_lf_rsp, st_lt_rsp, st_rf_rsp, st_rt_rsp;

    // Subordinate-side flit fan-out, per station.
    wire [NS-1:0]      e_valid, e_ready;
    wire [4*NSUS-1:0]  e_src;
    wire [4*TAGW-1:0]  e_tag;
    wire [3:0]         e_wr, e_head, e_last;
    wire [4*AW-1:0]    e_addr;
    wire [4*8-1:0]     e_len;
    wire [4*3-1:0]     e_size;
    wire [4*FW-1:0]    e_data;
    wire [4*(FW/8)-1:0] e_strb;

    wire [NS-1:0]      p_valid, p_ready, p_wr, p_last;
    wire [NS*NSUS-1:0] p_dst;
    wire [NS*TAGW-1:0] p_tag;
    wire [NS*2-1:0]    p_resp;
    wire [NS*FW-1:0]   p_data;

    // Only station 1 has real managers, but every station instantiates one
    // injection port so the hub is uniform; the rest tie off.
    wire [3:0]         d_wr, d_last;
    wire [4*TAGW-1:0]  d_tag;
    wire [4*2-1:0]     d_resp;
    wire [4*FW-1:0]    d_data;

    genvar s, i;
    generate
    for (s = 0; s < 4; s = s + 1) begin : g_stn
        localparam integer SNM = (s == 1) ? NM : 1;
        localparam integer S_OST = (s == 0) ? OST0 : (s == 1) ? OST1 :
                                  (s == 2) ? OST2 : OST3;
        localparam integer S_SFW = (s == 0) ? SFW0 : (s == 1) ? SFW1 :
                                  (s == 2) ? SFW2 : SFW3;
        localparam integer S_LPB = (s == 0) ? LPB0 : (s == 1) ? LPB1 :
                                  (s == 2) ? LPB2 : LPB3;
        localparam integer S_TMO = (s == 0) ? TMO0 : (s == 1) ? TMO1 :
                                  (s == 2) ? TMO2 : TMO3;

        wire [SNM-1:0]         sq_valid, sq_ready, sq_wr, sq_head, sq_last;
        wire [SNM*STNW-1:0]    sq_dstn;
        wire [SNM*PORTW-1:0]   sq_dprt;
        wire [SNM*TAGW-1:0]    sq_tag;
        wire [SNM*AW-1:0]      sq_addr;
        wire [SNM*8-1:0]       sq_len;
        wire [SNM*3-1:0]       sq_size;
        wire [SNM*FW-1:0]      sq_data;
        wire [SNM*(FW/8)-1:0]  sq_strb;
        wire [SNM-1:0]         sd_valid, sd_ready;

        sb_stn_line #(.FW(FW), .AW(AW), .TAGW(TAGW), .NSTN(4), .STN(s),
                      .NM(SNM), .NQ(NQ), .STNW(STNW), .PORTW(PORTW),
                      .SRCW(SRCW), .RQW(RQW), .RSW(RSW)) u_stn (
            .clk(bclk[s]), .rst(brst[s]),
            .nm_req_valid(sq_valid), .nm_req_ready(sq_ready),
            .nm_req_dstn(sq_dstn), .nm_req_dport(sq_dprt),
            .nm_req_tag(sq_tag), .nm_req_wr(sq_wr), .nm_req_head(sq_head),
            .nm_req_last(sq_last), .nm_req_addr(sq_addr), .nm_req_len(sq_len),
            .nm_req_size(sq_size), .nm_req_data(sq_data),
            .nm_req_strb(sq_strb),

            .ns_req_valid(e_valid[s*NQ +: NQ]),
            .ns_req_ready(e_ready[s*NQ +: NQ]),
            .ns_req_src(e_src[s*NSUS +: NSUS]),
            .ns_req_tag(e_tag[s*TAGW +: TAGW]),
            .ns_req_wr(e_wr[s]), .ns_req_head(e_head[s]),
            .ns_req_last(e_last[s]), .ns_req_addr(e_addr[s*AW +: AW]),
            .ns_req_len(e_len[s*8 +: 8]), .ns_req_size(e_size[s*3 +: 3]),
            .ns_req_data(e_data[s*FW +: FW]),
            .ns_req_strb(e_strb[s*(FW/8) +: FW/8]),

            .ns_rsp_valid(p_valid[s*NQ +: NQ]),
            .ns_rsp_ready(p_ready[s*NQ +: NQ]),
            .ns_rsp_dst(p_dst[s*NQ*NSUS +: NQ*NSUS]),
            .ns_rsp_tag(p_tag[s*NQ*TAGW +: NQ*TAGW]),
            .ns_rsp_wr(p_wr[s*NQ +: NQ]), .ns_rsp_last(p_last[s*NQ +: NQ]),
            .ns_rsp_resp(p_resp[s*NQ*2 +: NQ*2]),
            .ns_rsp_data(p_data[s*NQ*FW +: NQ*FW]),

            .nm_rsp_valid(sd_valid), .nm_rsp_ready(sd_ready),
            .nm_rsp_tag(d_tag[s*TAGW +: TAGW]), .nm_rsp_wr(d_wr[s]),
            .nm_rsp_last(d_last[s]), .nm_rsp_resp(d_resp[s*2 +: 2]),
            .nm_rsp_data(d_data[s*FW +: FW]),

            .lf_req_valid(st_lf_rqv[s]), .lf_req_ready(st_lf_rqr[s]),
            .lf_req_pay(st_lf_rqp[s*RQW +: RQW]),
            .lt_req_valid(st_lt_rqv[s]), .lt_req_ready(st_lt_rqr[s]),
            .lt_req_pay(st_lt_rqp[s*RQW +: RQW]),
            .lf_rsp_valid(st_lf_rsv[s]), .lf_rsp_ready(st_lf_rsr[s]),
            .lf_rsp_pay(st_lf_rsp[s*RSW +: RSW]),
            .lt_rsp_valid(st_lt_rsv[s]), .lt_rsp_ready(st_lt_rsr[s]),
            .lt_rsp_pay(st_lt_rsp[s*RSW +: RSW]),

            .rf_req_valid(st_rf_rqv[s]), .rf_req_ready(st_rf_rqr[s]),
            .rf_req_pay(st_rf_rqp[s*RQW +: RQW]),
            .rt_req_valid(st_rt_rqv[s]), .rt_req_ready(st_rt_rqr[s]),
            .rt_req_pay(st_rt_rqp[s*RQW +: RQW]),
            .rf_rsp_valid(st_rf_rsv[s]), .rf_rsp_ready(st_rf_rsr[s]),
            .rf_rsp_pay(st_rf_rsp[s*RSW +: RSW]),
            .rt_rsp_valid(st_rt_rsv[s]), .rt_rsp_ready(st_rt_rsr[s]),
            .rt_rsp_pay(st_rt_rsp[s*RSW +: RSW]),
            .stat_inj_flits(), .stat_inj_wait()
        );

        // ---- managers: real NMUs on station 1, tied off elsewhere ---------
        if (s == 1) begin : g_mgr
            wire [31:0] dc [0:NM-1];
            for (i = 0; i < NM; i = i + 1) begin : g_nmu
                // Manager 0 (JTAG) is 64-bit: the driver's word. 16-deep
                // FIFOs wedged every burst over 16 beats on v6.5 hardware.
                localparam integer MW = (i == 1) ? 512 : ((i == 0) ? 64 : 32);
                sb_nmu #(.MW(MW), .MIDW(MAXID), .AW(AW), .FW(FW), .TAGW(TAGW),
                         .DSTW(DPW), .LUT_PER_BRAM(S_LPB),
                         .STORE_FWD(S_SFW), .NSEG(NSEG),
                         .REQ_DEPTH((i == 2) ? 16 : 64),
                         .RSP_DEPTH((i == 2) ? 16 : 64),
                         // Manager 2 stays a single-beat control port.
                         .MAX_BURST((i == 2) ? 1 : 0),
                         .SEG_BASE(L_BASE), .SEG_MASK(L_MASK),
                         .SEG_XLT(L_XLT), .SEG_DST(L_STN),
                         .SEG_DPORT(L_PRT), .SEG_VLD(L_VLD)) u_nmu (
                    .s_aclk((i == 0) ? clk_ctrl : clk_xdma),
                    .s_aresetn((i == 0) ? aresetn_ctrl : aresetn_xdma),
                    .s_awid(mp_awid[i*MAXID +: MAXID]),
                    .s_awaddr(mp_awaddr[i*AW +: AW]),
                    .s_awlen(mp_awlen[i*8 +: 8]),
                    .s_awsize(mp_awsize[i*3 +: 3]),
                    .s_awburst(mp_awburst[i*2 +: 2]),
                    .s_awvalid(mp_awvalid[i]), .s_awready(mp_awready[i]),
                    .s_wdata(mp_wdata[i*MAXW +: MW]),
                    .s_wstrb(mp_wstrb[i*(MAXW/8) +: MW/8]),
                    .s_wlast(mp_wlast[i]), .s_wvalid(mp_wvalid[i]),
                    .s_wready(mp_wready[i]),
                    .s_bid(mp_bid[i*MAXID +: MAXID]),
                    .s_bresp(mp_bresp[i*2 +: 2]),
                    .s_bvalid(mp_bvalid[i]), .s_bready(mp_bready[i]),
                    .s_arid(mp_arid[i*MAXID +: MAXID]),
                    .s_araddr(mp_araddr[i*AW +: AW]),
                    .s_arlen(mp_arlen[i*8 +: 8]),
                    .s_arsize(mp_arsize[i*3 +: 3]),
                    .s_arburst(mp_arburst[i*2 +: 2]),
                    .s_arvalid(mp_arvalid[i]), .s_arready(mp_arready[i]),
                    .s_rid(mp_rid[i*MAXID +: MAXID]),
                    .s_rdata(mp_rdata[i*MAXW +: MW]),
                    .s_rresp(mp_rresp[i*2 +: 2]), .s_rlast(mp_rlast[i]),
                    .s_rvalid(mp_rvalid[i]), .s_rready(mp_rready[i]),
                    .bus_clk(bclk[s]), .bus_rst(brst[s]),
                    .req_valid(sq_valid[i]), .req_ready(sq_ready[i]),
                    .req_dst(sq_dstn[i*STNW +: STNW]),
                    .req_dport(sq_dprt[i*PORTW +: PORTW]),
                    .req_tag(sq_tag[i*TAGW +: TAGW]),
                    .req_wr(sq_wr[i]), .req_head(sq_head[i]),
                    .req_last(sq_last[i]),
                    .req_addr(sq_addr[i*AW +: AW]),
                    .req_len(sq_len[i*8 +: 8]),
                    .req_size(sq_size[i*3 +: 3]),
                    .req_data(sq_data[i*FW +: FW]),
                    .req_strb(sq_strb[i*(FW/8) +: FW/8]),
                    .rsp_valid(sd_valid[i]), .rsp_ready(sd_ready[i]),
                    .rsp_tag(d_tag[s*TAGW +: TAGW]), .rsp_wr(d_wr[s]),
                    .rsp_last(d_last[s]), .rsp_resp(d_resp[s*2 +: 2]),
                    .rsp_data(d_data[s*FW +: FW]),
                    .stat_decerr(dc[i])
                );
                if (MW < MAXW) begin : g_pad
                    assign mp_rdata[i*MAXW + MW +: MAXW-MW] =
                           {(MAXW-MW){1'b0}};
                end
            end
            assign stat_decerr = dc[0] + dc[1] + dc[2];
        end else begin : g_nomgr
            assign sq_valid = 1'b0;
            assign sq_dstn  = {STNW{1'b0}};
            assign sq_dprt  = {PORTW{1'b0}};
            assign sq_tag   = {TAGW{1'b0}};
            assign sq_wr    = 1'b0;
            assign sq_head  = 1'b0;
            assign sq_last  = 1'b0;
            assign sq_addr  = {AW{1'b0}};
            assign sq_len   = 8'd0;
            assign sq_size  = 3'd0;
            assign sq_data  = {FW{1'b0}};
            assign sq_strb  = {(FW/8){1'b0}};
            assign sd_ready = 1'b1;
        end

        // ---- local subordinates -------------------------------------------
        for (i = 0; i < NQ; i = i + 1) begin : g_nsu
            localparam integer K  = s*NQ + i;
            localparam integer DW = (i == 0) ? WIDE_DW : 32;
            localparam integer DOM = port_dom(i);
            wire ep_clk  = (DOM == 0) ? mclk[s]  : (DOM == 1) ? dclk[s]
                                                             : clk_ctrl;
            wire ep_rstn = (DOM == 0) ? mrstn[s] : (DOM == 1) ? drstn[s]
                                                             : aresetn_ctrl;
            if (DW < MAXW) begin : g_pad
                assign sp_wdata[K*MAXW + DW +: MAXW-DW] = {(MAXW-DW){1'b0}};
                assign sp_wstrb[K*(MAXW/8) + DW/8 +: (MAXW-DW)/8] =
                       {((MAXW-DW)/8){1'b0}};
            end
            sb_nsu #(.SDW(DW), .SIDW(MAXID), .AW(AW), .FW(FW), .TAGW(TAGW),
                     .SRCW(NSUS), .WOST(S_OST), .ROST(S_OST), .TIMEOUT(S_TMO),
                     .LUT_PER_BRAM(S_LPB),
                     .REQ_DEPTH(16), .RSP_DEPTH(16)) u_nsu (
                .bus_clk(bclk[s]), .bus_rst(brst[s]),
                .req_valid(e_valid[K]), .req_ready(e_ready[K]),
                .req_src(e_src[s*NSUS +: NSUS]),
                .req_tag(e_tag[s*TAGW +: TAGW]),
                .req_wr(e_wr[s]), .req_head(e_head[s]), .req_last(e_last[s]),
                .req_addr(e_addr[s*AW +: AW]), .req_len(e_len[s*8 +: 8]),
                .req_size(e_size[s*3 +: 3]), .req_data(e_data[s*FW +: FW]),
                .req_strb(e_strb[s*(FW/8) +: FW/8]),
                .rsp_valid(p_valid[K]), .rsp_ready(p_ready[K]),
                .rsp_dst(p_dst[K*NSUS +: NSUS]),
                .rsp_tag(p_tag[K*TAGW +: TAGW]),
                .rsp_wr(p_wr[K]), .rsp_last(p_last[K]),
                .rsp_resp(p_resp[K*2 +: 2]), .rsp_data(p_data[K*FW +: FW]),
                .m_aclk(ep_clk), .m_aresetn(ep_rstn),
                .m_awid(sp_awid[K*MAXID +: MAXID]),
                .m_awaddr(sp_awaddr[K*AW +: AW]),
                .m_awlen(sp_awlen[K*8 +: 8]),
                .m_awsize(sp_awsize[K*3 +: 3]),
                .m_awburst(sp_awburst[K*2 +: 2]),
                .m_awvalid(sp_awvalid[K]), .m_awready(sp_awready[K]),
                .m_wdata(sp_wdata[K*MAXW +: DW]),
                .m_wstrb(sp_wstrb[K*(MAXW/8) +: DW/8]),
                .m_wlast(sp_wlast[K]), .m_wvalid(sp_wvalid[K]),
                .m_wready(sp_wready[K]),
                .m_bid(sp_bid[K*MAXID +: MAXID]),
                .m_bresp(sp_bresp[K*2 +: 2]),
                .m_bvalid(sp_bvalid[K]), .m_bready(sp_bready[K]),
                .m_arid(sp_arid[K*MAXID +: MAXID]),
                .m_araddr(sp_araddr[K*AW +: AW]),
                .m_arlen(sp_arlen[K*8 +: 8]),
                .m_arsize(sp_arsize[K*3 +: 3]),
                .m_arburst(sp_arburst[K*2 +: 2]),
                .m_arvalid(sp_arvalid[K]), .m_arready(sp_arready[K]),
                .m_rid(sp_rid[K*MAXID +: MAXID]),
                .m_rdata(sp_rdata[K*MAXW +: DW]),
                .m_rresp(sp_rresp[K*2 +: 2]), .m_rlast(sp_rlast[K]),
                .m_rvalid(sp_rvalid[K]), .m_rready(sp_rready[K])
            );
        end
    end
    endgenerate

    // ================================================== links between stations
    // Four streams per boundary; managers on ONE station use only two.
    generate
    for (s = 0; s < 3; s = s + 1) begin : g_link
        // Managers left of this boundary use REQ-right and RSP-left; managers
        // right of it use the other pair. Only one pair is ever live.
        localparam integer NEED_R = LINK_FULL || (s >= MGR_STN);
        localparam integer NEED_L = LINK_FULL || (s <  MGR_STN);

        if (NEED_R) begin : g_r
            if (LINK_CDC) begin : g_cdc
                sb_link_cdc #(.W(RQW), .PIPE(PIPE), .CRED(CRED)) u_rq_fwd (
                    .i_clk(bclk[s]), .i_rst(brst[s]),
                    .i_valid(st_rt_rqv[s]), .i_ready(st_rt_rqr[s]),
                    .i_data(st_rt_rqp[s*RQW +: RQW]),
                    .o_clk(bclk[s+1]), .o_rst(brst[s+1]),
                    .o_valid(st_lf_rqv[s+1]), .o_ready(st_lf_rqr[s+1]),
                    .o_data(st_lf_rqp[(s+1)*RQW +: RQW]),
                    .stat_sent(), .stat_nocred());

                sb_link_cdc #(.W(RSW), .PIPE(PIPE), .CRED(CRED)) u_rs_bwd (
                    .i_clk(bclk[s+1]), .i_rst(brst[s+1]),
                    .i_valid(st_lt_rsv[s+1]), .i_ready(st_lt_rsr[s+1]),
                    .i_data(st_lt_rsp[(s+1)*RSW +: RSW]),
                    .o_clk(bclk[s]), .o_rst(brst[s]),
                    .o_valid(st_rf_rsv[s]), .o_ready(st_rf_rsr[s]),
                    .o_data(st_rf_rsp[s*RSW +: RSW]),
                    .stat_sent(), .stat_nocred());
            end else begin : g_sync
                sb_link #(.W(RQW), .PIPE(PIPE), .CRED(CRED)) u_rq_fwd (
                    .clk(bclk[0]), .rst(brst[0]),
                    .i_valid(st_rt_rqv[s]), .i_ready(st_rt_rqr[s]),
                    .i_data(st_rt_rqp[s*RQW +: RQW]),
                    .o_valid(st_lf_rqv[s+1]), .o_ready(st_lf_rqr[s+1]),
                    .o_data(st_lf_rqp[(s+1)*RQW +: RQW]));

                sb_link #(.W(RSW), .PIPE(PIPE), .CRED(CRED)) u_rs_bwd (
                    .clk(bclk[0]), .rst(brst[0]),
                    .i_valid(st_lt_rsv[s+1]), .i_ready(st_lt_rsr[s+1]),
                    .i_data(st_lt_rsp[(s+1)*RSW +: RSW]),
                    .o_valid(st_rf_rsv[s]), .o_ready(st_rf_rsr[s]),
                    .o_data(st_rf_rsp[s*RSW +: RSW]));
            end
        end else begin : g_r_off
            assign st_rt_rqr[s]                = 1'b1;
            assign st_lf_rqv[s+1]              = 1'b0;
            assign st_lf_rqp[(s+1)*RQW +: RQW] = {RQW{1'b0}};
            assign st_lt_rsr[s+1]              = 1'b1;
            assign st_rf_rsv[s]                = 1'b0;
            assign st_rf_rsp[s*RSW +: RSW]     = {RSW{1'b0}};
        end

        if (NEED_L) begin : g_l
            if (LINK_CDC) begin : g_cdc
                sb_link_cdc #(.W(RQW), .PIPE(PIPE), .CRED(CRED)) u_rq_bwd (
                    .i_clk(bclk[s+1]), .i_rst(brst[s+1]),
                    .i_valid(st_lt_rqv[s+1]), .i_ready(st_lt_rqr[s+1]),
                    .i_data(st_lt_rqp[(s+1)*RQW +: RQW]),
                    .o_clk(bclk[s]), .o_rst(brst[s]),
                    .o_valid(st_rf_rqv[s]), .o_ready(st_rf_rqr[s]),
                    .o_data(st_rf_rqp[s*RQW +: RQW]),
                    .stat_sent(), .stat_nocred());

                sb_link_cdc #(.W(RSW), .PIPE(PIPE), .CRED(CRED)) u_rs_fwd (
                    .i_clk(bclk[s]), .i_rst(brst[s]),
                    .i_valid(st_rt_rsv[s]), .i_ready(st_rt_rsr[s]),
                    .i_data(st_rt_rsp[s*RSW +: RSW]),
                    .o_clk(bclk[s+1]), .o_rst(brst[s+1]),
                    .o_valid(st_lf_rsv[s+1]), .o_ready(st_lf_rsr[s+1]),
                    .o_data(st_lf_rsp[(s+1)*RSW +: RSW]),
                    .stat_sent(), .stat_nocred());
            end else begin : g_sync
                sb_link #(.W(RQW), .PIPE(PIPE), .CRED(CRED)) u_rq_bwd (
                    .clk(bclk[0]), .rst(brst[0]),
                    .i_valid(st_lt_rqv[s+1]), .i_ready(st_lt_rqr[s+1]),
                    .i_data(st_lt_rqp[(s+1)*RQW +: RQW]),
                    .o_valid(st_rf_rqv[s]), .o_ready(st_rf_rqr[s]),
                    .o_data(st_rf_rqp[s*RQW +: RQW]));

                sb_link #(.W(RSW), .PIPE(PIPE), .CRED(CRED)) u_rs_fwd (
                    .clk(bclk[0]), .rst(brst[0]),
                    .i_valid(st_rt_rsv[s]), .i_ready(st_rt_rsr[s]),
                    .i_data(st_rt_rsp[s*RSW +: RSW]),
                    .o_valid(st_lf_rsv[s+1]), .o_ready(st_lf_rsr[s+1]),
                    .o_data(st_lf_rsp[(s+1)*RSW +: RSW]));
            end
        end else begin : g_l_off
            assign st_lt_rqr[s+1]              = 1'b1;
            assign st_rf_rqv[s]                = 1'b0;
            assign st_rf_rqp[s*RQW +: RQW]     = {RQW{1'b0}};
            assign st_rt_rsr[s]                = 1'b1;
            assign st_lf_rsv[s+1]              = 1'b0;
            assign st_lf_rsp[(s+1)*RSW +: RSW] = {RSW{1'b0}};
        end
    end
    endgenerate

    // The ends of the line have no neighbour.
    assign st_lf_rqv[0] = 1'b0;
    assign st_lf_rqp[0 +: RQW] = {RQW{1'b0}};
    assign st_lt_rqr[0] = 1'b1;
    assign st_lf_rsv[0] = 1'b0;
    assign st_lf_rsp[0 +: RSW] = {RSW{1'b0}};
    assign st_lt_rsr[0] = 1'b1;

    assign st_rf_rqv[3] = 1'b0;
    assign st_rf_rqp[3*RQW +: RQW] = {RQW{1'b0}};
    assign st_rt_rqr[3] = 1'b1;
    assign st_rf_rsv[3] = 1'b0;
    assign st_rf_rsp[3*RSW +: RSW] = {RSW{1'b0}};
    assign st_rt_rsr[3] = 1'b1;
endmodule

`default_nettype wire

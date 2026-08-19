// Two stations joined by a link: 3 managers and 2 subordinates at A, 3 more at
// B -- the link, the cross-station route, and the return path home.

// A's hub treats the link as destination index NLA; B routes on the flit's
// `dport` and forwards the originating `src` rather than stamping its own.

`default_nettype none

module sb_chain2 #(
    parameter integer FW    = 512,
    parameter integer AW    = 40,
    parameter integer MAXW  = 512,
    parameter integer MAXID = 4,
    parameter integer NM    = 3,
    parameter integer NLA   = 2,                // subordinates at A
    parameter integer NLB   = 3,                // subordinates at B
    parameter integer NS    = NLA + NLB,
    parameter integer TAGW  = 4,
    parameter integer DSTW  = 3,
    parameter integer SRCW  = 2,
    parameter integer OST   = 4,
    parameter integer STORE_FWD = 1,
    parameter integer LUT_PER_BRAM = 820,
    parameter integer PIPE  = 4,
    parameter integer CRED  = 16
)(
    input  wire                 bus_clk,
    input  wire                 bus_rst,
    input  wire                 clk_ctrl,   input wire aresetn_ctrl,
    input  wire                 clk_xdma,   input wire aresetn_xdma,
    input  wire                 clk_mesh,   input wire aresetn_mesh,
    input  wire                 clk_ddr,    input wire aresetn_ddr,

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
    localparam integer NDA    = NLA + 1;        // A: locals plus the link
    localparam integer LNK_RQ = SRCW + DSTW + TAGW + 3 + AW + 8 + 3 + FW + FW/8;
    localparam integer LNK_RS = SRCW + TAGW + 2 + 2 + FW;

    localparam [AW-1:0] MSK_MESH = 40'hB0_0000_0000;
    localparam [AW-1:0] MSK_64K  = 40'hFF_FFFF_0000;
    localparam [AW-1:0] B_A0 = 40'h80_0000_0000, B_A1 = 40'h00_0081_0000;
    localparam [AW-1:0] B_B0 = 40'h90_0000_0000, B_B1 = 40'h00_0090_0000;
    localparam [AW-1:0] B_B2 = 40'h00_0091_0000;

    // Remote targets route to A's link (NLA) but carry the far port in DPORT.
    localparam integer NSEG_ALL = 5;
    localparam [NSEG_ALL*AW-1:0]   ALL_BASE = {B_B2, B_B1, B_B0, B_A1, B_A0};
    localparam [NSEG_ALL*AW-1:0]   ALL_MASK = {MSK_64K, MSK_64K, MSK_MESH,
                                               MSK_64K, MSK_MESH};
    localparam [NSEG_ALL*DSTW-1:0] ALL_DST  = {3'd2, 3'd2, 3'd2, 3'd1, 3'd0};
    localparam [NSEG_ALL*DSTW-1:0] ALL_DPT  = {3'd2, 3'd1, 3'd0, 3'd1, 3'd0};

    // =============================================================== station A
    wire [NM-1:0]        qa_valid, qa_ready, qa_wr, qa_head, qa_last;
    wire [NM*DSTW-1:0]   qa_dst, qa_dpt;
    wire [NM*TAGW-1:0]   qa_tag;
    wire [NM*AW-1:0]     qa_addr;
    wire [NM*8-1:0]      qa_len;
    wire [NM*3-1:0]      qa_size;
    wire [NM*FW-1:0]     qa_data;
    wire [NM*(FW/8)-1:0] qa_strb;

    wire [NDA-1:0]      ea_valid, ea_ready;
    wire [SRCW-1:0]     ea_src;
    wire [DSTW-1:0]     ea_dpt;
    wire [TAGW-1:0]     ea_tag;
    wire                ea_wr, ea_head, ea_last;
    wire [AW-1:0]       ea_addr;
    wire [7:0]          ea_len;
    wire [2:0]          ea_size;
    wire [FW-1:0]       ea_data;
    wire [FW/8-1:0]     ea_strb;

    wire [NDA-1:0]      pa_valid, pa_ready, pa_wr, pa_last;
    wire [NDA*SRCW-1:0] pa_dst;
    wire [NDA*TAGW-1:0] pa_tag;
    wire [NDA*2-1:0]    pa_resp;
    wire [NDA*FW-1:0]   pa_data;

    wire [NM-1:0]       da_valid, da_ready;
    wire [TAGW-1:0]     da_tag;
    wire                da_wr, da_last;
    wire [1:0]          da_resp;
    wire [FW-1:0]       da_data;

    sb_station #(.NM(NM), .NS(NDA), .FW(FW), .AW(AW), .TAGW(TAGW),
                 .DSTW(DSTW), .SRCW(SRCW)) u_stn_a (
        .clk(bus_clk), .rst(bus_rst),
        .nm_req_valid(qa_valid), .nm_req_ready(qa_ready), .nm_req_dst(qa_dst),
        .nm_req_dport(qa_dpt), .nm_req_src({NM*SRCW{1'b0}}),
        .nm_req_tag(qa_tag), .nm_req_wr(qa_wr), .nm_req_head(qa_head),
        .nm_req_last(qa_last), .nm_req_addr(qa_addr), .nm_req_len(qa_len),
        .nm_req_size(qa_size), .nm_req_data(qa_data), .nm_req_strb(qa_strb),
        .ns_req_valid(ea_valid), .ns_req_ready(ea_ready), .ns_req_src(ea_src),
        .ns_req_dport(ea_dpt), .ns_req_tag(ea_tag), .ns_req_wr(ea_wr),
        .ns_req_head(ea_head), .ns_req_last(ea_last), .ns_req_addr(ea_addr),
        .ns_req_len(ea_len), .ns_req_size(ea_size), .ns_req_data(ea_data),
        .ns_req_strb(ea_strb),
        .ns_rsp_valid(pa_valid), .ns_rsp_ready(pa_ready), .ns_rsp_dst(pa_dst),
        .ns_rsp_tag(pa_tag), .ns_rsp_wr(pa_wr), .ns_rsp_last(pa_last),
        .ns_rsp_resp(pa_resp), .ns_rsp_data(pa_data),
        .nm_rsp_valid(da_valid), .nm_rsp_ready(da_ready), .nm_rsp_dst(),
        .nm_rsp_tag(da_tag), .nm_rsp_wr(da_wr), .nm_rsp_last(da_last),
        .nm_rsp_resp(da_resp), .nm_rsp_data(da_data)
    );

    wire [31:0] dc0, dc1, dc2;
    assign stat_decerr = dc0 + dc1 + dc2;
    assign mp_rdata[0*MAXW + 32 +: MAXW-32] = {(MAXW-32){1'b0}};
    assign mp_rdata[2*MAXW + 32 +: MAXW-32] = {(MAXW-32){1'b0}};

`define CH2_FLIT(I) \
        .req_valid(qa_valid[I]), .req_ready(qa_ready[I]), \
        .req_dst(qa_dst[(I)*DSTW +: DSTW]), \
        .req_dport(qa_dpt[(I)*DSTW +: DSTW]), \
        .req_tag(qa_tag[(I)*TAGW +: TAGW]), \
        .req_wr(qa_wr[I]), .req_head(qa_head[I]), .req_last(qa_last[I]), \
        .req_addr(qa_addr[(I)*AW +: AW]), .req_len(qa_len[(I)*8 +: 8]), \
        .req_size(qa_size[(I)*3 +: 3]), .req_data(qa_data[(I)*FW +: FW]), \
        .req_strb(qa_strb[(I)*(FW/8) +: FW/8]), \
        .rsp_valid(da_valid[I]), .rsp_ready(da_ready[I]), .rsp_tag(da_tag), \
        .rsp_wr(da_wr), .rsp_last(da_last), .rsp_resp(da_resp), \
        .rsp_data(da_data)

`define CH2_AXI(I, W) \
        .s_awid(mp_awid[(I)*MAXID +: MAXID]), \
        .s_awaddr(mp_awaddr[(I)*AW +: AW]), \
        .s_awlen(mp_awlen[(I)*8 +: 8]), .s_awsize(mp_awsize[(I)*3 +: 3]), \
        .s_awburst(mp_awburst[(I)*2 +: 2]), .s_awvalid(mp_awvalid[I]), \
        .s_awready(mp_awready[I]), \
        .s_wdata(mp_wdata[(I)*MAXW +: W]), \
        .s_wstrb(mp_wstrb[(I)*(MAXW/8) +: (W)/8]), \
        .s_wlast(mp_wlast[I]), .s_wvalid(mp_wvalid[I]), \
        .s_wready(mp_wready[I]), \
        .s_bid(mp_bid[(I)*MAXID +: MAXID]), .s_bresp(mp_bresp[(I)*2 +: 2]), \
        .s_bvalid(mp_bvalid[I]), .s_bready(mp_bready[I]), \
        .s_arid(mp_arid[(I)*MAXID +: MAXID]), \
        .s_araddr(mp_araddr[(I)*AW +: AW]), \
        .s_arlen(mp_arlen[(I)*8 +: 8]), .s_arsize(mp_arsize[(I)*3 +: 3]), \
        .s_arburst(mp_arburst[(I)*2 +: 2]), .s_arvalid(mp_arvalid[I]), \
        .s_arready(mp_arready[I]), \
        .s_rid(mp_rid[(I)*MAXID +: MAXID]), \
        .s_rdata(mp_rdata[(I)*MAXW +: W]), .s_rresp(mp_rresp[(I)*2 +: 2]), \
        .s_rlast(mp_rlast[I]), .s_rvalid(mp_rvalid[I]), .s_rready(mp_rready[I])

`define CH2_NMU(W, DEP, RDEP) \
        .MW(W), .MIDW(MAXID), .AW(AW), .FW(FW), .TAGW(TAGW), .DSTW(DSTW), \
        .LUT_PER_BRAM(LUT_PER_BRAM), .STORE_FWD(STORE_FWD), \
        .NSEG(NSEG_ALL), .REQ_DEPTH(DEP), .RSP_DEPTH(RDEP), \
        .SEG_BASE(ALL_BASE), .SEG_MASK(ALL_MASK), .SEG_XLT(ALL_BASE), \
        .SEG_DST(ALL_DST), .SEG_DPORT(ALL_DPT), .SEG_VLD({NSEG_ALL{1'b1}})

    sb_nmu #(`CH2_NMU(32, 16, 256)) u_nmu0 (
        .s_aclk(clk_ctrl), .s_aresetn(aresetn_ctrl),
        `CH2_AXI(0, 32), .bus_clk(bus_clk), .bus_rst(bus_rst),
        `CH2_FLIT(0), .stat_decerr(dc0));

    sb_nmu #(`CH2_NMU(512, 64, 64)) u_nmu1 (
        .s_aclk(clk_xdma), .s_aresetn(aresetn_xdma),
        `CH2_AXI(1, 512), .bus_clk(bus_clk), .bus_rst(bus_rst),
        `CH2_FLIT(1), .stat_decerr(dc1));

    sb_nmu #(`CH2_NMU(32, 16, 16)) u_nmu2 (
        .s_aclk(clk_xdma), .s_aresetn(aresetn_xdma),
        `CH2_AXI(2, 32), .bus_clk(bus_clk), .bus_rst(bus_rst),
        `CH2_FLIT(2), .stat_decerr(dc2));

    // =================================================================== link
    wire                lq_valid, lq_ready;
    wire [LNK_RQ-1:0]   lq_data;
    wire                lqb_valid, lqb_ready;
    wire [LNK_RQ-1:0]   lqb_data;

    assign lq_valid    = ea_valid[NLA];
    assign ea_ready[NLA] = lq_ready;
    assign lq_data     = {ea_src, ea_dpt, ea_tag, ea_wr, ea_head, ea_last,
                          ea_addr, ea_len, ea_size, ea_data, ea_strb};

    sb_link #(.W(LNK_RQ), .PIPE(PIPE), .CRED(CRED)) u_lk_req (
        .clk(bus_clk), .rst(bus_rst),
        .i_valid(lq_valid), .i_ready(lq_ready), .i_data(lq_data),
        .o_valid(lqb_valid), .o_ready(lqb_ready), .o_data(lqb_data));

    wire [SRCW-1:0] qb_src;
    wire [DSTW-1:0] qb_dpt;
    wire [TAGW-1:0] qb_tag;
    wire            qb_wr, qb_head, qb_last;
    wire [AW-1:0]   qb_addr;
    wire [7:0]      qb_len;
    wire [2:0]      qb_size;
    wire [FW-1:0]   qb_data;
    wire [FW/8-1:0] qb_strb;

    assign {qb_src, qb_dpt, qb_tag, qb_wr, qb_head, qb_last, qb_addr, qb_len,
            qb_size, qb_data, qb_strb} = lqb_data;

    wire                ls_valid, ls_ready;
    wire [LNK_RS-1:0]   ls_data;
    wire                lsa_valid, lsa_ready;
    wire [LNK_RS-1:0]   lsa_data;

    sb_link #(.W(LNK_RS), .PIPE(PIPE), .CRED(CRED)) u_lk_rsp (
        .clk(bus_clk), .rst(bus_rst),
        .i_valid(ls_valid), .i_ready(ls_ready), .i_data(ls_data),
        .o_valid(lsa_valid), .o_ready(lsa_ready), .o_data(lsa_data));

    assign pa_valid[NLA] = lsa_valid;
    assign lsa_ready     = pa_ready[NLA];
    assign {pa_dst[NLA*SRCW +: SRCW], pa_tag[NLA*TAGW +: TAGW], pa_wr[NLA],
            pa_last[NLA], pa_resp[NLA*2 +: 2],
            pa_data[NLA*FW +: FW]} = lsa_data;

    // =============================================================== station B
    wire [NLB-1:0]      eb_valid, eb_ready;
    wire [SRCW-1:0]     eb_src;
    wire [DSTW-1:0]     eb_dpt;
    wire [TAGW-1:0]     eb_tag;
    wire                eb_wr, eb_head, eb_last;
    wire [AW-1:0]       eb_addr;
    wire [7:0]          eb_len;
    wire [2:0]          eb_size;
    wire [FW-1:0]       eb_data;
    wire [FW/8-1:0]     eb_strb;

    wire [NLB-1:0]      pb_valid, pb_ready, pb_wr, pb_last;
    wire [NLB*SRCW-1:0] pb_dst;
    wire [NLB*TAGW-1:0] pb_tag;
    wire [NLB*2-1:0]    pb_resp;
    wire [NLB*FW-1:0]   pb_data;

    wire [SRCW-1:0]     db_dst;
    wire [TAGW-1:0]     db_tag;
    wire                db_wr, db_last;
    wire [1:0]          db_resp;
    wire [FW-1:0]       db_data;

    sb_station #(.NM(1), .NS(NLB), .FW(FW), .AW(AW), .TAGW(TAGW),
                 .DSTW(DSTW), .SRCW(SRCW), .SRC_PASS(1)) u_stn_b (
        .clk(bus_clk), .rst(bus_rst),
        .nm_req_valid(lqb_valid), .nm_req_ready(lqb_ready),
        .nm_req_dst(qb_dpt), .nm_req_dport(qb_dpt), .nm_req_src(qb_src),
        .nm_req_tag(qb_tag), .nm_req_wr(qb_wr), .nm_req_head(qb_head),
        .nm_req_last(qb_last), .nm_req_addr(qb_addr), .nm_req_len(qb_len),
        .nm_req_size(qb_size), .nm_req_data(qb_data), .nm_req_strb(qb_strb),
        .ns_req_valid(eb_valid), .ns_req_ready(eb_ready), .ns_req_src(eb_src),
        .ns_req_dport(eb_dpt), .ns_req_tag(eb_tag), .ns_req_wr(eb_wr),
        .ns_req_head(eb_head), .ns_req_last(eb_last), .ns_req_addr(eb_addr),
        .ns_req_len(eb_len), .ns_req_size(eb_size), .ns_req_data(eb_data),
        .ns_req_strb(eb_strb),
        .ns_rsp_valid(pb_valid), .ns_rsp_ready(pb_ready), .ns_rsp_dst(pb_dst),
        .ns_rsp_tag(pb_tag), .ns_rsp_wr(pb_wr), .ns_rsp_last(pb_last),
        .ns_rsp_resp(pb_resp), .ns_rsp_data(pb_data),
        .nm_rsp_valid(ls_valid), .nm_rsp_ready(ls_ready), .nm_rsp_dst(db_dst),
        .nm_rsp_tag(db_tag), .nm_rsp_wr(db_wr), .nm_rsp_last(db_last),
        .nm_rsp_resp(db_resp), .nm_rsp_data(db_data)
    );

    assign ls_data = {db_dst, db_tag, db_wr, db_last, db_resp, db_data};

    // ============================================================ subordinates
    wire [NS-1:0] sclk  = {clk_ctrl, clk_ctrl, clk_mesh, clk_ddr, clk_mesh};
    wire [NS-1:0] srstn = {aresetn_ctrl, aresetn_ctrl, aresetn_mesh,
                           aresetn_ddr, aresetn_mesh};

    genvar i;
    generate
    for (i = 0; i < NS; i = i + 1) begin : g_nsu
        localparam integer DW  = ((i == 0) || (i == NLA)) ? 512 : 32;
        localparam integer ATB = (i < NLA);          // this one hangs off A
        localparam integer LI  = ATB ? i : (i - NLA);

        if (DW < MAXW) begin : g_pad
            assign sp_wdata[i*MAXW + DW +: MAXW-DW] = {(MAXW-DW){1'b0}};
            assign sp_wstrb[i*(MAXW/8) + DW/8 +: (MAXW-DW)/8] =
                   {((MAXW-DW)/8){1'b0}};
        end

        wire        rqv, rqr, rsv, rsr;
        wire [SRCW-1:0] rsd;
        wire [TAGW-1:0] rst_;
        wire        rsw, rsl;
        wire [1:0]  rsp;
        wire [FW-1:0] rsdat;

        if (ATB) begin : g_at_a
            assign rqv = ea_valid[LI];
            assign ea_ready[LI] = rqr;
            assign pa_valid[LI] = rsv;
            assign rsr = pa_ready[LI];
            assign pa_dst [LI*SRCW +: SRCW] = rsd;
            assign pa_tag [LI*TAGW +: TAGW] = rst_;
            assign pa_wr  [LI] = rsw;
            assign pa_last[LI] = rsl;
            assign pa_resp[LI*2 +: 2] = rsp;
            assign pa_data[LI*FW +: FW] = rsdat;
        end else begin : g_at_b
            assign rqv = eb_valid[LI];
            assign eb_ready[LI] = rqr;
            assign pb_valid[LI] = rsv;
            assign rsr = pb_ready[LI];
            assign pb_dst [LI*SRCW +: SRCW] = rsd;
            assign pb_tag [LI*TAGW +: TAGW] = rst_;
            assign pb_wr  [LI] = rsw;
            assign pb_last[LI] = rsl;
            assign pb_resp[LI*2 +: 2] = rsp;
            assign pb_data[LI*FW +: FW] = rsdat;
        end

        sb_nsu #(.SDW(DW), .SIDW(MAXID), .AW(AW), .FW(FW), .TAGW(TAGW),
                 .SRCW(SRCW), .WOST(OST), .ROST(OST),
                 .LUT_PER_BRAM(LUT_PER_BRAM),
                 .REQ_DEPTH(16), .RSP_DEPTH(16)) u_nsu (
            .bus_clk(bus_clk), .bus_rst(bus_rst),
            .req_valid(rqv), .req_ready(rqr),
            .req_src (ATB ? ea_src  : eb_src),
            .req_tag (ATB ? ea_tag  : eb_tag),
            .req_wr  (ATB ? ea_wr   : eb_wr),
            .req_head(ATB ? ea_head : eb_head),
            .req_last(ATB ? ea_last : eb_last),
            .req_addr(ATB ? ea_addr : eb_addr),
            .req_len (ATB ? ea_len  : eb_len),
            .req_size(ATB ? ea_size : eb_size),
            .req_data(ATB ? ea_data : eb_data),
            .req_strb(ATB ? ea_strb : eb_strb),
            .rsp_valid(rsv), .rsp_ready(rsr), .rsp_dst(rsd), .rsp_tag(rst_),
            .rsp_wr(rsw), .rsp_last(rsl), .rsp_resp(rsp), .rsp_data(rsdat),
            .m_aclk(sclk[i]), .m_aresetn(srstn[i]),
            .m_awid(sp_awid[i*MAXID +: MAXID]),
            .m_awaddr(sp_awaddr[i*AW +: AW]), .m_awlen(sp_awlen[i*8 +: 8]),
            .m_awsize(sp_awsize[i*3 +: 3]), .m_awburst(sp_awburst[i*2 +: 2]),
            .m_awvalid(sp_awvalid[i]), .m_awready(sp_awready[i]),
            .m_wdata(sp_wdata[i*MAXW +: DW]),
            .m_wstrb(sp_wstrb[i*(MAXW/8) +: DW/8]),
            .m_wlast(sp_wlast[i]), .m_wvalid(sp_wvalid[i]),
            .m_wready(sp_wready[i]),
            .m_bid(sp_bid[i*MAXID +: MAXID]), .m_bresp(sp_bresp[i*2 +: 2]),
            .m_bvalid(sp_bvalid[i]), .m_bready(sp_bready[i]),
            .m_arid(sp_arid[i*MAXID +: MAXID]),
            .m_araddr(sp_araddr[i*AW +: AW]), .m_arlen(sp_arlen[i*8 +: 8]),
            .m_arsize(sp_arsize[i*3 +: 3]), .m_arburst(sp_arburst[i*2 +: 2]),
            .m_arvalid(sp_arvalid[i]), .m_arready(sp_arready[i]),
            .m_rid(sp_rid[i*MAXID +: MAXID]),
            .m_rdata(sp_rdata[i*MAXW +: DW]), .m_rresp(sp_rresp[i*2 +: 2]),
            .m_rlast(sp_rlast[i]), .m_rvalid(sp_rvalid[i]),
            .m_rready(sp_rready[i])
        );
    end
    endgenerate
endmodule

`default_nettype wire

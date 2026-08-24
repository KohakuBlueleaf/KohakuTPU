// SUPERSEDED by sb_line4.v: a STAR, so SLR1<->SLR2 carries two links and costs
// 2,344 cross-SLR wires against the line's 1,170.

// Four stations, one per SLR: a root holding the managers and its own local
// endpoints, plus three leaves reached over credited links.

// This is the validation topology -- {jtag, xdma, jtag-lite} into SLR1, two
// endpoints in every SLR, each SLR on its own local clock.

`default_nettype none

module sb_quad #(
    parameter integer FW    = 512,
    parameter integer AW    = 40,
    parameter integer MAXW  = 512,
    parameter integer MAXID = 4,
    parameter integer NM    = 3,
    parameter integer NPS   = 2,                // endpoints per station
    parameter integer NS    = 4 * NPS,
    parameter integer TAGW  = 4,
    parameter integer DSTW  = 3,
    parameter integer SRCW  = 2,
    parameter integer OST   = 4,
    parameter integer STORE_FWD    = 1,
    parameter integer LUT_PER_BRAM = 0,
    parameter integer TIMEOUT      = 0,
    parameter integer PIPE  = 4,
    parameter integer CRED  = 16
)(
    input  wire                 bus_clk,
    input  wire                 bus_rst,
    input  wire                 clk_ctrl,   input wire aresetn_ctrl,
    input  wire                 clk_xdma,   input wire aresetn_xdma,
    input  wire                 clk_s0,     input wire aresetn_s0,
    input  wire                 clk_s1,     input wire aresetn_s1,
    input  wire                 clk_s2,     input wire aresetn_s2,
    input  wire                 clk_s3,     input wire aresetn_s3,

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
    localparam integer NK     = 3;
    localparam integer LNK_RQ = SRCW + DSTW + TAGW + 3 + AW + 8 + 3 + FW + FW/8;
    localparam integer LNK_RS = SRCW + TAGW + 2 + 2 + FW;

    // Top 4 address bits are the SLR, bit 16 the endpoint: one 64K window each.
    // Derived from AW, not literal, so a 32-bit build gets the SAME map the
    // generated BD wrappers use and the simulation matches what is built.
    localparam [AW-1:0] MSK  = {{(AW-16){1'b1}}, 16'd0};
    localparam [AW-1:0] A_S0 = {4'd0, {(AW-4){1'b0}}};
    localparam [AW-1:0] A_S1 = {4'd1, {(AW-4){1'b0}}};
    localparam [AW-1:0] A_S2 = {4'd2, {(AW-4){1'b0}}};
    localparam [AW-1:0] A_S3 = {4'd3, {(AW-4){1'b0}}};
    localparam [AW-1:0] OFF1 = {{(AW-17){1'b0}}, 1'b1, 16'd0};

    localparam integer NSEG = 8;
    localparam [NSEG*AW-1:0] Q_BASE = { A_S3 + OFF1, A_S3, A_S2 + OFF1, A_S2, A_S1 + OFF1, A_S1, A_S0 + OFF1, A_S0 };
    localparam [NSEG*AW-1:0]   Q_MASK = {NSEG{MSK}};
    // Root ports: 0,1 local; 2 link SLR0; 3 link SLR2; 4 link SLR3.
    localparam [NSEG*DSTW-1:0] Q_DST = {3'd4, 3'd4, 3'd3, 3'd3, 3'd1, 3'd0, 3'd2, 3'd2};
    localparam [NSEG*DSTW-1:0] Q_DPT = {3'd1, 3'd0, 3'd1, 3'd0, 3'd1, 3'd0, 3'd1, 3'd0};

    wire [NK-1:0]      rq_valid, rq_ready;
    wire [DSTW-1:0]    rq_dport;
    wire [SRCW-1:0]    rq_src;
    wire [TAGW-1:0]    rq_tag;
    wire               rq_wr, rq_head, rq_last;
    wire [AW-1:0]      rq_addr;
    wire [7:0]         rq_len;
    wire [2:0]         rq_size;
    wire [FW-1:0]      rq_data;
    wire [FW/8-1:0]    rq_strb;

    wire [NK-1:0]      rs_valid, rs_ready;
    wire [NK*SRCW-1:0] rs_dst;
    wire [NK*TAGW-1:0] rs_tag;
    wire [NK-1:0]      rs_wr, rs_last;
    wire [NK*2-1:0]    rs_resp;
    wire [NK*FW-1:0]   rs_data;

    sb_stn_root #(.FW(FW), .AW(AW), .MAXW(MAXW), .MAXID(MAXID), .NM(NM),
                  .NL(NPS), .NK(NK), .TAGW(TAGW), .DSTW(DSTW), .SRCW(SRCW),
                  .OST(OST), .STORE_FWD(STORE_FWD),
                  .LUT_PER_BRAM(LUT_PER_BRAM), .TIMEOUT(TIMEOUT),
                  .LOC_W(32'h0000_0001), .NSEG(NSEG),
                  .SEG_BASE(Q_BASE), .SEG_MASK(Q_MASK),
                  .SEG_DST(Q_DST), .SEG_DPORT(Q_DPT)) u_root (
        .bus_clk(bus_clk), .bus_rst(bus_rst),
        .clk_ctrl(clk_ctrl), .aresetn_ctrl(aresetn_ctrl),
        .clk_xdma(clk_xdma), .aresetn_xdma(aresetn_xdma),
        .clk_loc(clk_s1), .aresetn_loc(aresetn_s1),

        .mp_awid(mp_awid), .mp_awaddr(mp_awaddr), .mp_awlen(mp_awlen),
        .mp_awsize(mp_awsize), .mp_awburst(mp_awburst),
        .mp_awvalid(mp_awvalid), .mp_awready(mp_awready),
        .mp_wdata(mp_wdata), .mp_wstrb(mp_wstrb), .mp_wlast(mp_wlast),
        .mp_wvalid(mp_wvalid), .mp_wready(mp_wready),
        .mp_bid(mp_bid), .mp_bresp(mp_bresp), .mp_bvalid(mp_bvalid),
        .mp_bready(mp_bready),
        .mp_arid(mp_arid), .mp_araddr(mp_araddr), .mp_arlen(mp_arlen),
        .mp_arsize(mp_arsize), .mp_arburst(mp_arburst),
        .mp_arvalid(mp_arvalid), .mp_arready(mp_arready),
        .mp_rid(mp_rid), .mp_rdata(mp_rdata), .mp_rresp(mp_rresp),
        .mp_rlast(mp_rlast), .mp_rvalid(mp_rvalid), .mp_rready(mp_rready),

        .sp_awid(sp_awid[NPS*MAXID-1:0]),
        .sp_awaddr(sp_awaddr[NPS*AW-1:0]),
        .sp_awlen(sp_awlen[NPS*8-1:0]), .sp_awsize(sp_awsize[NPS*3-1:0]),
        .sp_awburst(sp_awburst[NPS*2-1:0]),
        .sp_awvalid(sp_awvalid[NPS-1:0]), .sp_awready(sp_awready[NPS-1:0]),
        .sp_wdata(sp_wdata[NPS*MAXW-1:0]),
        .sp_wstrb(sp_wstrb[NPS*(MAXW/8)-1:0]),
        .sp_wlast(sp_wlast[NPS-1:0]), .sp_wvalid(sp_wvalid[NPS-1:0]),
        .sp_wready(sp_wready[NPS-1:0]),
        .sp_bid(sp_bid[NPS*MAXID-1:0]), .sp_bresp(sp_bresp[NPS*2-1:0]),
        .sp_bvalid(sp_bvalid[NPS-1:0]), .sp_bready(sp_bready[NPS-1:0]),
        .sp_arid(sp_arid[NPS*MAXID-1:0]),
        .sp_araddr(sp_araddr[NPS*AW-1:0]),
        .sp_arlen(sp_arlen[NPS*8-1:0]), .sp_arsize(sp_arsize[NPS*3-1:0]),
        .sp_arburst(sp_arburst[NPS*2-1:0]),
        .sp_arvalid(sp_arvalid[NPS-1:0]), .sp_arready(sp_arready[NPS-1:0]),
        .sp_rid(sp_rid[NPS*MAXID-1:0]), .sp_rdata(sp_rdata[NPS*MAXW-1:0]),
        .sp_rresp(sp_rresp[NPS*2-1:0]), .sp_rlast(sp_rlast[NPS-1:0]),
        .sp_rvalid(sp_rvalid[NPS-1:0]), .sp_rready(sp_rready[NPS-1:0]),

        .lk_req_valid(rq_valid), .lk_req_ready(rq_ready),
        .lk_req_dport(rq_dport), .lk_req_src(rq_src), .lk_req_tag(rq_tag),
        .lk_req_wr(rq_wr), .lk_req_head(rq_head), .lk_req_last(rq_last),
        .lk_req_addr(rq_addr), .lk_req_len(rq_len), .lk_req_size(rq_size),
        .lk_req_data(rq_data), .lk_req_strb(rq_strb),
        .lk_rsp_valid(rs_valid), .lk_rsp_ready(rs_ready), .lk_rsp_dst(rs_dst),
        .lk_rsp_tag(rs_tag), .lk_rsp_wr(rs_wr), .lk_rsp_last(rs_last),
        .lk_rsp_resp(rs_resp), .lk_rsp_data(rs_data),
        .stat_decerr(stat_decerr)
    );

    wire [NK-1:0] lclk  = {clk_s3, clk_s2, clk_s0};
    wire [NK-1:0] lrstn = {aresetn_s3, aresetn_s2, aresetn_s0};

    genvar k;
    generate
    for (k = 0; k < NK; k = k + 1) begin : g_leaf
        // Leaf endpoints land above the root's: link 0 is SLR0 at NPS.
        localparam integer B = NPS * (k + 1);

        wire              fq_valid, fq_ready;
        wire [LNK_RQ-1:0] fq_data;
        wire              fs_valid, fs_ready;
        wire [LNK_RS-1:0] fs_data;

        sb_link #(.W(LNK_RQ), .PIPE(PIPE), .CRED(CRED)) u_lk_req (
            .clk(bus_clk), .rst(bus_rst),
            .i_valid(rq_valid[k]), .i_ready(rq_ready[k]),
            .i_data({rq_src, rq_dport, rq_tag, rq_wr, rq_head, rq_last,
                     rq_addr, rq_len, rq_size, rq_data, rq_strb}),
            .o_valid(fq_valid), .o_ready(fq_ready), .o_data(fq_data));

        wire [SRCW-1:0] lq_src;
        wire [DSTW-1:0] lq_dpt;
        wire [TAGW-1:0] lq_tag;
        wire            lq_wr, lq_head, lq_last;
        wire [AW-1:0]   lq_addr;
        wire [7:0]      lq_len;
        wire [2:0]      lq_size;
        wire [FW-1:0]   lq_data;
        wire [FW/8-1:0] lq_strb;

        assign {lq_src, lq_dpt, lq_tag, lq_wr, lq_head, lq_last, lq_addr,
                lq_len, lq_size, lq_data, lq_strb} = fq_data;

        sb_link #(.W(LNK_RS), .PIPE(PIPE), .CRED(CRED)) u_lk_rsp (
            .clk(bus_clk), .rst(bus_rst),
            .i_valid(fs_valid), .i_ready(fs_ready), .i_data(fs_data),
            .o_valid(rs_valid[k]), .o_ready(rs_ready[k]),
            .o_data({rs_dst[k*SRCW +: SRCW], rs_tag[k*TAGW +: TAGW],
                     rs_wr[k], rs_last[k], rs_resp[k*2 +: 2],
                     rs_data[k*FW +: FW]}));

        wire [SRCW-1:0] ls_dst;
        wire [TAGW-1:0] ls_tag;
        wire            ls_wr, ls_last;
        wire [1:0]      ls_resp;
        wire [FW-1:0]   ls_data;
        assign fs_data = {ls_dst, ls_tag, ls_wr, ls_last, ls_resp, ls_data};

        sb_stn_leaf #(.FW(FW), .AW(AW), .MAXW(MAXW), .MAXID(MAXID), .NS(NPS),
                      .TAGW(TAGW), .DSTW(DSTW), .SRCW(SRCW), .OST(OST),
                      .TIMEOUT(TIMEOUT), .LUT_PER_BRAM(LUT_PER_BRAM),
                      .LOC_W(32'h0000_0001)) u_leaf (
            .bus_clk(bus_clk), .bus_rst(bus_rst),
            .clk_loc(lclk[k]), .aresetn_loc(lrstn[k]),

            .lk_req_valid(fq_valid), .lk_req_ready(fq_ready),
            .lk_req_dport(lq_dpt), .lk_req_src(lq_src), .lk_req_tag(lq_tag),
            .lk_req_wr(lq_wr), .lk_req_head(lq_head), .lk_req_last(lq_last),
            .lk_req_addr(lq_addr), .lk_req_len(lq_len), .lk_req_size(lq_size),
            .lk_req_data(lq_data), .lk_req_strb(lq_strb),
            .lk_rsp_valid(fs_valid), .lk_rsp_ready(fs_ready),
            .lk_rsp_dst(ls_dst), .lk_rsp_tag(ls_tag), .lk_rsp_wr(ls_wr),
            .lk_rsp_last(ls_last), .lk_rsp_resp(ls_resp),
            .lk_rsp_data(ls_data),

            .sp_awid(sp_awid[B*MAXID +: NPS*MAXID]),
            .sp_awaddr(sp_awaddr[B*AW +: NPS*AW]),
            .sp_awlen(sp_awlen[B*8 +: NPS*8]),
            .sp_awsize(sp_awsize[B*3 +: NPS*3]),
            .sp_awburst(sp_awburst[B*2 +: NPS*2]),
            .sp_awvalid(sp_awvalid[B +: NPS]),
            .sp_awready(sp_awready[B +: NPS]),
            .sp_wdata(sp_wdata[B*MAXW +: NPS*MAXW]),
            .sp_wstrb(sp_wstrb[B*(MAXW/8) +: NPS*(MAXW/8)]),
            .sp_wlast(sp_wlast[B +: NPS]),
            .sp_wvalid(sp_wvalid[B +: NPS]),
            .sp_wready(sp_wready[B +: NPS]),
            .sp_bid(sp_bid[B*MAXID +: NPS*MAXID]),
            .sp_bresp(sp_bresp[B*2 +: NPS*2]),
            .sp_bvalid(sp_bvalid[B +: NPS]),
            .sp_bready(sp_bready[B +: NPS]),
            .sp_arid(sp_arid[B*MAXID +: NPS*MAXID]),
            .sp_araddr(sp_araddr[B*AW +: NPS*AW]),
            .sp_arlen(sp_arlen[B*8 +: NPS*8]),
            .sp_arsize(sp_arsize[B*3 +: NPS*3]),
            .sp_arburst(sp_arburst[B*2 +: NPS*2]),
            .sp_arvalid(sp_arvalid[B +: NPS]),
            .sp_arready(sp_arready[B +: NPS]),
            .sp_rid(sp_rid[B*MAXID +: NPS*MAXID]),
            .sp_rdata(sp_rdata[B*MAXW +: NPS*MAXW]),
            .sp_rresp(sp_rresp[B*2 +: NPS*2]),
            .sp_rlast(sp_rlast[B +: NPS]),
            .sp_rvalid(sp_rvalid[B +: NPS]),
            .sp_rready(sp_rready[B +: NPS])
        );
    end
    endgenerate
endmodule

`default_nettype wire

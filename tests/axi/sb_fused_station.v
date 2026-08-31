// One fused station doing M masters x N subordinates: the in-station SASD engine
// (M -> concentrate -> shared core -> 1 flit) into a single-station hub routing to
// N NSU subs. For the general-shape comparison vs vendor M x N interconnect. Single
// station (neighbour links tied off); a 4-station split adds O(1)-LUT backbone links.

`default_nettype none

module sb_fused_station #(
    parameter integer M  = 4,
    parameter integer N  = 4,
    parameter integer DW = 512,
    parameter         MEM = "block",
    parameter integer SFW = 1
)(
    input  wire                clk,
    input  wire                rst,
    input  wire [M*4-1:0]      s_awid,
    input  wire [M*40-1:0]     s_awaddr,
    input  wire [M*8-1:0]      s_awlen,
    input  wire [M*3-1:0]      s_awsize,
    input  wire [M*2-1:0]      s_awburst,
    input  wire [M-1:0]        s_awvalid,
    output wire [M-1:0]        s_awready,
    input  wire [M*DW-1:0]     s_wdata,
    input  wire [M*(DW/8)-1:0] s_wstrb,
    input  wire [M-1:0]        s_wlast,
    input  wire [M-1:0]        s_wvalid,
    output wire [M-1:0]        s_wready,
    output wire [M*4-1:0]      s_bid,
    output wire [M*2-1:0]      s_bresp,
    output wire [M-1:0]        s_bvalid,
    input  wire [M-1:0]        s_bready,
    input  wire [M*4-1:0]      s_arid,
    input  wire [M*40-1:0]     s_araddr,
    input  wire [M*8-1:0]      s_arlen,
    input  wire [M*3-1:0]      s_arsize,
    input  wire [M*2-1:0]      s_arburst,
    input  wire [M-1:0]        s_arvalid,
    output wire [M-1:0]        s_arready,
    output wire [M*4-1:0]      s_rid,
    output wire [M*DW-1:0]     s_rdata,
    output wire [M*2-1:0]      s_rresp,
    output wire [M-1:0]        s_rlast,
    output wire [M-1:0]        s_rvalid,
    input  wire [M-1:0]        s_rready,
    // N external AXI subordinates
    output wire [N*4-1:0]      m_awid,
    output wire [N*40-1:0]     m_awaddr,
    output wire [N*8-1:0]      m_awlen,
    output wire [N*3-1:0]      m_awsize,
    output wire [N*2-1:0]      m_awburst,
    output wire [N-1:0]        m_awvalid,
    input  wire [N-1:0]        m_awready,
    output wire [N*DW-1:0]     m_wdata,
    output wire [N*(DW/8)-1:0] m_wstrb,
    output wire [N-1:0]        m_wlast,
    output wire [N-1:0]        m_wvalid,
    input  wire [N-1:0]        m_wready,
    input  wire [N*4-1:0]      m_bid,
    input  wire [N*2-1:0]      m_bresp,
    input  wire [N-1:0]        m_bvalid,
    output wire [N-1:0]        m_bready,
    output wire [N*4-1:0]      m_arid,
    output wire [N*40-1:0]     m_araddr,
    output wire [N*8-1:0]      m_arlen,
    output wire [N*3-1:0]      m_arsize,
    output wire [N*2-1:0]      m_arburst,
    output wire [N-1:0]        m_arvalid,
    input  wire [N-1:0]        m_arready,
    input  wire [N*4-1:0]      m_rid,
    input  wire [N*DW-1:0]     m_rdata,
    input  wire [N*2-1:0]      m_rresp,
    input  wire [N-1:0]        m_rlast,
    input  wire [N-1:0]        m_rvalid,
    output wire [N-1:0]        m_rready
);
    localparam integer AW=40, FW=DW, TAGW=4, DSTW=4, IDW=4;
    localparam integer STNW=1, PORTW=($clog2(N)<1)?1:$clog2(N), SRCW=1;
    localparam integer NSUS=STNW+SRCW;
    localparam integer NSEG=N;
    localparam [AW-1:0] MSK = {8'hFF, 32'd0};
    function [NSEG*AW-1:0] mkbase; input integer u; integer k; begin
        mkbase={NSEG*AW{1'b0}};
        for (k=0;k<NSEG;k=k+1) begin
            mkbase[k*AW +: AW] = k << 32;
        end
    end endfunction
    function [NSEG*DSTW-1:0] mkprt; input integer u; integer k; begin
        mkprt={NSEG*DSTW{1'b0}};
        for (k=0;k<NSEG;k=k+1) begin
            mkprt[k*DSTW +: DSTW] = k[DSTW-1:0];
        end
    end endfunction

    // fused input engine: M masters -> 1 flit (dst=station 0, dport=sub)
    wire            q_valid, q_ready, q_wr, q_head, q_last;
    wire [DSTW-1:0] q_dst, q_dport;
    wire [TAGW-1:0] q_tag;
    wire [AW-1:0]   q_addr;
    wire [7:0]      q_len;
    wire [2:0]      q_size;
    wire [FW-1:0]   q_data;
    wire [FW/8-1:0] q_strb;
    wire            d_valid, d_ready, d_wr, d_last;
    wire [TAGW-1:0] d_tag;
    wire [1:0]      d_resp;
    wire [FW-1:0]   d_data;

    sb_station_in #(.M(M), .DW(DW), .AW(AW), .IDW(IDW), .FW(FW), .TAGW(TAGW),
        .DSTW(DSTW), .NSEG(NSEG), .OUTST(4), .MEM(MEM), .STORE_FWD(SFW),
        .SEG_BASE(mkbase(0)), .SEG_MASK({NSEG{MSK}}), .SEG_XLT(mkbase(0)),
        .SEG_DST({NSEG*DSTW{1'b0}}), .SEG_DPORT(mkprt(0)), .SEG_VLD({NSEG{1'b1}})
    ) u_in (
        .clk(clk), .rst(rst),
        .s_awid(s_awid), .s_awaddr(s_awaddr), .s_awlen(s_awlen), .s_awsize(s_awsize),
        .s_awburst(s_awburst), .s_awvalid(s_awvalid), .s_awready(s_awready),
        .s_wdata(s_wdata), .s_wstrb(s_wstrb), .s_wlast(s_wlast),
        .s_wvalid(s_wvalid), .s_wready(s_wready),
        .s_bid(s_bid), .s_bresp(s_bresp), .s_bvalid(s_bvalid), .s_bready(s_bready),
        .s_arid(s_arid), .s_araddr(s_araddr), .s_arlen(s_arlen), .s_arsize(s_arsize),
        .s_arburst(s_arburst), .s_arvalid(s_arvalid), .s_arready(s_arready),
        .s_rid(s_rid), .s_rdata(s_rdata), .s_rresp(s_rresp), .s_rlast(s_rlast),
        .s_rvalid(s_rvalid), .s_rready(s_rready),
        .req_valid(q_valid), .req_ready(q_ready), .req_dst(q_dst), .req_dport(q_dport),
        .req_tag(q_tag), .req_wr(q_wr), .req_head(q_head), .req_last(q_last),
        .req_addr(q_addr), .req_len(q_len), .req_size(q_size), .req_data(q_data),
        .req_strb(q_strb),
        .rsp_valid(d_valid), .rsp_ready(d_ready), .rsp_tag(d_tag), .rsp_wr(d_wr),
        .rsp_last(d_last), .rsp_resp(d_resp), .rsp_data(d_data), .stat_decerr()
    );

    // single-station hub: 1 injection -> N subs
    wire [N-1:0]        e_valid, e_ready;
    wire [NSUS-1:0]     e_src;
    wire [TAGW-1:0]     e_tag;
    wire                e_wr, e_head, e_last;
    wire [AW-1:0]       e_addr;
    wire [7:0]          e_len;
    wire [2:0]          e_size;
    wire [FW-1:0]       e_data;
    wire [FW/8-1:0]     e_strb;
    wire [N-1:0]        p_valid, p_ready, p_wr, p_last;
    wire [N*NSUS-1:0]   p_dst;
    wire [N*TAGW-1:0]   p_tag;
    wire [N*2-1:0]      p_resp;
    wire [N*FW-1:0]     p_data;

    localparam integer RQW = STNW + PORTW + STNW + SRCW + TAGW + 3 + AW + 8 + 3 + FW + FW/8;
    localparam integer RSW = STNW + SRCW + TAGW + 2 + 2 + FW;

    sb_stn_line #(.FW(FW), .AW(AW), .TAGW(TAGW), .NSTN(1), .STN(0), .NM(1), .NQ(N),
                  .STNW(STNW), .PORTW(PORTW), .SRCW(SRCW), .RQW(RQW), .RSW(RSW)) u_stn (
        .clk(clk), .rst(rst),
        .nm_req_valid(q_valid), .nm_req_ready(q_ready),
        .nm_req_dstn(1'b0), .nm_req_dport(q_dport[PORTW-1:0]),
        .nm_req_tag(q_tag), .nm_req_wr(q_wr), .nm_req_head(q_head),
        .nm_req_last(q_last), .nm_req_addr(q_addr), .nm_req_len(q_len),
        .nm_req_size(q_size), .nm_req_data(q_data), .nm_req_strb(q_strb),
        .ns_req_valid(e_valid), .ns_req_ready(e_ready), .ns_req_src(e_src),
        .ns_req_tag(e_tag), .ns_req_wr(e_wr), .ns_req_head(e_head),
        .ns_req_last(e_last), .ns_req_addr(e_addr), .ns_req_len(e_len),
        .ns_req_size(e_size), .ns_req_data(e_data), .ns_req_strb(e_strb),
        .ns_rsp_valid(p_valid), .ns_rsp_ready(p_ready), .ns_rsp_dst(p_dst),
        .ns_rsp_tag(p_tag), .ns_rsp_wr(p_wr), .ns_rsp_last(p_last),
        .ns_rsp_resp(p_resp), .ns_rsp_data(p_data),
        .nm_rsp_valid(d_valid), .nm_rsp_ready(d_ready), .nm_rsp_tag(d_tag),
        .nm_rsp_wr(d_wr), .nm_rsp_last(d_last), .nm_rsp_resp(d_resp),
        .nm_rsp_data(d_data),
        .lf_req_valid(1'b0), .lf_req_ready(), .lf_req_pay({RQW{1'b0}}),
        .lt_req_valid(), .lt_req_ready(1'b1), .lt_req_pay(),
        .lf_rsp_valid(1'b0), .lf_rsp_ready(), .lf_rsp_pay({RSW{1'b0}}),
        .lt_rsp_valid(), .lt_rsp_ready(1'b1), .lt_rsp_pay(),
        .rf_req_valid(1'b0), .rf_req_ready(), .rf_req_pay({RQW{1'b0}}),
        .rt_req_valid(), .rt_req_ready(1'b1), .rt_req_pay(),
        .rf_rsp_valid(1'b0), .rf_rsp_ready(), .rf_rsp_pay({RSW{1'b0}}),
        .rt_rsp_valid(), .rt_rsp_ready(1'b1), .rt_rsp_pay(),
        .stat_inj_flits(), .stat_inj_wait()
    );

    // N subs
    genvar j;
    generate
    for (j = 0; j < N; j = j + 1) begin : g_nsu
        sb_nsu #(.SDW(DW), .SIDW(4), .AW(AW), .FW(FW), .TAGW(TAGW), .SRCW(NSUS),
                 .WOST(4), .ROST(4), .REQ_DEPTH(16), .RSP_DEPTH(16),
                 .REQ_MEM(MEM), .RSP_MEM(MEM), .CHAN_MEM(MEM)) u_nsu (
            .bus_clk(clk), .bus_rst(rst),
            .req_valid(e_valid[j]), .req_ready(e_ready[j]), .req_src(e_src),
            .req_tag(e_tag), .req_wr(e_wr), .req_head(e_head), .req_last(e_last),
            .req_addr(e_addr), .req_len(e_len), .req_size(e_size), .req_data(e_data),
            .req_strb(e_strb),
            .rsp_valid(p_valid[j]), .rsp_ready(p_ready[j]),
            .rsp_dst(p_dst[j*NSUS +: NSUS]), .rsp_tag(p_tag[j*TAGW +: TAGW]),
            .rsp_wr(p_wr[j]), .rsp_last(p_last[j]), .rsp_resp(p_resp[j*2 +: 2]),
            .rsp_data(p_data[j*FW +: FW]),
            .m_aclk(clk), .m_aresetn(~rst),
            .m_awid(m_awid[j*4 +: 4]), .m_awaddr(m_awaddr[j*40 +: 40]),
            .m_awlen(m_awlen[j*8 +: 8]), .m_awsize(m_awsize[j*3 +: 3]),
            .m_awburst(m_awburst[j*2 +: 2]), .m_awvalid(m_awvalid[j]),
            .m_awready(m_awready[j]),
            .m_wdata(m_wdata[j*DW +: DW]), .m_wstrb(m_wstrb[j*(DW/8) +: DW/8]),
            .m_wlast(m_wlast[j]), .m_wvalid(m_wvalid[j]), .m_wready(m_wready[j]),
            .m_bid(m_bid[j*4 +: 4]), .m_bresp(m_bresp[j*2 +: 2]),
            .m_bvalid(m_bvalid[j]), .m_bready(m_bready[j]),
            .m_arid(m_arid[j*4 +: 4]), .m_araddr(m_araddr[j*40 +: 40]),
            .m_arlen(m_arlen[j*8 +: 8]), .m_arsize(m_arsize[j*3 +: 3]),
            .m_arburst(m_arburst[j*2 +: 2]), .m_arvalid(m_arvalid[j]),
            .m_arready(m_arready[j]),
            .m_rid(m_rid[j*4 +: 4]), .m_rdata(m_rdata[j*DW +: DW]),
            .m_rresp(m_rresp[j*2 +: 2]), .m_rlast(m_rlast[j]),
            .m_rvalid(m_rvalid[j]), .m_rready(m_rready[j])
        );
    end
    endgenerate
endmodule

`default_nettype wire

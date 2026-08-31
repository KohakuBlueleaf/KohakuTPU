// Fused vs independent input-engine area probes, one station, single clock.
//   station_in_probe : M masters -> sb_station_in (concentrate + 1 shared core)
//   indep_probe      : M masters -> M independent sb_nmu (the current design)
// Same generated N-segment decode map on both, so the delta is the fusion win.

`default_nettype none

// -------- generated decode: slave k at k<<32, top-8-bit compare, dst=k ---------
module station_in_probe #(
    parameter integer M  = 4,
    parameter integer DW = 512,
    parameter integer N  = 8
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
    output wire                req_valid,
    input  wire                req_ready,
    output wire [3:0]          req_dst,
    output wire [3:0]          req_dport,
    output wire [3:0]          req_tag,
    output wire                req_wr,
    output wire                req_head,
    output wire                req_last,
    output wire [39:0]         req_addr,
    output wire [7:0]          req_len,
    output wire [2:0]          req_size,
    output wire [255:0]        req_data,
    output wire [31:0]         req_strb,
    input  wire                rsp_valid,
    output wire                rsp_ready,
    input  wire [3:0]          rsp_tag,
    input  wire                rsp_wr,
    input  wire                rsp_last,
    input  wire [1:0]          rsp_resp,
    input  wire [255:0]        rsp_data,
    output wire [31:0]         stat_decerr
);
    localparam integer AW = 40, DSTW = 4, NSEG = N;
    localparam [AW-1:0] MSK = {8'hFF, 32'd0};
    function [NSEG*AW-1:0] mkbase; input integer u; integer k; begin
        mkbase = {NSEG*AW{1'b0}};
        for (k = 0; k < NSEG; k = k + 1) begin
            mkbase[k*AW +: AW] = k << 32;
        end
    end endfunction
    function [NSEG*DSTW-1:0] mkdst; input integer u; integer k; begin
        mkdst = {NSEG*DSTW{1'b0}};
        for (k = 0; k < NSEG; k = k + 1) begin
            mkdst[k*DSTW +: DSTW] = k[DSTW-1:0];
        end
    end endfunction

    sb_station_in #(.M(M), .DW(DW), .AW(AW), .IDW(4), .FW(256), .TAGW(4),
        .DSTW(DSTW), .NSEG(NSEG), .OUTST(4),
        .SEG_BASE(mkbase(0)), .SEG_MASK({NSEG{MSK}}), .SEG_XLT(mkbase(0)),
        .SEG_DST(mkdst(0)), .SEG_DPORT({NSEG*DSTW{1'b0}}), .SEG_VLD({NSEG{1'b1}})
    ) u (
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
        .req_valid(req_valid), .req_ready(req_ready), .req_dst(req_dst),
        .req_dport(req_dport), .req_tag(req_tag), .req_wr(req_wr), .req_head(req_head),
        .req_last(req_last), .req_addr(req_addr), .req_len(req_len), .req_size(req_size),
        .req_data(req_data), .req_strb(req_strb),
        .rsp_valid(rsp_valid), .rsp_ready(rsp_ready), .rsp_tag(rsp_tag),
        .rsp_wr(rsp_wr), .rsp_last(rsp_last), .rsp_resp(rsp_resp), .rsp_data(rsp_data),
        .stat_decerr(stat_decerr)
    );
endmodule


module indep_probe #(
    parameter integer M  = 4,
    parameter integer DW = 512,
    parameter integer N  = 8
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
    output wire [M-1:0]        req_valid,
    input  wire [M-1:0]        req_ready,
    output wire [M*4-1:0]      req_dst,
    output wire [M*4-1:0]      req_dport,
    output wire [M*4-1:0]      req_tag,
    output wire [M-1:0]        req_wr,
    output wire [M-1:0]        req_head,
    output wire [M-1:0]        req_last,
    output wire [M*40-1:0]     req_addr,
    output wire [M*8-1:0]      req_len,
    output wire [M*3-1:0]      req_size,
    output wire [M*256-1:0]    req_data,
    output wire [M*32-1:0]     req_strb,
    input  wire [M-1:0]        rsp_valid,
    output wire [M-1:0]        rsp_ready,
    input  wire [M*4-1:0]      rsp_tag,
    input  wire [M-1:0]        rsp_wr,
    input  wire [M-1:0]        rsp_last,
    input  wire [M*2-1:0]      rsp_resp,
    input  wire [M*256-1:0]    rsp_data,
    output wire [M*32-1:0]     stat_decerr
);
    localparam integer AW = 40, DSTW = 4, NSEG = N;
    localparam [AW-1:0] MSK = {8'hFF, 32'd0};
    function [NSEG*AW-1:0] mkbase; input integer u; integer k; begin
        mkbase = {NSEG*AW{1'b0}};
        for (k = 0; k < NSEG; k = k + 1) begin
            mkbase[k*AW +: AW] = k << 32;
        end
    end endfunction
    function [NSEG*DSTW-1:0] mkdst; input integer u; integer k; begin
        mkdst = {NSEG*DSTW{1'b0}};
        for (k = 0; k < NSEG; k = k + 1) begin
            mkdst[k*DSTW +: DSTW] = k[DSTW-1:0];
        end
    end endfunction

    genvar g;
    generate
    for (g = 0; g < M; g = g + 1) begin : gm
        sb_nmu #(.MW(DW), .MIDW(4), .AW(AW), .FW(256), .TAGW(4), .DSTW(DSTW),
            .NSEG(NSEG), .OUTST(4),
            .SEG_BASE(mkbase(0)), .SEG_MASK({NSEG{MSK}}), .SEG_XLT(mkbase(0)),
            .SEG_DST(mkdst(0)), .SEG_DPORT({NSEG*DSTW{1'b0}}), .SEG_VLD({NSEG{1'b1}})
        ) u (
            .s_aclk(clk), .s_aresetn(~rst),
            .s_awid(s_awid[g*4 +: 4]), .s_awaddr(s_awaddr[g*40 +: 40]),
            .s_awlen(s_awlen[g*8 +: 8]), .s_awsize(s_awsize[g*3 +: 3]),
            .s_awburst(s_awburst[g*2 +: 2]), .s_awvalid(s_awvalid[g]),
            .s_awready(s_awready[g]),
            .s_wdata(s_wdata[g*DW +: DW]), .s_wstrb(s_wstrb[g*(DW/8) +: DW/8]),
            .s_wlast(s_wlast[g]), .s_wvalid(s_wvalid[g]), .s_wready(s_wready[g]),
            .s_bid(s_bid[g*4 +: 4]), .s_bresp(s_bresp[g*2 +: 2]),
            .s_bvalid(s_bvalid[g]), .s_bready(s_bready[g]),
            .s_arid(s_arid[g*4 +: 4]), .s_araddr(s_araddr[g*40 +: 40]),
            .s_arlen(s_arlen[g*8 +: 8]), .s_arsize(s_arsize[g*3 +: 3]),
            .s_arburst(s_arburst[g*2 +: 2]), .s_arvalid(s_arvalid[g]),
            .s_arready(s_arready[g]),
            .s_rid(s_rid[g*4 +: 4]), .s_rdata(s_rdata[g*DW +: DW]),
            .s_rresp(s_rresp[g*2 +: 2]), .s_rlast(s_rlast[g]),
            .s_rvalid(s_rvalid[g]), .s_rready(s_rready[g]),
            .bus_clk(clk), .bus_rst(rst),
            .req_valid(req_valid[g]), .req_ready(req_ready[g]),
            .req_dst(req_dst[g*4 +: 4]), .req_dport(req_dport[g*4 +: 4]),
            .req_tag(req_tag[g*4 +: 4]), .req_wr(req_wr[g]), .req_head(req_head[g]),
            .req_last(req_last[g]), .req_addr(req_addr[g*40 +: 40]),
            .req_len(req_len[g*8 +: 8]), .req_size(req_size[g*3 +: 3]),
            .req_data(req_data[g*256 +: 256]), .req_strb(req_strb[g*32 +: 32]),
            .rsp_valid(rsp_valid[g]), .rsp_ready(rsp_ready[g]),
            .rsp_tag(rsp_tag[g*4 +: 4]), .rsp_wr(rsp_wr[g]), .rsp_last(rsp_last[g]),
            .rsp_resp(rsp_resp[g*2 +: 2]), .rsp_data(rsp_data[g*256 +: 256]),
            .stat_decerr(stat_decerr[g*32 +: 32])
        );
    end
    endgenerate
endmodule

`default_nettype wire

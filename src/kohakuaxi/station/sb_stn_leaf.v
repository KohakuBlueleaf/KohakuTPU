// A leaf station: no managers, NS local subordinates on ONE clock, one link.
// sb_leaf is the v5-shaped measurement artifact; this is the configurable one.

// With one REQ source the injection mux disappears entirely, which is why a
// leaf costs far less than a SmartConnect covering the same endpoints.

`default_nettype none

module sb_stn_leaf #(
    parameter integer FW    = 512,
    parameter integer AW    = 40,
    parameter integer MAXW  = 512,
    parameter integer MAXID = 4,
    parameter integer NS    = 2,
    parameter integer TAGW  = 4,
    parameter integer DSTW  = 3,
    parameter integer SRCW  = 2,
    parameter integer OST   = 4,
    parameter integer TIMEOUT      = 0,
    parameter integer LUT_PER_BRAM = 0,
    // Bit i set makes subordinate i FW wide, clear makes it 32.
    parameter [31:0]  LOC_W = 32'h0000_0001
)(
    input  wire                 bus_clk,
    input  wire                 bus_rst,
    input  wire                 clk_loc,    input wire aresetn_loc,

    input  wire                 lk_req_valid,
    output wire                 lk_req_ready,
    input  wire [DSTW-1:0]      lk_req_dport,
    input  wire [SRCW-1:0]      lk_req_src,
    input  wire [TAGW-1:0]      lk_req_tag,
    input  wire                 lk_req_wr,
    input  wire                 lk_req_head,
    input  wire                 lk_req_last,
    input  wire [AW-1:0]        lk_req_addr,
    input  wire [7:0]           lk_req_len,
    input  wire [2:0]           lk_req_size,
    input  wire [FW-1:0]        lk_req_data,
    input  wire [FW/8-1:0]      lk_req_strb,

    output wire                 lk_rsp_valid,
    input  wire                 lk_rsp_ready,
    output wire [SRCW-1:0]      lk_rsp_dst,
    output wire [TAGW-1:0]      lk_rsp_tag,
    output wire                 lk_rsp_wr,
    output wire                 lk_rsp_last,
    output wire [1:0]           lk_rsp_resp,
    output wire [FW-1:0]        lk_rsp_data,

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
    output wire [NS-1:0]          sp_rready
);
    localparam integer REQ_PW = SRCW + TAGW + 3 + AW + 8 + 3 + FW + FW/8;
    // SRCW is in the payload: the hub only carries `dst`, so a return route
    // left outside it does not survive arbitration.
    localparam integer RSP_PW = SRCW + TAGW + 2 + 2 + FW;

    wire [NS-1:0]   e_valid, e_ready;
    wire [SRCW-1:0] e_src;
    wire [TAGW-1:0] e_tag;
    wire            e_wr, e_head, e_last;
    wire [AW-1:0]   e_addr;
    wire [7:0]      e_len;
    wire [2:0]      e_size;
    wire [FW-1:0]   e_data;
    wire [FW/8-1:0] e_strb;

    localparam integer HDW = (NS <= 1) ? 1 : $clog2(NS);

    sb_hub #(.NSRC(1), .NDST(NS), .PW(REQ_PW)) u_req (
        .clk(bus_clk), .rst(bus_rst),
        .i_valid(lk_req_valid), .i_ready(lk_req_ready), .i_last(lk_req_last),
        .i_dst(lk_req_dport[HDW-1:0]),
        .i_pay({lk_req_src, lk_req_tag, lk_req_wr, lk_req_head, lk_req_last,
                lk_req_addr, lk_req_len, lk_req_size, lk_req_data,
                lk_req_strb}),
        .o_valid(e_valid), .o_ready(e_ready),
        .o_pay({e_src, e_tag, e_wr, e_head, e_last, e_addr, e_len, e_size,
                e_data, e_strb})
    );

    wire [NS-1:0]      p_valid, p_ready, p_wr, p_last;
    wire [NS*SRCW-1:0] p_dst;
    wire [NS*TAGW-1:0] p_tag;
    wire [NS*2-1:0]    p_resp;
    wire [NS*FW-1:0]   p_data;

    wire [NS*RSP_PW-1:0] rsp_pay;
    genvar g;
    generate
    for (g = 0; g < NS; g = g + 1) begin : g_pay
        assign rsp_pay[g*RSP_PW +: RSP_PW] = {
            p_dst[g*SRCW +: SRCW], p_tag[g*TAGW +: TAGW], p_wr[g], p_last[g],
            p_resp[g*2 +: 2], p_data[g*FW +: FW] };
    end
    endgenerate

    sb_hub #(.NSRC(NS), .NDST(1), .PW(RSP_PW)) u_rsp (
        .clk(bus_clk), .rst(bus_rst),
        .i_valid(p_valid), .i_ready(p_ready), .i_last(p_last),
        .i_dst({NS{1'b0}}), .i_pay(rsp_pay),
        .o_valid(lk_rsp_valid), .o_ready(lk_rsp_ready),
        .o_pay({lk_rsp_dst, lk_rsp_tag, lk_rsp_wr, lk_rsp_last, lk_rsp_resp,
                lk_rsp_data})
    );

    genvar i;
    generate
    for (i = 0; i < NS; i = i + 1) begin : g_nsu
        localparam integer DW = LOC_W[i] ? FW : 32;
        if (DW < MAXW) begin : g_pad
            assign sp_wdata[i*MAXW + DW +: MAXW-DW] = {(MAXW-DW){1'b0}};
            assign sp_wstrb[i*(MAXW/8) + DW/8 +: (MAXW-DW)/8] =
                   {((MAXW-DW)/8){1'b0}};
        end
        sb_nsu #(.SDW(DW), .SIDW(MAXID), .AW(AW), .FW(FW), .TAGW(TAGW),
                 .SRCW(SRCW), .WOST(OST), .ROST(OST), .TIMEOUT(TIMEOUT),
                 .LUT_PER_BRAM(LUT_PER_BRAM),
                 .REQ_DEPTH(16), .RSP_DEPTH(16)) u_nsu (
            .bus_clk(bus_clk), .bus_rst(bus_rst),
            .req_valid(e_valid[i]), .req_ready(e_ready[i]), .req_src(e_src),
            .req_tag(e_tag), .req_wr(e_wr), .req_head(e_head),
            .req_last(e_last), .req_addr(e_addr), .req_len(e_len),
            .req_size(e_size), .req_data(e_data), .req_strb(e_strb),
            .rsp_valid(p_valid[i]), .rsp_ready(p_ready[i]),
            .rsp_dst(p_dst[i*SRCW +: SRCW]), .rsp_tag(p_tag[i*TAGW +: TAGW]),
            .rsp_wr(p_wr[i]), .rsp_last(p_last[i]),
            .rsp_resp(p_resp[i*2 +: 2]), .rsp_data(p_data[i*FW +: FW]),
            .m_aclk(clk_loc), .m_aresetn(aresetn_loc),
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

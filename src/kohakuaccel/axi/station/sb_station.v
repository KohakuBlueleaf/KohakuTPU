// One station: NM manager shims and NS subordinate shims on two shared paths.

// REQ and RSP are SEPARATE physical paths, sharing no buffer, so RSP can never
// block behind REQ. Sharing wires between VCs is a link-level optimisation.

`default_nettype none

module sb_station #(
    parameter integer NM   = 3,
    parameter integer NS   = 9,
    parameter integer FW   = 512,                     // flit payload width
    parameter integer AW   = 40,
    parameter integer TAGW = 8,
    parameter integer DSTW = (NS <= 1) ? 1 : $clog2(NS),
    parameter integer SRCW = (NM <= 1) ? 1 : $clog2(NM),
    // A station whose source is a LINK must forward the originating manager
    // index, not stamp its own, or every response returns to manager 0.
    parameter integer SRC_PASS = 0,
    parameter integer STATS = 0,                 // 0 costs nothing
    parameter integer ISKID = 0                  // register hub inputs (Fmax)
)(
    input  wire                  clk,
    input  wire                  rst,

    // ---- REQ in, from the NMUs -------------------------------------------
    input  wire [NM-1:0]         nm_req_valid,
    output wire [NM-1:0]         nm_req_ready,
    input  wire [NM*DSTW-1:0]    nm_req_dst,
    input  wire [NM*DSTW-1:0]    nm_req_dport,
    input  wire [NM*SRCW-1:0]    nm_req_src,
    input  wire [NM*TAGW-1:0]    nm_req_tag,
    input  wire [NM-1:0]         nm_req_wr,
    input  wire [NM-1:0]         nm_req_head,
    input  wire [NM-1:0]         nm_req_last,
    input  wire [NM*AW-1:0]      nm_req_addr,
    input  wire [NM*8-1:0]       nm_req_len,
    input  wire [NM*3-1:0]       nm_req_size,
    input  wire [NM*FW-1:0]      nm_req_data,
    input  wire [NM*(FW/8)-1:0]  nm_req_strb,

    // ---- REQ out, broadcast to the NSUs ----------------------------------
    output wire [NS-1:0]         ns_req_valid,
    input  wire [NS-1:0]         ns_req_ready,
    output wire [SRCW-1:0]       ns_req_src,
    output wire [DSTW-1:0]       ns_req_dport,
    output wire [TAGW-1:0]       ns_req_tag,
    output wire                  ns_req_wr,
    output wire                  ns_req_head,
    output wire                  ns_req_last,
    output wire [AW-1:0]         ns_req_addr,
    output wire [7:0]            ns_req_len,
    output wire [2:0]            ns_req_size,
    output wire [FW-1:0]         ns_req_data,
    output wire [FW/8-1:0]       ns_req_strb,

    // ---- RSP in, from the NSUs -------------------------------------------
    input  wire [NS-1:0]         ns_rsp_valid,
    output wire [NS-1:0]         ns_rsp_ready,
    input  wire [NS*SRCW-1:0]    ns_rsp_dst,
    input  wire [NS*TAGW-1:0]    ns_rsp_tag,
    input  wire [NS-1:0]         ns_rsp_wr,
    input  wire [NS-1:0]         ns_rsp_last,
    input  wire [NS*2-1:0]       ns_rsp_resp,
    input  wire [NS*FW-1:0]      ns_rsp_data,

    // ---- RSP out, broadcast to the NMUs ----------------------------------
    output wire [NM-1:0]         nm_rsp_valid,
    input  wire [NM-1:0]         nm_rsp_ready,
    output wire [SRCW-1:0]       nm_rsp_dst,
    output wire [TAGW-1:0]       nm_rsp_tag,
    output wire                  nm_rsp_wr,
    output wire                  nm_rsp_last,
    output wire [1:0]            nm_rsp_resp,
    output wire [FW-1:0]         nm_rsp_data,

    output wire [31:0]           stat_req_flits,
    output wire [31:0]           stat_req_wait,
    output wire [31:0]           stat_rsp_flits,
    output wire [31:0]           stat_rsp_wait
);
    // dport rides the payload: `dst` is consumed by this hop's hub, so a flit
    // heading out over a link would lose where it is going.
    // The hub indexes its own ports; DSTW/SRCW may be WIDER so a flit can carry
    // a global {station, port} across links. Slice, do not assume they match.
    localparam integer HDW = (NS <= 1) ? 1 : $clog2(NS);
    localparam integer HSW = (NM <= 1) ? 1 : $clog2(NM);

    wire [NM*HDW-1:0] hub_rq_dst;
    wire [NS*HSW-1:0] hub_rs_dst;

    localparam integer REQ_PW = SRCW + DSTW + TAGW + 3 + AW + 8 + 3 + FW + FW/8;
    // The return route rides the RSP payload too: a forwarding station routes
    // to one link, so a dst consumed by the hub would not reach the root.
    localparam integer RSP_PW = SRCW + TAGW + 2 + 2 + FW;

    // The src field is the NMU's own index, so the station stamps it rather
    // than trusting the shim -- a forged return route is unroutable garbage.
    wire [NM*REQ_PW-1:0] req_pay;
    genvar g;
    generate
    for (g = 0; g < NM; g = g + 1) begin : g_reqpay
        localparam [SRCW-1:0] MY_SRC = g;
        assign req_pay[g*REQ_PW +: REQ_PW] = {
            SRC_PASS ? nm_req_src[g*SRCW +: SRCW] : MY_SRC,
            nm_req_dport[g*DSTW  +: DSTW],
            nm_req_tag [g*TAGW     +: TAGW],
            nm_req_wr  [g],
            nm_req_head[g],
            nm_req_last[g],
            nm_req_addr[g*AW       +: AW],
            nm_req_len [g*8        +: 8],
            nm_req_size[g*3        +: 3],
            nm_req_data[g*FW       +: FW],
            nm_req_strb[g*(FW/8)   +: FW/8] };
    end
    endgenerate

    sb_hub #(.NSRC(NM), .NDST(NS), .PW(REQ_PW), .STATS(STATS), .ISKID(ISKID)) u_req (
        .clk(clk), .rst(rst),
        .i_valid(nm_req_valid), .i_ready(nm_req_ready), .i_last(nm_req_last),
        .i_dst(hub_rq_dst), .i_pay(req_pay),
        .o_valid(ns_req_valid), .o_ready(ns_req_ready),
        .o_pay({ns_req_src, ns_req_dport, ns_req_tag, ns_req_wr, ns_req_head,
                ns_req_last, ns_req_addr, ns_req_len, ns_req_size,
                ns_req_data, ns_req_strb}),
        .stat_flits(stat_req_flits), .stat_wait(stat_req_wait)
    );

    wire [NS*RSP_PW-1:0] rsp_pay;
    generate
    for (g = 0; g < NM; g = g + 1) begin : g_rqdst
        assign hub_rq_dst[g*HDW +: HDW] = nm_req_dst[g*DSTW +: HDW];
    end
    for (g = 0; g < NS; g = g + 1) begin : g_rsdst
        // A forwarding station has ONE response destination and carries a
        // global src that is not a hub index; slicing it drops the flit.
        assign hub_rs_dst[g*HSW +: HSW] =
            (NM <= 1) ? {HSW{1'b0}} : ns_rsp_dst[g*SRCW +: HSW];
    end
    for (g = 0; g < NS; g = g + 1) begin : g_rsppay
        assign rsp_pay[g*RSP_PW +: RSP_PW] = {
            ns_rsp_dst [g*SRCW +: SRCW],
            ns_rsp_tag [g*TAGW +: TAGW],
            ns_rsp_wr  [g],
            ns_rsp_last[g],
            ns_rsp_resp[g*2    +: 2],
            ns_rsp_data[g*FW   +: FW] };
    end
    endgenerate

    sb_hub #(.NSRC(NS), .NDST(NM), .PW(RSP_PW), .STATS(STATS), .ISKID(ISKID)) u_rsp (
        .clk(clk), .rst(rst),
        .i_valid(ns_rsp_valid), .i_ready(ns_rsp_ready), .i_last(ns_rsp_last),
        .i_dst(hub_rs_dst), .i_pay(rsp_pay),
        .o_valid(nm_rsp_valid), .o_ready(nm_rsp_ready),
        .o_pay({nm_rsp_dst, nm_rsp_tag, nm_rsp_wr, nm_rsp_last, nm_rsp_resp,
                nm_rsp_data}),
        .stat_flits(stat_rsp_flits), .stat_wait(stat_rsp_wait)
    );
endmodule

`default_nettype wire

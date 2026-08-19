// One station at multimesh v5 root_smc's shape: 3 managers, 9 subordinates,
// 4 clock domains, widths 32/512. The configuration a cost comparison uses.

// Ports are flattened to MAXW/MAXID; a port narrower than MAXW uses the low
// bits. This is the shape a generator emits, not something hand-maintained.

// v1 carries mesh memory at 512, not v5's 256: the NSU has no downsizer yet.
// Configure the SmartConnect baseline the same way or the comparison lies.

`default_nettype none

module sb_root9 #(
    parameter integer FW    = 512,
    parameter integer AW    = 40,
    parameter integer MAXW  = 512,
    parameter integer MAXID = 4,
    parameter integer NM    = 3,
    parameter integer NS    = 9,
    parameter integer TAGW  = 4,
    parameter integer DSTW  = 4,
    parameter integer SRCW  = 2,
    // 0 BALANCED, 1 PERF, 2 MIN_LUT, 3 MIN_FF, 4 NO_BRAM
    parameter integer PRESET = 0,
    // Non-zero ignores PRESET's storage choice and costs each FIFO out.
    parameter integer LUT_PER_BRAM = 0,
    // The speed-for-size axis: 1 outstanding and no store-and-forward is what
    // axi_interconnect's minimum-area mode does.
    parameter integer OST       = 4,
    parameter integer STORE_FWD = 1,
    parameter integer TIMEOUT   = 0
)(
    input  wire                 bus_clk,
    input  wire                 bus_rst,

    input  wire                 clk_ctrl,   input wire aresetn_ctrl,
    input  wire                 clk_xdma,   input wire aresetn_xdma,
    input  wire                 clk_mesh,   input wire aresetn_mesh,
    input  wire                 clk_ddr,    input wire aresetn_ddr,

    // ---- manager-facing (AXI slave) ports, flattened ----------------------
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

    // ---- subordinate-facing (AXI master) ports, flattened -----------------
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
    localparam NOBR      = (PRESET == 3) || (PRESET == 4);
    // 64, not 512: AXI4's 4 KB rule caps a 512-bit port at 64 beats, so a
    // deeper queue is sized for a burst that cannot legally arrive.
    localparam integer P_BULK_REQ = 64;
    // In FLITS: a 512-bit manager on a 256-bit fabric returns two per beat.
    localparam integer P_BULK_RSP = (FW < 512) ? 128 : 64;
    // DEPTH picks the primitive: a 16-deep FIFO in BRAM burns 9 RAMB36 on
    // width at 3% depth use. Only 512/256-deep queues are worth a block.
    localparam MINL   = (PRESET == 1) || (PRESET == 2);
    localparam P_SHAL = (NOBR || !MINL) ? "distributed" : "block";
    localparam P_DEEP = NOBR ? "distributed" : "block";

    // ---------------------------------------------------------- address map
    // Routes on the aperture bit and addr[37:36], so v5's 1 TiB window prefix
    // is not carried at all.
    localparam [AW-1:0] MSK_MESH = 40'hB0_0000_0000;
    localparam [AW-1:0] MSK_1M   = 40'hFF_FFF0_0000;
    localparam [AW-1:0] MSK_64K  = 40'hFF_FFFF_0000;

    localparam [AW-1:0] B_M0 = 40'h80_0000_0000, B_M1 = 40'h90_0000_0000;
    localparam [AW-1:0] B_M2 = 40'hA0_0000_0000, B_M3 = 40'hB0_0000_0000;
    localparam [AW-1:0] B_DDR = 40'h00_0030_0000, B_MC = 40'h00_0081_0000;
    localparam [AW-1:0] B_W0 = 40'h00_0090_0000, B_W1 = 40'h00_0091_0000;
    localparam [AW-1:0] B_W2 = 40'h00_0092_0000, B_W3 = 40'h00_0093_0000;

    // NMU0 -- JTAG debug bridge, 32-bit, control clock, reaches everything.
    localparam integer NSEG0 = 10;
    localparam [NSEG0*AW-1:0] S0_BASE =
        {B_W3, B_W2, B_W1, B_W0, B_MC, B_DDR, B_M3, B_M2, B_M1, B_M0};
    localparam [NSEG0*AW-1:0] S0_MASK =
        {MSK_64K, MSK_64K, MSK_64K, MSK_64K, MSK_64K, MSK_1M,
         MSK_MESH, MSK_MESH, MSK_MESH, MSK_MESH};
    localparam [NSEG0*DSTW-1:0] S0_DST =
        {4'd8, 4'd7, 4'd6, 4'd5, 4'd3, 4'd4, 4'd2, 4'd2, 4'd1, 4'd0};

    // NMU1 -- XDMA bulk, 512-bit, PCIe clock, mesh memory only.
    localparam integer NSEG1 = 4;
    localparam [NSEG1*AW-1:0]   S1_BASE = {B_M3, B_M2, B_M1, B_M0};
    localparam [NSEG1*AW-1:0]   S1_MASK = {MSK_MESH, MSK_MESH, MSK_MESH, MSK_MESH};
    localparam [NSEG1*DSTW-1:0] S1_DST  = {4'd2, 4'd2, 4'd1, 4'd0};

    // NMU2 -- XDMA AXI4-Lite, 32-bit, PCIe clock, control only. The mesh
    // windows are absent from this table, which is v5's exclude_bd_addr_seg.
    localparam integer NSEG2 = 6;
    localparam [NSEG2*AW-1:0] S2_BASE = {B_W3, B_W2, B_W1, B_W0, B_MC, B_DDR};
    localparam [NSEG2*AW-1:0] S2_MASK =
        {MSK_64K, MSK_64K, MSK_64K, MSK_64K, MSK_64K, MSK_1M};
    localparam [NSEG2*DSTW-1:0] S2_DST = {4'd8, 4'd7, 4'd6, 4'd5, 4'd3, 4'd4};

    // -------------------------------------------------------- per-port config
    wire [NS-1:0] sclk, srstn;
    assign sclk  = { clk_ctrl, clk_ctrl, clk_ctrl, clk_ctrl,     // S8..S5 wiz
                     clk_ddr,                                    // S4 ddr ctrl
                     clk_mesh,                                   // S3 mesh ctrl
                     clk_ctrl,                                   // S2 leaf2
                     clk_mesh,                                   // S1 mesh mem
                     clk_ctrl };                                 // S0 leaf0
    assign srstn = { aresetn_ctrl, aresetn_ctrl, aresetn_ctrl, aresetn_ctrl,
                     aresetn_ddr, aresetn_mesh, aresetn_ctrl, aresetn_mesh,
                     aresetn_ctrl };

    // ------------------------------------------------------------- fabric
    wire [NM-1:0]         q_valid, q_ready, q_wr, q_head, q_last;
    wire [NM*DSTW-1:0]    q_dst, q_dpt;
    wire [NM*TAGW-1:0]    q_tag;
    wire [NM*AW-1:0]      q_addr;
    wire [NM*8-1:0]       q_len;
    wire [NM*3-1:0]       q_size;
    wire [NM*FW-1:0]      q_data;
    wire [NM*(FW/8)-1:0]  q_strb;

    wire [NS-1:0]         e_valid, e_ready;
    wire [SRCW-1:0]       e_src;
    wire [TAGW-1:0]       e_tag;
    wire                  e_wr, e_head, e_last;
    wire [AW-1:0]         e_addr;
    wire [7:0]            e_len;
    wire [2:0]            e_size;
    wire [FW-1:0]         e_data;
    wire [FW/8-1:0]       e_strb;

    wire [NS-1:0]         p_valid, p_ready, p_wr, p_last;
    wire [NS*SRCW-1:0]    p_dst;
    wire [NS*TAGW-1:0]    p_tag;
    wire [NS*2-1:0]       p_resp;
    wire [NS*FW-1:0]      p_data;

    wire [NM-1:0]         d_valid, d_ready;
    wire [TAGW-1:0]       d_tag;
    wire                  d_wr, d_last;
    wire [1:0]            d_resp;
    wire [FW-1:0]         d_data;

    sb_station #(.NM(NM), .NS(NS), .FW(FW), .AW(AW), .TAGW(TAGW)) u_stn (
        .clk(bus_clk), .rst(bus_rst),
        .nm_req_valid(q_valid), .nm_req_ready(q_ready), .nm_req_dst(q_dst),
        .nm_req_dport(q_dpt), .nm_req_src({NM*SRCW{1'b0}}),
        .nm_req_tag(q_tag), .nm_req_wr(q_wr), .nm_req_head(q_head),
        .nm_req_last(q_last), .nm_req_addr(q_addr), .nm_req_len(q_len),
        .nm_req_size(q_size), .nm_req_data(q_data), .nm_req_strb(q_strb),
        .ns_req_valid(e_valid), .ns_req_ready(e_ready), .ns_req_src(e_src),
        .ns_req_dport(),
        .ns_req_tag(e_tag), .ns_req_wr(e_wr), .ns_req_head(e_head),
        .ns_req_last(e_last), .ns_req_addr(e_addr), .ns_req_len(e_len),
        .ns_req_size(e_size), .ns_req_data(e_data), .ns_req_strb(e_strb),
        .ns_rsp_valid(p_valid), .ns_rsp_ready(p_ready), .ns_rsp_dst(p_dst),
        .ns_rsp_tag(p_tag), .ns_rsp_wr(p_wr), .ns_rsp_last(p_last),
        .ns_rsp_resp(p_resp), .ns_rsp_data(p_data),
        .nm_rsp_valid(d_valid), .nm_rsp_ready(d_ready), .nm_rsp_dst(),
        .nm_rsp_tag(d_tag),
        .nm_rsp_wr(d_wr), .nm_rsp_last(d_last), .nm_rsp_resp(d_resp),
        .nm_rsp_data(d_data)
    );

    // --------------------------------------------------------------- managers
    wire [31:0] dc0, dc1, dc2;
    assign stat_decerr = dc0 + dc1 + dc2;

    // A narrow port drives only the low bits of its MAXW slot; the rest would
    // float, which is a synthesis warning and an X in simulation.
    assign mp_rdata[0*MAXW + 32 +: MAXW-32] = {(MAXW-32){1'b0}};
    assign mp_rdata[2*MAXW + 32 +: MAXW-32] = {(MAXW-32){1'b0}};

`define SB_NMU_FLIT(I) \
        .req_valid(q_valid[I]), .req_ready(q_ready[I]), \
        .req_dst(q_dst[(I)*DSTW +: DSTW]), \
        .req_dport(q_dpt[(I)*DSTW +: DSTW]), \
        .req_tag(q_tag[(I)*TAGW +: TAGW]), \
        .req_wr(q_wr[I]), .req_head(q_head[I]), .req_last(q_last[I]), \
        .req_addr(q_addr[(I)*AW +: AW]), .req_len(q_len[(I)*8 +: 8]), \
        .req_size(q_size[(I)*3 +: 3]), .req_data(q_data[(I)*FW +: FW]), \
        .req_strb(q_strb[(I)*(FW/8) +: FW/8]), \
        .rsp_valid(d_valid[I]), .rsp_ready(d_ready[I]), .rsp_tag(d_tag), \
        .rsp_wr(d_wr), .rsp_last(d_last), .rsp_resp(d_resp), .rsp_data(d_data)

`define SB_NMU_AXI(I, W) \
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

    sb_nmu #(.MW(32), .MIDW(MAXID), .AW(AW), .FW(FW), .TAGW(TAGW),
             .DSTW(DSTW), .LUT_PER_BRAM(LUT_PER_BRAM),
             .STORE_FWD(STORE_FWD), .NSEG(NSEG0), .REQ_DEPTH(16), .RSP_DEPTH(P_BULK_RSP),
             // Single-beat control port: the 4 KB bound would size it for 256.
             .MAX_BURST(1),
             .REQ_MEM(P_SHAL), .RSP_MEM(P_DEEP),
             .SEG_BASE(S0_BASE), .SEG_MASK(S0_MASK), .SEG_XLT(S0_BASE),
             .SEG_DST(S0_DST), .SEG_DPORT(S0_DST), .SEG_VLD({NSEG0{1'b1}})) u_nmu0 (
        .s_aclk(clk_ctrl), .s_aresetn(aresetn_ctrl),
        `SB_NMU_AXI(0, 32), .bus_clk(bus_clk), .bus_rst(bus_rst),
        `SB_NMU_FLIT(0), .stat_decerr(dc0)
    );

    sb_nmu #(.MW(512), .MIDW(MAXID), .AW(AW), .FW(FW), .TAGW(TAGW),
             .DSTW(DSTW), .LUT_PER_BRAM(LUT_PER_BRAM),
             .STORE_FWD(STORE_FWD), .NSEG(NSEG1),
             .REQ_DEPTH(P_BULK_REQ), .RSP_DEPTH(P_BULK_RSP),
             .REQ_MEM(P_DEEP), .RSP_MEM(P_DEEP),
             .SEG_BASE(S1_BASE), .SEG_MASK(S1_MASK), .SEG_XLT(S1_BASE),
             .SEG_DST(S1_DST), .SEG_DPORT(S1_DST), .SEG_VLD({NSEG1{1'b1}})) u_nmu1 (
        .s_aclk(clk_xdma), .s_aresetn(aresetn_xdma),
        `SB_NMU_AXI(1, 512), .bus_clk(bus_clk), .bus_rst(bus_rst),
        `SB_NMU_FLIT(1), .stat_decerr(dc1)
    );

    sb_nmu #(.MW(32), .MIDW(MAXID), .AW(AW), .FW(FW), .TAGW(TAGW),
             .DSTW(DSTW), .LUT_PER_BRAM(LUT_PER_BRAM),
             .STORE_FWD(STORE_FWD), .NSEG(NSEG2), .REQ_DEPTH(4), .RSP_DEPTH(4),
             .MAX_BURST(1),
             .REQ_MEM(P_SHAL), .RSP_MEM(P_SHAL),
             .SEG_BASE(S2_BASE), .SEG_MASK(S2_MASK), .SEG_XLT(S2_BASE),
             .SEG_DST(S2_DST), .SEG_DPORT(S2_DST), .SEG_VLD({NSEG2{1'b1}})) u_nmu2 (
        .s_aclk(clk_xdma), .s_aresetn(aresetn_xdma),
        `SB_NMU_AXI(2, 32), .bus_clk(bus_clk), .bus_rst(bus_rst),
        `SB_NMU_FLIT(2), .stat_decerr(dc2)
    );

    // ----------------------------------------------------------- subordinates
    genvar i;
    generate
    for (i = 0; i < NS; i = i + 1) begin : g_nsu
        localparam integer DW = (i < 3) ? FW : 32;
        if (DW < MAXW) begin : g_pad
            assign sp_wdata[i*MAXW + DW +: MAXW-DW] = {(MAXW-DW){1'b0}};
            assign sp_wstrb[i*(MAXW/8) + DW/8 +: (MAXW-DW)/8] =
                   {((MAXW-DW)/8){1'b0}};
        end
        sb_nsu #(.SDW(DW), .SIDW(MAXID), .AW(AW), .FW(FW), .TAGW(TAGW),
                 .SRCW(SRCW), .WOST(OST), .ROST(OST),
                 .LUT_PER_BRAM(LUT_PER_BRAM), .TIMEOUT(TIMEOUT),
                 .REQ_DEPTH(16), .RSP_DEPTH(16),
                 .REQ_MEM(P_SHAL), .RSP_MEM(P_SHAL),
                 .CHAN_MEM(P_SHAL)) u_nsu (
            .bus_clk(bus_clk), .bus_rst(bus_rst),
            .req_valid(e_valid[i]), .req_ready(e_ready[i]), .req_src(e_src),
            .req_tag(e_tag), .req_wr(e_wr), .req_head(e_head),
            .req_last(e_last), .req_addr(e_addr), .req_len(e_len),
            .req_size(e_size), .req_data(e_data), .req_strb(e_strb),
            .rsp_valid(p_valid[i]), .rsp_ready(p_ready[i]),
            .rsp_dst(p_dst[i*SRCW +: SRCW]), .rsp_tag(p_tag[i*TAGW +: TAGW]),
            .rsp_wr(p_wr[i]), .rsp_last(p_last[i]),
            .rsp_resp(p_resp[i*2 +: 2]), .rsp_data(p_data[i*FW +: FW]),
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

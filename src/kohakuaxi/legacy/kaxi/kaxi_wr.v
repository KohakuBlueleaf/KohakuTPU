// Configurable write engine: M masters -> N homes, MODE 0 = SASD (one global write
// in flight, data broadcast) or 1 = SAMD (per-home parallel write). AWID is owner-
// tagged so B routes back. Pairs with kaxi_rd; kaxi_xbar composes the two so read
// and write modes are set independently.

`default_nettype none

module kaxi_wr #(
    parameter integer M        = 4,
    parameter integer N_HOME   = 4,
    parameter integer ADDR_W   = 40,
    parameter integer DATA_W   = 512,
    parameter integer ID_W     = 4,
    parameter integer HOME_LSB = 32,
    parameter integer MODE     = 1,               // 0 = SASD, 1 = SAMD
    parameter integer HIDX_W   = (N_HOME <= 1) ? 1 : $clog2(N_HOME),
    parameter integer MIDX_W   = (M <= 1) ? 1 : $clog2(M),
    parameter integer SID_W    = ID_W + MIDX_W
)(
    input  wire                    clk,
    input  wire                    resetn,
    input  wire [M*ID_W-1:0]       s_awid,
    input  wire [M*ADDR_W-1:0]     s_awaddr,
    input  wire [M*8-1:0]          s_awlen,
    input  wire [M*3-1:0]          s_awsize,
    input  wire [M*2-1:0]          s_awburst,
    input  wire [M-1:0]            s_awvalid,
    output reg  [M-1:0]            s_awready,
    input  wire [M*DATA_W-1:0]     s_wdata,
    input  wire [M*(DATA_W/8)-1:0] s_wstrb,
    input  wire [M-1:0]            s_wlast,
    input  wire [M-1:0]            s_wvalid,
    output reg  [M-1:0]            s_wready,
    output reg  [M*ID_W-1:0]       s_bid,
    output reg  [M*2-1:0]          s_bresp,
    output reg  [M-1:0]            s_bvalid,
    input  wire [M-1:0]            s_bready,
    output reg  [N_HOME*SID_W-1:0]     m_awid,
    output reg  [N_HOME*ADDR_W-1:0]    m_awaddr,
    output reg  [N_HOME*8-1:0]         m_awlen,
    output reg  [N_HOME*3-1:0]         m_awsize,
    output reg  [N_HOME*2-1:0]         m_awburst,
    output reg  [N_HOME-1:0]           m_awvalid,
    input  wire [N_HOME-1:0]           m_awready,
    output reg  [N_HOME*DATA_W-1:0]    m_wdata,
    output reg  [N_HOME*(DATA_W/8)-1:0] m_wstrb,
    output reg  [N_HOME-1:0]           m_wlast,
    output reg  [N_HOME-1:0]           m_wvalid,
    input  wire [N_HOME-1:0]           m_wready,
    input  wire [N_HOME*SID_W-1:0]     m_bid,
    input  wire [N_HOME*2-1:0]         m_bresp,
    input  wire [N_HOME-1:0]           m_bvalid,
    output reg  [N_HOME-1:0]           m_bready
);
    localparam integer STRB_W = DATA_W/8;
    integer h, k, mm;
    reg [MIDX_W:0] rot;

    generate
    if (MODE != 0) begin : g_samd
        // ---- per-home parallel write (SAMD) ----
        reg  [1:0]        wst  [0:N_HOME-1];
        reg  [MIDX_W-1:0] wcur [0:N_HOME-1];
        reg  [MIDX_W-1:0] wdsel[0:N_HOME-1];
        reg  [MIDX_W-1:0] wrr  [0:N_HOME-1];
        reg  [M-1:0]      wbusy;
        reg  [HIDX_W-1:0] whome [0:M-1];
        reg  [MIDX_W-1:0] wg [0:N_HOME-1]; reg [N_HOME-1:0] wg_v; reg [MIDX_W-1:0] cw;
        always @(*) begin
            for (h = 0; h < N_HOME; h = h + 1) begin
                wg[h] = 0; wg_v[h] = 1'b0;
                for (k = 0; k < M; k = k + 1) begin
                    rot = {1'b0, wrr[h]} + k[MIDX_W:0];
                    if (rot >= M[MIDX_W:0]) begin
                        rot = rot - M[MIDX_W:0];
                    end
                    cw  = rot[MIDX_W-1:0];
                    if (!wg_v[h] && s_awvalid[cw] && !wbusy[cw]
                        && (s_awaddr[cw*ADDR_W + HOME_LSB +: HIDX_W] == h[HIDX_W-1:0]))
                        begin wg[h] = cw; wg_v[h] = 1'b1; end
                end
            end
        end
        always @(*) begin
            s_awready = {M{1'b0}}; s_wready = {M{1'b0}}; s_bvalid = {M{1'b0}};
            s_bid = 0; s_bresp = 0;
            m_awvalid = 0; m_wvalid = 0; m_bready = 0;
            m_awid = 0; m_awaddr = 0; m_awlen = 0; m_awsize = 0; m_awburst = 0;
            for (h = 0; h < N_HOME; h = h + 1) begin
                if ((wst[h] == 2'd0) && wg_v[h] && m_awready[h]) begin
                    s_awready[wg[h]] = 1'b1;
                end
                if (wst[h] == 2'd1) begin
                    s_wready[wcur[h]] = m_wready[h];
                end
                if (wst[h] == 2'd2) begin
                    s_bvalid[wcur[h]] = m_bvalid[h];
                end
                if ((wst[h] == 2'd0) && wg_v[h]) begin
                    m_awvalid[h] = 1'b1;
                    m_awid   [h*SID_W +: SID_W]   = {wg[h], s_awid[wg[h]*ID_W +: ID_W]};
                    m_awaddr [h*ADDR_W +: ADDR_W] = s_awaddr[wg[h]*ADDR_W +: ADDR_W];
                    m_awlen  [h*8 +: 8]           = s_awlen [wg[h]*8 +: 8];
                    m_awsize [h*3 +: 3]           = s_awsize[wg[h]*3 +: 3];
                    m_awburst[h*2 +: 2]           = s_awburst[wg[h]*2 +: 2];
                end
                if (wst[h] == 2'd1) begin
                    m_wvalid[h] = s_wvalid[wcur[h]];
                end
                if (wst[h] == 2'd2) begin
                    m_bready[h] = s_bready[wcur[h]];
                end
            end
            for (mm = 0; mm < M; mm = mm + 1) begin
                s_bid  [mm*ID_W +: ID_W] = m_bid  [whome[mm]*SID_W +: ID_W];
                s_bresp[mm*2 +: 2]       = m_bresp[whome[mm]*2 +: 2];
            end
        end
        // Dedicated select wdsel (home->master, mux-only fanout) mirrors kaxi_rd's
        // rhome: shared wcur made this a 2-LUT/bit cone, wdsel packs it to 1 LUT6/bit.
        always @(*) begin
            m_wdata = 0; m_wstrb = 0; m_wlast = 0;
            for (h = 0; h < N_HOME; h = h + 1) begin
                m_wdata[h*DATA_W +: DATA_W] = s_wdata[wdsel[h]*DATA_W +: DATA_W];
                m_wstrb[h*STRB_W +: STRB_W] = s_wstrb[wdsel[h]*STRB_W +: STRB_W];
                m_wlast[h]                  = s_wlast[wdsel[h]];
            end
        end
        always @(posedge clk) begin
            if (!resetn) begin
                wbusy <= 0;
                for (h = 0; h < N_HOME; h = h + 1) begin
                    wst[h] <= 0; wrr[h] <= 0; wdsel[h] <= 0;
                end
                for (mm = 0; mm < M; mm = mm + 1) begin
                    whome[mm] <= 0;
                end
            end else begin
                for (h = 0; h < N_HOME; h = h + 1) begin
                    case (wst[h])
                        2'd0: if (wg_v[h] && m_awready[h]) begin
                                wcur[h] <= wg[h]; wdsel[h] <= wg[h]; whome[wg[h]] <= h[HIDX_W-1:0];
                                wrr[h] <= (wg[h] == (M-1)) ? {MIDX_W{1'b0}} : (wg[h] + 1'b1);
                                wbusy[wg[h]] <= 1'b1; wst[h] <= 2'd1;
                            end
                        2'd1: if (s_wvalid[wcur[h]] && m_wready[h] && s_wlast[wcur[h]]) begin
                                wst[h] <= 2'd2;
                            end
                        2'd2: if (m_bvalid[h] && s_bready[wcur[h]]) begin
                                wbusy[wcur[h]] <= 1'b0; wst[h] <= 2'd0;
                            end
                        default: wst[h] <= 2'd0;
                    endcase
                end
            end
        end
    end else begin : g_sasd
        // ---- one global write in flight (SASD) ----
        reg  [1:0]        gw;
        reg  [MIDX_W-1:0] gw_m; reg [HIDX_W-1:0] gw_h; reg [MIDX_W-1:0] gw_rr;
        reg  [MIDX_W-1:0] am; reg av; reg [HIDX_W-1:0] ah;
        always @(*) begin
            am = 0; av = 1'b0; ah = 0;
            for (k = 0; k < M; k = k + 1) begin
                rot = {1'b0, gw_rr} + k[MIDX_W:0];
                if (rot >= M[MIDX_W:0]) begin
                    rot = rot - M[MIDX_W:0];
                end
                if (!av && s_awvalid[rot[MIDX_W-1:0]]) begin
                    am = rot[MIDX_W-1:0]; av = 1'b1;
                    ah = s_awaddr[am*ADDR_W + HOME_LSB +: HIDX_W];
                end
            end
        end
        always @(*) begin
            s_awready = {M{1'b0}}; s_wready = {M{1'b0}}; s_bvalid = {M{1'b0}};
            s_bid = 0; s_bresp = 0;
            m_awvalid = 0; m_wvalid = 0; m_bready = 0;
            m_awid = 0; m_awaddr = 0; m_awlen = 0; m_awsize = 0; m_awburst = 0;
            m_wdata = 0; m_wstrb = 0; m_wlast = 0;
            if ((gw == 2'd0) && av && m_awready[ah]) begin
                s_awready[am] = 1'b1;
            end
            if (gw == 2'd1) begin
                s_wready[gw_m] = m_wready[gw_h];
            end
            if (gw == 2'd2) begin
                s_bvalid[gw_m] = m_bvalid[gw_h];
                s_bid  [gw_m*ID_W +: ID_W] = m_bid[gw_h*SID_W +: ID_W];
                s_bresp[gw_m*2 +: 2]       = m_bresp[gw_h*2 +: 2];
            end
            for (h = 0; h < N_HOME; h = h + 1) begin
                if ((gw == 2'd0) && av && (ah == h[HIDX_W-1:0])) begin
                    m_awvalid[h] = 1'b1;
                    m_awid   [h*SID_W +: SID_W]   = {am, s_awid[am*ID_W +: ID_W]};
                    m_awaddr [h*ADDR_W +: ADDR_W] = s_awaddr[am*ADDR_W +: ADDR_W];
                    m_awlen  [h*8 +: 8]           = s_awlen [am*8 +: 8];
                    m_awsize [h*3 +: 3]           = s_awsize[am*3 +: 3];
                    m_awburst[h*2 +: 2]           = s_awburst[am*2 +: 2];
                end
                if ((gw == 2'd1) && (gw_h == h[HIDX_W-1:0])) begin
                    m_wvalid[h] = s_wvalid[gw_m];
                end
                if ((gw == 2'd2) && (gw_h == h[HIDX_W-1:0])) begin
                    m_bready[h] = s_bready[gw_m];
                end
                m_wdata[h*DATA_W +: DATA_W] = s_wdata[gw_m*DATA_W +: DATA_W];
                m_wstrb[h*STRB_W +: STRB_W] = s_wstrb[gw_m*STRB_W +: STRB_W];
                m_wlast[h]                  = s_wlast[gw_m];
            end
        end
        always @(posedge clk) begin
            if (!resetn) begin
                gw <= 0; gw_rr <= 0;
            end else begin
                case (gw)
                    2'd0: if (av && m_awready[ah]) begin
                            gw_m <= am; gw_h <= ah;
                            gw_rr <= (am == (M-1)) ? {MIDX_W{1'b0}} : (am + 1'b1);
                            gw <= 2'd1;
                        end
                    2'd1: if (s_wvalid[gw_m] && m_wready[gw_h] && s_wlast[gw_m]) begin
                            gw <= 2'd2;
                        end
                    2'd2: if (m_bvalid[gw_h] && s_bready[gw_m]) begin
                            gw <= 2'd0;
                        end
                    default: gw <= 2'd0;
                endcase
            end
        end
    end
    endgenerate
endmodule

`default_nettype wire

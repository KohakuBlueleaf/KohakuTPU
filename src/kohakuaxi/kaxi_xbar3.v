// KohakuAXI M x N crossbar -- SASD-write + SAMD-read mixture (shrink of xbar2).
// Reads stay per-home parallel (bandwidth). WRITES share ONE global datapath: a
// single write is in flight bus-wide, so m_wdata is one M:1 512b mux BROADCAST to
// every home (only the target home asserts m_wvalid) instead of xbar2's N per-home
// muxes. Trades write concurrency for ~ (N-1) x one wide mux of LUT. Same ports as
// kaxi_xbar2. Superseded by kx_mempath_e (the fused xbar-cache).

`default_nettype none

module kaxi_xbar3 #(
    parameter integer M        = 4,
    parameter integer N_HOME   = 4,
    parameter integer ADDR_W   = 40,
    parameter integer DATA_W   = 512,
    parameter integer ID_W     = 4,
    parameter integer HOME_LSB = 32,
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
    input  wire [M*ID_W-1:0]       s_arid,
    input  wire [M*ADDR_W-1:0]     s_araddr,
    input  wire [M*8-1:0]          s_arlen,
    input  wire [M*3-1:0]          s_arsize,
    input  wire [M*2-1:0]          s_arburst,
    input  wire [M-1:0]            s_arvalid,
    output reg  [M-1:0]            s_arready,
    output reg  [M*ID_W-1:0]       s_rid,
    output reg  [M*DATA_W-1:0]     s_rdata,
    output reg  [M*2-1:0]          s_rresp,
    output reg  [M-1:0]            s_rlast,
    output reg  [M-1:0]            s_rvalid,
    input  wire [M-1:0]            s_rready,

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
    output reg  [N_HOME-1:0]           m_bready,
    output reg  [N_HOME*SID_W-1:0]     m_arid,
    output reg  [N_HOME*ADDR_W-1:0]    m_araddr,
    output reg  [N_HOME*8-1:0]         m_arlen,
    output reg  [N_HOME*3-1:0]         m_arsize,
    output reg  [N_HOME*2-1:0]         m_arburst,
    output reg  [N_HOME-1:0]           m_arvalid,
    input  wire [N_HOME-1:0]           m_arready,
    input  wire [N_HOME*SID_W-1:0]     m_rid,
    input  wire [N_HOME*DATA_W-1:0]    m_rdata,
    input  wire [N_HOME*2-1:0]         m_rresp,
    input  wire [N_HOME-1:0]           m_rlast,
    input  wire [N_HOME-1:0]           m_rvalid,
    output reg  [N_HOME-1:0]           m_rready
);
    localparam integer STRB_W = DATA_W/8;
    localparam [1:0] GW_IDLE=0, GW_DATA=1, GW_RESP=2;   // global write FSM
    localparam       RI=1'b0, RD=1'b1;                  // per-home read FSM
    integer h, k;

    // ---- global single write in flight ----
    reg  [1:0]        gw_st;
    reg  [MIDX_W-1:0] gw_m;     // master being written
    reg  [HIDX_W-1:0] gw_h;     // its target home
    reg  [MIDX_W-1:0] gw_rr;    // round-robin ptr

    reg  [MIDX_W-1:0] gaw_m; reg gaw_v; reg [HIDX_W-1:0] gaw_h; reg [MIDX_W-1:0] cw;
    always @(*) begin
        gaw_m = 0; gaw_v = 1'b0; gaw_h = 0;
        for (k = 0; k < M; k = k + 1) begin
            cw = (gw_rr + k) % M;
            if (!gaw_v && s_awvalid[cw]) begin
                gaw_m = cw; gaw_v = 1'b1;
                gaw_h = s_awaddr[cw*ADDR_W + HOME_LSB +: HIDX_W];
            end
        end
    end

    // ---- per-home read state ----
    reg               rst  [0:N_HOME-1];
    reg  [MIDX_W-1:0] curr [0:N_HOME-1];
    reg  [MIDX_W-1:0] rrr  [0:N_HOME-1];
    reg  [M-1:0]      rbusy;
    reg  [MIDX_W-1:0] arg [0:N_HOME-1]; reg [N_HOME-1:0] arg_v; reg [MIDX_W-1:0] cr;
    always @(*) begin
        for (h = 0; h < N_HOME; h = h + 1) begin
            arg[h] = 0; arg_v[h] = 1'b0;
            for (k = 0; k < M; k = k + 1) begin
                cr = (rrr[h] + k) % M;
                if (!arg_v[h] && s_arvalid[cr] && !rbusy[cr] &&
                    (s_araddr[cr*ADDR_W + HOME_LSB +: HIDX_W] == h[HIDX_W-1:0]))
                    begin arg[h] = cr; arg_v[h] = 1'b1; end
            end
        end
    end

    // ---- master-facing outputs ----
    always @(*) begin
        s_awready = {M{1'b0}}; s_wready = {M{1'b0}}; s_bvalid = {M{1'b0}};
        s_arready = {M{1'b0}}; s_rvalid = {M{1'b0}};
        s_bid = 0; s_bresp = 0; s_rid = 0; s_rdata = 0; s_rresp = 0; s_rlast = 0;
        // write (global)
        if ((gw_st == GW_IDLE) && gaw_v && m_awready[gaw_h]) s_awready[gaw_m] = 1'b1;
        if (gw_st == GW_DATA) s_wready[gw_m] = m_wready[gw_h];
        if (gw_st == GW_RESP) begin
            s_bvalid[gw_m] = m_bvalid[gw_h];
            s_bid  [gw_m*ID_W +: ID_W] = m_bid[gw_h*SID_W +: ID_W];
            s_bresp[gw_m*2 +: 2]       = m_bresp[gw_h*2 +: 2];
        end
        // read (per home)
        for (h = 0; h < N_HOME; h = h + 1) begin
            if ((rst[h] == RI) && arg_v[h] && m_arready[h]) s_arready[arg[h]] = 1'b1;
            if (rst[h] == RD) begin
                s_rvalid[curr[h]] = m_rvalid[h];
                s_rid  [curr[h]*ID_W +: ID_W]     = m_rid[h*SID_W +: ID_W];
                s_rdata[curr[h]*DATA_W +: DATA_W] = m_rdata[h*DATA_W +: DATA_W];
                s_rresp[curr[h]*2 +: 2]           = m_rresp[h*2 +: 2];
                s_rlast[curr[h]]                  = m_rlast[h];
            end
        end
    end

    // ---- home-facing outputs ----
    always @(*) begin
        m_awvalid = 0; m_wvalid = 0; m_bready = 0; m_arvalid = 0; m_rready = 0;
        m_awid = 0; m_awaddr = 0; m_awlen = 0; m_awsize = 0; m_awburst = 0;
        m_wdata = 0; m_wstrb = 0; m_wlast = 0;
        m_arid = 0; m_araddr = 0; m_arlen = 0; m_arsize = 0; m_arburst = 0;
        // write: AW to gaw_h; W/B broadcast payload, gated per home
        for (h = 0; h < N_HOME; h = h + 1) begin
            if ((gw_st == GW_IDLE) && gaw_v && (gaw_h == h[HIDX_W-1:0])) begin
                m_awvalid[h] = 1'b1;
                m_awid   [h*SID_W +: SID_W]   = {gaw_m, s_awid[gaw_m*ID_W +: ID_W]};
                m_awaddr [h*ADDR_W +: ADDR_W] = s_awaddr[gaw_m*ADDR_W +: ADDR_W];
                m_awlen  [h*8 +: 8]           = s_awlen [gaw_m*8 +: 8];
                m_awsize [h*3 +: 3]           = s_awsize[gaw_m*3 +: 3];
                m_awburst[h*2 +: 2]           = s_awburst[gaw_m*2 +: 2];
            end
            if ((gw_st == GW_DATA) && (gw_h == h[HIDX_W-1:0]))
                m_wvalid[h] = s_wvalid[gw_m];
            if ((gw_st == GW_RESP) && (gw_h == h[HIDX_W-1:0]))
                m_bready[h] = s_bready[gw_m];
            // read AR (per home)
            if (rst[h] == RI) begin
                m_arvalid[h] = arg_v[h];
                m_arid   [h*SID_W +: SID_W]   = {arg[h], s_arid[arg[h]*ID_W +: ID_W]};
                m_araddr [h*ADDR_W +: ADDR_W] = s_araddr[arg[h]*ADDR_W +: ADDR_W];
                m_arlen  [h*8 +: 8]           = s_arlen [arg[h]*8 +: 8];
                m_arsize [h*3 +: 3]           = s_arsize[arg[h]*3 +: 3];
                m_arburst[h*2 +: 2]           = s_arburst[arg[h]*2 +: 2];
            end
            if (rst[h] == RD) m_rready[h] = s_rready[curr[h]];
        end
        // the ONE shared write datapath, broadcast (mux on gw_m only)
        for (h = 0; h < N_HOME; h = h + 1) begin
            m_wdata[h*DATA_W +: DATA_W] = s_wdata[gw_m*DATA_W +: DATA_W];
            m_wstrb[h*STRB_W +: STRB_W] = s_wstrb[gw_m*STRB_W +: STRB_W];
            m_wlast[h]                  = s_wlast[gw_m];
        end
    end

    // ---- sequential ----
    always @(posedge clk) begin
        if (!resetn) begin
            gw_st <= GW_IDLE; gw_rr <= 0; rbusy <= 0;
            for (h = 0; h < N_HOME; h = h + 1) begin rst[h] <= RI; rrr[h] <= 0; end
        end else begin
            // global write
            case (gw_st)
            GW_IDLE: if (gaw_v && m_awready[gaw_h]) begin
                        gw_m <= gaw_m; gw_h <= gaw_h; gw_rr <= (gaw_m + 1'b1) % M;
                        gw_st <= GW_DATA;
                    end
            GW_DATA: if (s_wvalid[gw_m] && m_wready[gw_h] && s_wlast[gw_m])
                        gw_st <= GW_RESP;
            GW_RESP: if (m_bvalid[gw_h] && s_bready[gw_m]) gw_st <= GW_IDLE;
            default: gw_st <= GW_IDLE;
            endcase
            // per-home read
            for (h = 0; h < N_HOME; h = h + 1) begin
                case (rst[h])
                RI: if (arg_v[h] && m_arready[h]) begin
                        curr[h] <= arg[h]; rrr[h] <= (arg[h] + 1'b1) % M;
                        rbusy[arg[h]] <= 1'b1; rst[h] <= RD;
                    end
                RD: if (m_rvalid[h] && s_rready[curr[h]] && m_rlast[h]) begin
                        rbusy[curr[h]] <= 1'b0; rst[h] <= RI;
                    end
                default: rst[h] <= RI;
                endcase
            end
        end
    end

endmodule

`default_nettype wire

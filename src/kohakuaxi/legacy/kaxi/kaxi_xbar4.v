// KohakuAXI M x N crossbar -- FULL SAMD (parallel read AND write lanes), the memory
// path's required form (a cache behind a SASD-write xbar caps writes at one lane).
// Structured as xbar3's clean per-home read mirrored onto the write side: each home
// has its own write arbiter + FSM + M:1 data mux, so N writes to N homes proceed at
// once. Cost over xbar3 = the N per-home write-data muxes (vs one global broadcast).
// AR and AW share the home decode (address[HOME_LSB +: HIDX_W]); only the datapaths
// are separate. Same ports as kaxi_xbar2/3.

`default_nettype none

module kaxi_xbar4 #(
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
    localparam RI=1'b0, RD=1'b1;                   // per-home read FSM
    integer h, k;
    reg [MIDX_W:0] rot;                            // rotate temp, one extra bit

    // ================= per-home WRITE state (mirror of read) =================
    reg  [1:0]        wst  [0:N_HOME-1];            // 0 idle, 1 data, 2 resp
    reg  [MIDX_W-1:0] wcur [0:N_HOME-1];
    reg  [MIDX_W-1:0] wrr  [0:N_HOME-1];
    reg  [M-1:0]      wbusy;
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

    // ================= per-home READ state (as xbar3) ========================
    reg               rst  [0:N_HOME-1];
    reg  [MIDX_W-1:0] curr [0:N_HOME-1];
    reg  [MIDX_W-1:0] rrr  [0:N_HOME-1];
    reg  [M-1:0]      rbusy;
    reg  [MIDX_W-1:0] arg [0:N_HOME-1]; reg [N_HOME-1:0] arg_v; reg [MIDX_W-1:0] cr;
    always @(*) begin
        for (h = 0; h < N_HOME; h = h + 1) begin
            arg[h] = 0; arg_v[h] = 1'b0;
            for (k = 0; k < M; k = k + 1) begin
                rot = {1'b0, rrr[h]} + k[MIDX_W:0];
                if (rot >= M[MIDX_W:0]) begin
                    rot = rot - M[MIDX_W:0];
                end
                cr  = rot[MIDX_W-1:0];
                if (!arg_v[h] && s_arvalid[cr] && !rbusy[cr]
                    && (s_araddr[cr*ADDR_W + HOME_LSB +: HIDX_W] == h[HIDX_W-1:0]))
                    begin arg[h] = cr; arg_v[h] = 1'b1; end
            end
        end
    end

    // ================= master-facing outputs =================================
    always @(*) begin
        s_awready = {M{1'b0}}; s_wready = {M{1'b0}}; s_bvalid = {M{1'b0}};
        s_arready = {M{1'b0}}; s_rvalid = {M{1'b0}};
        s_bid = 0; s_bresp = 0; s_rid = 0; s_rdata = 0; s_rresp = 0; s_rlast = 0;
        for (h = 0; h < N_HOME; h = h + 1) begin
            // write (per home)
            if ((wst[h] == 2'd0) && wg_v[h] && m_awready[h]) begin
                s_awready[wg[h]] = 1'b1;
            end
            if (wst[h] == 2'd1) begin
                s_wready[wcur[h]] = m_wready[h];
            end
            if (wst[h] == 2'd2) begin
                s_bvalid[wcur[h]]                = m_bvalid[h];
                s_bid  [wcur[h]*ID_W +: ID_W]    = m_bid[h*SID_W +: ID_W];
                s_bresp[wcur[h]*2 +: 2]          = m_bresp[h*2 +: 2];
            end
            // read (per home)
            if ((rst[h] == RI) && arg_v[h] && m_arready[h]) begin
                s_arready[arg[h]] = 1'b1;
            end
            if (rst[h] == RD) begin
                s_rvalid[curr[h]]                 = m_rvalid[h];
                s_rid  [curr[h]*ID_W +: ID_W]     = m_rid[h*SID_W +: ID_W];
                s_rdata[curr[h]*DATA_W +: DATA_W] = m_rdata[h*DATA_W +: DATA_W];
                s_rresp[curr[h]*2 +: 2]           = m_rresp[h*2 +: 2];
                s_rlast[curr[h]]                  = m_rlast[h];
            end
        end
    end

    // ================= home-facing outputs ===================================
    always @(*) begin
        m_awvalid = 0; m_wvalid = 0; m_bready = 0; m_arvalid = 0; m_rready = 0;
        m_awid = 0; m_awaddr = 0; m_awlen = 0; m_awsize = 0; m_awburst = 0;
        m_wdata = 0; m_wstrb = 0; m_wlast = 0;
        m_arid = 0; m_araddr = 0; m_arlen = 0; m_arsize = 0; m_arburst = 0;
        for (h = 0; h < N_HOME; h = h + 1) begin
            // write AW/W/B (per home)
            if ((wst[h] == 2'd0) && wg_v[h]) begin
                m_awvalid[h] = 1'b1;
                m_awid   [h*SID_W +: SID_W]   = {wg[h], s_awid[wg[h]*ID_W +: ID_W]};
                m_awaddr [h*ADDR_W +: ADDR_W] = s_awaddr[wg[h]*ADDR_W +: ADDR_W];
                m_awlen  [h*8 +: 8]           = s_awlen [wg[h]*8 +: 8];
                m_awsize [h*3 +: 3]           = s_awsize[wg[h]*3 +: 3];
                m_awburst[h*2 +: 2]           = s_awburst[wg[h]*2 +: 2];
            end
            // per-home write-data mux: this is the SAMD cost vs xbar3's broadcast
            m_wdata[h*DATA_W +: DATA_W] = s_wdata[wcur[h]*DATA_W +: DATA_W];
            m_wstrb[h*STRB_W +: STRB_W] = s_wstrb[wcur[h]*STRB_W +: STRB_W];
            m_wlast[h]                  = s_wlast[wcur[h]];
            if (wst[h] == 2'd1) begin
                m_wvalid[h] = s_wvalid[wcur[h]];
            end
            if (wst[h] == 2'd2) begin
                m_bready[h] = s_bready[wcur[h]];
            end
            // read AR (per home)
            if (rst[h] == RI) begin
                m_arvalid[h] = arg_v[h];
                m_arid   [h*SID_W +: SID_W]   = {arg[h], s_arid[arg[h]*ID_W +: ID_W]};
                m_araddr [h*ADDR_W +: ADDR_W] = s_araddr[arg[h]*ADDR_W +: ADDR_W];
                m_arlen  [h*8 +: 8]           = s_arlen [arg[h]*8 +: 8];
                m_arsize [h*3 +: 3]           = s_arsize[arg[h]*3 +: 3];
                m_arburst[h*2 +: 2]           = s_arburst[arg[h]*2 +: 2];
            end
            if (rst[h] == RD) begin
                m_rready[h] = s_rready[curr[h]];
            end
        end
    end

    // ================= sequential ============================================
    always @(posedge clk) begin
        if (!resetn) begin
            wbusy <= 0; rbusy <= 0;
            for (h = 0; h < N_HOME; h = h + 1) begin
                wst[h] <= 2'd0; wrr[h] <= 0; rst[h] <= RI; rrr[h] <= 0;
            end
        end else begin
            for (h = 0; h < N_HOME; h = h + 1) begin
                // per-home write
                case (wst[h])
                    2'd0: if (wg_v[h] && m_awready[h]) begin
                            wcur[h] <= wg[h];
                            wrr[h] <= (wg[h] == (M-1)) ? {MIDX_W{1'b0}} : (wg[h] + 1'b1);
                            wbusy[wg[h]] <= 1'b1; wst[h] <= 2'd1;
                        end
                    2'd1: if (s_wvalid[wcur[h]] && m_wready[h] && s_wlast[wcur[h]])
                            wst[h] <= 2'd2;
                    2'd2: if (m_bvalid[h] && s_bready[wcur[h]]) begin
                            wbusy[wcur[h]] <= 1'b0; wst[h] <= 2'd0;
                        end
                    default: wst[h] <= 2'd0;
                endcase
                // per-home read
                case (rst[h])
                    RI: if (arg_v[h] && m_arready[h]) begin
                            curr[h] <= arg[h];
                            rrr[h] <= (arg[h] == (M-1)) ? {MIDX_W{1'b0}} : (arg[h] + 1'b1);
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

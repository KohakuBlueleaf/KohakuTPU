// kaxi_xbar4 with the read-data output as a CLEAN per-master binary-select mux
// (s_rdata[m] = m_rdata[rhome[m]]) instead of the home-indexed scatter. Tests the
// "mux efficiency, not BRAM" lever for full-SAMD LUT: the switch is combinational,
// so BRAM cannot shrink it, but a clean 4:1 (one LUT6/bit) beats a match-selected
// scatter. Behaviourally identical to kaxi_xbar4 (rhome[m]=h iff curr[h]=m).

`default_nettype none

module kaxi_xbar4b #(
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
    localparam RI=1'b0, RD=1'b1;
    integer h, k, m;
    reg [MIDX_W:0] rot;

    reg  [1:0]        wst  [0:N_HOME-1];
    reg  [MIDX_W-1:0] wcur [0:N_HOME-1];
    reg  [MIDX_W-1:0] wrr  [0:N_HOME-1];
    reg  [M-1:0]      wbusy;
    reg  [HIDX_W-1:0] whome [0:M-1];               // master -> home (writes)
    reg  [MIDX_W-1:0] wg [0:N_HOME-1]; reg [N_HOME-1:0] wg_v; reg [MIDX_W-1:0] cw;
    always @(*) begin
        for (h = 0; h < N_HOME; h = h + 1) begin
            wg[h] = 0; wg_v[h] = 1'b0;
            for (k = 0; k < M; k = k + 1) begin
                rot = {1'b0, wrr[h]} + k[MIDX_W:0];
                if (rot >= M[MIDX_W:0]) rot = rot - M[MIDX_W:0];
                cw  = rot[MIDX_W-1:0];
                if (!wg_v[h] && s_awvalid[cw] && !wbusy[cw] &&
                    (s_awaddr[cw*ADDR_W + HOME_LSB +: HIDX_W] == h[HIDX_W-1:0]))
                    begin wg[h] = cw; wg_v[h] = 1'b1; end
            end
        end
    end

    reg               rst  [0:N_HOME-1];
    reg  [MIDX_W-1:0] curr [0:N_HOME-1];
    reg  [MIDX_W-1:0] rrr  [0:N_HOME-1];
    reg  [M-1:0]      rbusy;
    reg  [HIDX_W-1:0] rhome [0:M-1];               // master -> home (reads)
    reg  [MIDX_W-1:0] arg [0:N_HOME-1]; reg [N_HOME-1:0] arg_v; reg [MIDX_W-1:0] cr;
    always @(*) begin
        for (h = 0; h < N_HOME; h = h + 1) begin
            arg[h] = 0; arg_v[h] = 1'b0;
            for (k = 0; k < M; k = k + 1) begin
                rot = {1'b0, rrr[h]} + k[MIDX_W:0];
                if (rot >= M[MIDX_W:0]) rot = rot - M[MIDX_W:0];
                cr  = rot[MIDX_W-1:0];
                if (!arg_v[h] && s_arvalid[cr] && !rbusy[cr] &&
                    (s_araddr[cr*ADDR_W + HOME_LSB +: HIDX_W] == h[HIDX_W-1:0]))
                    begin arg[h] = cr; arg_v[h] = 1'b1; end
            end
        end
    end

    // master-facing: ready/valid + B stay home-scattered (1-2 bit, cheap); the WIDE
    // read data + B/R id are clean per-master muxes over whome/rhome.
    always @(*) begin
        s_awready = {M{1'b0}}; s_wready = {M{1'b0}}; s_bvalid = {M{1'b0}};
        s_arready = {M{1'b0}}; s_rvalid = {M{1'b0}};
        s_bid = 0; s_bresp = 0; s_rid = 0; s_rdata = 0; s_rresp = 0; s_rlast = 0;
        for (h = 0; h < N_HOME; h = h + 1) begin
            if ((wst[h] == 2'd0) && wg_v[h] && m_awready[h]) s_awready[wg[h]] = 1'b1;
            if (wst[h] == 2'd1) s_wready[wcur[h]] = m_wready[h];
            if (wst[h] == 2'd2) s_bvalid[wcur[h]] = m_bvalid[h];
            if ((rst[h] == RI) && arg_v[h] && m_arready[h]) s_arready[arg[h]] = 1'b1;
            if (rst[h] == RD) s_rvalid[curr[h]] = m_rvalid[h];
        end
        for (m = 0; m < M; m = m + 1) begin
            s_bid  [m*ID_W +: ID_W]    = m_bid  [whome[m]*SID_W +: ID_W];
            s_bresp[m*2 +: 2]          = m_bresp[whome[m]*2 +: 2];
            s_rid  [m*ID_W +: ID_W]    = m_rid  [rhome[m]*SID_W +: ID_W];
            s_rdata[m*DATA_W +: DATA_W]= m_rdata[rhome[m]*DATA_W +: DATA_W];
            s_rresp[m*2 +: 2]          = m_rresp[rhome[m]*2 +: 2];
            s_rlast[m]                 = m_rlast[rhome[m]];
        end
    end

    always @(*) begin
        m_awvalid = 0; m_wvalid = 0; m_bready = 0; m_arvalid = 0; m_rready = 0;
        m_awid = 0; m_awaddr = 0; m_awlen = 0; m_awsize = 0; m_awburst = 0;
        m_wdata = 0; m_wstrb = 0; m_wlast = 0;
        m_arid = 0; m_araddr = 0; m_arlen = 0; m_arsize = 0; m_arburst = 0;
        for (h = 0; h < N_HOME; h = h + 1) begin
            if ((wst[h] == 2'd0) && wg_v[h]) begin
                m_awvalid[h] = 1'b1;
                m_awid   [h*SID_W +: SID_W]   = {wg[h], s_awid[wg[h]*ID_W +: ID_W]};
                m_awaddr [h*ADDR_W +: ADDR_W] = s_awaddr[wg[h]*ADDR_W +: ADDR_W];
                m_awlen  [h*8 +: 8]           = s_awlen [wg[h]*8 +: 8];
                m_awsize [h*3 +: 3]           = s_awsize[wg[h]*3 +: 3];
                m_awburst[h*2 +: 2]           = s_awburst[wg[h]*2 +: 2];
            end
            m_wdata[h*DATA_W +: DATA_W] = s_wdata[wcur[h]*DATA_W +: DATA_W];
            m_wstrb[h*STRB_W +: STRB_W] = s_wstrb[wcur[h]*STRB_W +: STRB_W];
            m_wlast[h]                  = s_wlast[wcur[h]];
            if (wst[h] == 2'd1) m_wvalid[h] = s_wvalid[wcur[h]];
            if (wst[h] == 2'd2) m_bready[h] = s_bready[wcur[h]];
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
    end

    always @(posedge clk) begin
        if (!resetn) begin
            wbusy <= 0; rbusy <= 0;
            for (h = 0; h < N_HOME; h = h + 1) begin
                wst[h] <= 2'd0; wrr[h] <= 0; rst[h] <= RI; rrr[h] <= 0;
            end
            for (m = 0; m < M; m = m + 1) begin rhome[m] <= 0; whome[m] <= 0; end
        end else begin
            for (h = 0; h < N_HOME; h = h + 1) begin
                case (wst[h])
                2'd0: if (wg_v[h] && m_awready[h]) begin
                        wcur[h] <= wg[h]; whome[wg[h]] <= h[HIDX_W-1:0];
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
                case (rst[h])
                RI: if (arg_v[h] && m_arready[h]) begin
                        curr[h] <= arg[h]; rhome[arg[h]] <= h[HIDX_W-1:0];
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

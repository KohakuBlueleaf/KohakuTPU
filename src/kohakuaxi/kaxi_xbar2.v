// KohakuAXI M x N same-width AXI4 crossbar -- min-area rewrite (not axi_n1, not
// the station bus). Per-home read + write FSMs, single-outstanding PER HOME:
// each home owns one write and one read master at a time, so W follows AW with
// no order FIFO and responses route by a plain mux (a master is the current
// owner of at most one home). Combinational route, 2-deep skids only, ZERO BRAM,
// single clock. The master index rides the top of the ID for the return path.
// v1 trades write/read throughput per home for area+simplicity; multi-outstanding
// is a measured upgrade. Superseded by kx_mempath_e (the fused xbar-cache).

`default_nettype none

module kaxi_xbar2 #(
    parameter integer M        = 4,
    parameter integer N_HOME   = 4,
    parameter integer ADDR_W   = 40,
    parameter integer DATA_W   = 256,
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
    localparam [1:0] WI = 2'd0, WD = 2'd1, WB = 2'd2;  // write FSM
    localparam       RI = 1'b0, RD = 1'b1;             // read FSM
    integer h, m;

    // per-home state
    reg  [1:0]        wst  [0:N_HOME-1];
    reg               rst  [0:N_HOME-1];
    reg  [MIDX_W-1:0] curw [0:N_HOME-1];   // master owning this home's write
    reg  [MIDX_W-1:0] curr [0:N_HOME-1];   // master owning this home's read
    reg  [MIDX_W-1:0] rrw  [0:N_HOME-1];   // round-robin ptr, write
    reg  [MIDX_W-1:0] rrr  [0:N_HOME-1];   // round-robin ptr, read
    // Per-master single-outstanding gate: a master owns <=1 home per channel, so
    // W/R never broadcast. (A master may issue AWs to many homes; we serialise.)
    reg  [M-1:0]      wbusy, rbusy;

    // combinational per-home grant (round-robin among masters targeting home h)
    reg  [MIDX_W-1:0] awg [0:N_HOME-1]; reg [N_HOME-1:0] awg_v;
    reg  [MIDX_W-1:0] arg [0:N_HOME-1]; reg [N_HOME-1:0] arg_v;
    integer k; reg [MIDX_W-1:0] cand;
    always @(*) begin
        for (h = 0; h < N_HOME; h = h + 1) begin
            awg[h] = {MIDX_W{1'b0}}; awg_v[h] = 1'b0;
            arg[h] = {MIDX_W{1'b0}}; arg_v[h] = 1'b0;
            for (k = 0; k < M; k = k + 1) begin
                cand = (rrw[h] + k) % M;
                if (!awg_v[h] && s_awvalid[cand] && !wbusy[cand] &&
                    (s_awaddr[cand*ADDR_W + HOME_LSB +: HIDX_W] == h[HIDX_W-1:0]))
                    begin awg[h] = cand; awg_v[h] = 1'b1; end
                cand = (rrr[h] + k) % M;
                if (!arg_v[h] && s_arvalid[cand] && !rbusy[cand] &&
                    (s_araddr[cand*ADDR_W + HOME_LSB +: HIDX_W] == h[HIDX_W-1:0]))
                    begin arg[h] = cand; arg_v[h] = 1'b1; end
            end
        end
    end

    // ---- master-facing (s_*) outputs: aggregate the per-home contributions.
    // A master owns <=1 home for write and <=1 for read, so these are plain OR/mux.
    always @(*) begin
        s_awready = {M{1'b0}}; s_wready = {M{1'b0}}; s_bvalid = {M{1'b0}};
        s_arready = {M{1'b0}}; s_rvalid = {M{1'b0}};
        s_bid = {M*ID_W{1'b0}}; s_bresp = {M*2{1'b0}};
        s_rid = {M*ID_W{1'b0}}; s_rdata = {M*DATA_W{1'b0}};
        s_rresp = {M*2{1'b0}}; s_rlast = {M{1'b0}};
        for (h = 0; h < N_HOME; h = h + 1) begin
            // write accept of the granted master
            if ((wst[h] == WI) && awg_v[h] && m_awready[h])
                s_awready[awg[h]] = 1'b1;
            if (wst[h] == WD) begin
                s_wready[curw[h]] = m_wready[h];
            end
            if (wst[h] == WB) begin
                s_bvalid[curw[h]] = m_bvalid[h];
                s_bid  [curw[h]*ID_W +: ID_W] = m_bid[h*SID_W +: ID_W];
                s_bresp[curw[h]*2 +: 2]       = m_bresp[h*2 +: 2];
            end
            // read accept of the granted master
            if ((rst[h] == RI) && arg_v[h] && m_arready[h])
                s_arready[arg[h]] = 1'b1;
            if (rst[h] == RD) begin
                s_rvalid[curr[h]] = m_rvalid[h];
                s_rid  [curr[h]*ID_W +: ID_W] = m_rid[h*SID_W +: ID_W];
                s_rdata[curr[h]*DATA_W +: DATA_W] = m_rdata[h*DATA_W +: DATA_W];
                s_rresp[curr[h]*2 +: 2]       = m_rresp[h*2 +: 2];
                s_rlast[curr[h]]              = m_rlast[h];
            end
        end
    end

    // ---- home-facing (m_*) outputs ----
    always @(*) begin
        m_awvalid = {N_HOME{1'b0}}; m_wvalid = {N_HOME{1'b0}};
        m_bready  = {N_HOME{1'b0}};
        m_arvalid = {N_HOME{1'b0}}; m_rready = {N_HOME{1'b0}};
        m_awid = 0; m_awaddr = 0; m_awlen = 0; m_awsize = 0; m_awburst = 0;
        m_wdata = 0; m_wstrb = 0; m_wlast = 0;
        m_arid = 0; m_araddr = 0; m_arlen = 0; m_arsize = 0; m_arburst = 0;
        for (h = 0; h < N_HOME; h = h + 1) begin
            if (wst[h] == WI) begin
                m_awvalid[h] = awg_v[h];
                m_awid   [h*SID_W +: SID_W]  = {awg[h], s_awid[awg[h]*ID_W +: ID_W]};
                m_awaddr [h*ADDR_W +: ADDR_W]= s_awaddr[awg[h]*ADDR_W +: ADDR_W];
                m_awlen  [h*8 +: 8]          = s_awlen [awg[h]*8 +: 8];
                m_awsize [h*3 +: 3]          = s_awsize[awg[h]*3 +: 3];
                m_awburst[h*2 +: 2]          = s_awburst[awg[h]*2 +: 2];
            end
            if (wst[h] == WD) begin
                m_wvalid[h]                  = s_wvalid[curw[h]];
                m_wdata [h*DATA_W +: DATA_W] = s_wdata[curw[h]*DATA_W +: DATA_W];
                m_wstrb [h*STRB_W +: STRB_W] = s_wstrb[curw[h]*STRB_W +: STRB_W];
                m_wlast [h]                  = s_wlast[curw[h]];
            end
            if (wst[h] == WB) m_bready[h] = s_bready[curw[h]];
            if (rst[h] == RI) begin
                m_arvalid[h] = arg_v[h];
                m_arid   [h*SID_W +: SID_W]  = {arg[h], s_arid[arg[h]*ID_W +: ID_W]};
                m_araddr [h*ADDR_W +: ADDR_W]= s_araddr[arg[h]*ADDR_W +: ADDR_W];
                m_arlen  [h*8 +: 8]          = s_arlen [arg[h]*8 +: 8];
                m_arsize [h*3 +: 3]          = s_arsize[arg[h]*3 +: 3];
                m_arburst[h*2 +: 2]          = s_arburst[arg[h]*2 +: 2];
            end
            if (rst[h] == RD) m_rready[h] = s_rready[curr[h]];
        end
    end

    // ---- per-home FSMs ----
    always @(posedge clk) begin
        if (!resetn) begin
            wbusy <= {M{1'b0}}; rbusy <= {M{1'b0}};
            for (h = 0; h < N_HOME; h = h + 1) begin
                wst[h] <= WI; rst[h] <= RI;
                rrw[h] <= 0;  rrr[h] <= 0;
            end
        end else begin
            for (h = 0; h < N_HOME; h = h + 1) begin
                // write
                case (wst[h])
                WI: if (awg_v[h] && m_awready[h]) begin
                        curw[h] <= awg[h]; rrw[h] <= (awg[h] + 1'b1) % M;
                        wbusy[awg[h]] <= 1'b1; wst[h] <= WD;
                    end
                WD: if (s_wvalid[curw[h]] && m_wready[h] && s_wlast[curw[h]])
                        wst[h] <= WB;
                WB: if (m_bvalid[h] && s_bready[curw[h]]) begin
                        wbusy[curw[h]] <= 1'b0; wst[h] <= WI;
                    end
                default: wst[h] <= WI;
                endcase
                // read
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

// M AXI4 masters -> 1 AXI4 stream, the shared "in-station SASD" concentrator.
// Reused by the station input engine (downstream = decode+pack+flit) and by the
// cheap-SAMD kaxi master side (downstream = route to homes).

// AXI4 has no WID, so ONE write burst holds the shared W path to its WLAST: writes
// serialise here BY THE PROTOCOL, which is the in-station SASD contract. Reads do
// not serialise -- each AR is forwarded with an owner-tagged ID, so many are in
// flight at once and R/B route home by that tag.

// v1 arbiter is a fixed priority encoder (lowest index wins). Round-robin / data-
// master priority is a drop-in on aw_grant/ar_grant; policy barely moves the LUT.

`default_nettype none

module sb_axi_concentrate #(
    parameter integer M    = 3,
    parameter integer DW   = 512,
    parameter integer AW   = 40,
    parameter integer IDW  = 4,
    parameter integer OW   = (M <= 1) ? 1 : $clog2(M),
    parameter integer OIDW = IDW + OW
)(
    input  wire                 clk,
    input  wire                 rst,

    input  wire [M*IDW-1:0]     s_awid,
    input  wire [M*AW-1:0]      s_awaddr,
    input  wire [M*8-1:0]       s_awlen,
    input  wire [M*3-1:0]       s_awsize,
    input  wire [M*2-1:0]       s_awburst,
    input  wire [M-1:0]         s_awvalid,
    output wire [M-1:0]         s_awready,
    input  wire [M*DW-1:0]      s_wdata,
    input  wire [M*(DW/8)-1:0]  s_wstrb,
    input  wire [M-1:0]         s_wlast,
    input  wire [M-1:0]         s_wvalid,
    output wire [M-1:0]         s_wready,
    output wire [M*IDW-1:0]     s_bid,
    output wire [M*2-1:0]       s_bresp,
    output wire [M-1:0]         s_bvalid,
    input  wire [M-1:0]         s_bready,
    input  wire [M*IDW-1:0]     s_arid,
    input  wire [M*AW-1:0]      s_araddr,
    input  wire [M*8-1:0]       s_arlen,
    input  wire [M*3-1:0]       s_arsize,
    input  wire [M*2-1:0]       s_arburst,
    input  wire [M-1:0]         s_arvalid,
    output wire [M-1:0]         s_arready,
    output wire [M*IDW-1:0]     s_rid,
    output wire [M*DW-1:0]      s_rdata,
    output wire [M*2-1:0]       s_rresp,
    output wire [M-1:0]         s_rlast,
    output wire [M-1:0]         s_rvalid,
    input  wire [M-1:0]         s_rready,

    output wire [OIDW-1:0]      m_awid,
    output wire [AW-1:0]        m_awaddr,
    output wire [7:0]           m_awlen,
    output wire [2:0]           m_awsize,
    output wire [1:0]           m_awburst,
    output wire                 m_awvalid,
    input  wire                 m_awready,
    output wire [DW-1:0]        m_wdata,
    output wire [DW/8-1:0]      m_wstrb,
    output wire                 m_wlast,
    output wire                 m_wvalid,
    input  wire                 m_wready,
    input  wire [OIDW-1:0]      m_bid,
    input  wire [1:0]           m_bresp,
    input  wire                 m_bvalid,
    output wire                 m_bready,
    output wire [OIDW-1:0]      m_arid,
    output wire [AW-1:0]        m_araddr,
    output wire [7:0]           m_arlen,
    output wire [2:0]           m_arsize,
    output wire [1:0]           m_arburst,
    output wire                 m_arvalid,
    input  wire                 m_arready,
    input  wire [OIDW-1:0]      m_rid,
    input  wire [DW-1:0]        m_rdata,
    input  wire [1:0]           m_rresp,
    input  wire                 m_rlast,
    input  wire                 m_rvalid,
    output wire                 m_rready
);
    integer gi;

    // ============================================================= write path
    reg  [OW-1:0] w_own;
    reg           w_busy;                 // a write burst holds the W path

    reg  [OW-1:0] aw_grant;
    reg           aw_gv;
    always @(*) begin
        aw_grant = {OW{1'b0}};
        aw_gv    = 1'b0;
        for (gi = M-1; gi >= 0; gi = gi - 1)
            if (s_awvalid[gi]) begin aw_grant = gi[OW-1:0]; aw_gv = 1'b1; end
    end

    wire aw_fire = !w_busy && aw_gv && m_awready;   // AW accepted, lock owner

    assign m_awvalid = !w_busy && aw_gv;
    assign m_awid    = {aw_grant, s_awid [aw_grant*IDW +: IDW]};
    assign m_awaddr  =            s_awaddr[aw_grant*AW  +: AW];
    assign m_awlen   =            s_awlen [aw_grant*8   +: 8];
    assign m_awsize  =            s_awsize[aw_grant*3   +: 3];
    assign m_awburst =            s_awburst[aw_grant*2  +: 2];

    // W streams from the locked owner until WLAST.
    assign m_wvalid  = w_busy && s_wvalid[w_own];
    assign m_wdata   = s_wdata[w_own*DW     +: DW];
    assign m_wstrb   = s_wstrb[w_own*(DW/8) +: DW/8];
    assign m_wlast   = s_wlast[w_own];

    genvar gm;
    generate
    for (gm = 0; gm < M; gm = gm + 1) begin : g_wch
        assign s_awready[gm] = !w_busy && aw_gv && (aw_grant == gm[OW-1:0]) && m_awready;
        assign s_wready [gm] = w_busy && (w_own == gm[OW-1:0]) && m_wready;
    end
    endgenerate

    always @(posedge clk) begin
        if (rst) begin
            w_busy <= 1'b0;
            w_own  <= {OW{1'b0}};
        end else begin
            if (aw_fire) begin
                w_own  <= aw_grant;
                w_busy <= 1'b1;
            end else if (w_busy && m_wvalid && m_wready && m_wlast) begin
                w_busy <= 1'b0;
            end
        end
    end

    // B response demux by owner tag.
    wire [OW-1:0] b_own = m_bid[OIDW-1 -: OW];
    generate
    for (gm = 0; gm < M; gm = gm + 1) begin : g_bch
        assign s_bvalid[gm]        = m_bvalid && (b_own == gm[OW-1:0]);
        assign s_bid [gm*IDW +: IDW] = m_bid[IDW-1:0];
        assign s_bresp[gm*2 +: 2]   = m_bresp;
    end
    endgenerate
    assign m_bready = s_bready[b_own];

    // ============================================================== read path
    reg  [OW-1:0] ar_grant;
    reg           ar_gv;
    always @(*) begin
        ar_grant = {OW{1'b0}};
        ar_gv    = 1'b0;
        for (gi = M-1; gi >= 0; gi = gi - 1)
            if (s_arvalid[gi]) begin ar_grant = gi[OW-1:0]; ar_gv = 1'b1; end
    end

    assign m_arvalid = ar_gv;
    assign m_arid    = {ar_grant, s_arid [ar_grant*IDW +: IDW]};
    assign m_araddr  =            s_araddr[ar_grant*AW  +: AW];
    assign m_arlen   =            s_arlen [ar_grant*8   +: 8];
    assign m_arsize  =            s_arsize[ar_grant*3   +: 3];
    assign m_arburst =            s_arburst[ar_grant*2  +: 2];

    generate
    for (gm = 0; gm < M; gm = gm + 1) begin : g_arch
        assign s_arready[gm] = ar_gv && (ar_grant == gm[OW-1:0]) && m_arready;
    end
    endgenerate

    // R response demux by owner tag (data/last broadcast, valid/ready per owner).
    wire [OW-1:0] r_own = m_rid[OIDW-1 -: OW];
    generate
    for (gm = 0; gm < M; gm = gm + 1) begin : g_rch
        assign s_rvalid[gm]          = m_rvalid && (r_own == gm[OW-1:0]);
        assign s_rid [gm*IDW +: IDW] = m_rid[IDW-1:0];
        assign s_rdata[gm*DW +: DW]  = m_rdata;
        assign s_rresp[gm*2 +: 2]    = m_rresp;
        assign s_rlast[gm]           = m_rlast;
    end
    endgenerate
    assign m_rready = s_rready[r_own];

endmodule

`default_nettype wire

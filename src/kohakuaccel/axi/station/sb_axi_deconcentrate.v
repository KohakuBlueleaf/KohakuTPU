// 1 AXI4 stream -> N AXI4 subordinates, the eject-side mirror of sb_axi_concentrate.
// Port = addr[PSEL_LSB +: PW], held for the burst: serialises across subs (in-station
// SASD) but runs full width within a burst.

`default_nettype none

module sb_axi_deconcentrate #(
    parameter integer N        = 4,
    parameter integer DW       = 512,
    parameter integer AW       = 40,
    parameter integer IDW      = 4,
    parameter integer PSEL_LSB = 16,
    parameter integer PW       = (N <= 1) ? 1 : $clog2(N)
)(
    input  wire                 clk,
    input  wire                 rst,

    input  wire [IDW-1:0]       s_awid,
    input  wire [AW-1:0]        s_awaddr,
    input  wire [7:0]           s_awlen,
    input  wire [2:0]           s_awsize,
    input  wire [1:0]           s_awburst,
    input  wire                 s_awvalid,
    output wire                 s_awready,
    input  wire [DW-1:0]        s_wdata,
    input  wire [DW/8-1:0]      s_wstrb,
    input  wire                 s_wlast,
    input  wire                 s_wvalid,
    output wire                 s_wready,
    output wire [IDW-1:0]       s_bid,
    output wire [1:0]           s_bresp,
    output wire                 s_bvalid,
    input  wire                 s_bready,
    input  wire [IDW-1:0]       s_arid,
    input  wire [AW-1:0]        s_araddr,
    input  wire [7:0]           s_arlen,
    input  wire [2:0]           s_arsize,
    input  wire [1:0]           s_arburst,
    input  wire                 s_arvalid,
    output wire                 s_arready,
    output wire [IDW-1:0]       s_rid,
    output wire [DW-1:0]        s_rdata,
    output wire [1:0]           s_rresp,
    output wire                 s_rlast,
    output wire                 s_rvalid,
    input  wire                 s_rready,

    output wire [N*IDW-1:0]     m_awid,
    output wire [N*AW-1:0]      m_awaddr,
    output wire [N*8-1:0]       m_awlen,
    output wire [N*3-1:0]       m_awsize,
    output wire [N*2-1:0]       m_awburst,
    output wire [N-1:0]         m_awvalid,
    input  wire [N-1:0]         m_awready,
    output wire [N*DW-1:0]      m_wdata,
    output wire [N*(DW/8)-1:0]  m_wstrb,
    output wire [N-1:0]         m_wlast,
    output wire [N-1:0]         m_wvalid,
    input  wire [N-1:0]         m_wready,
    input  wire [N*IDW-1:0]     m_bid,
    input  wire [N*2-1:0]       m_bresp,
    input  wire [N-1:0]         m_bvalid,
    output wire [N-1:0]         m_bready,
    output wire [N*IDW-1:0]     m_arid,
    output wire [N*AW-1:0]      m_araddr,
    output wire [N*8-1:0]       m_arlen,
    output wire [N*3-1:0]       m_arsize,
    output wire [N*2-1:0]       m_arburst,
    output wire [N-1:0]         m_arvalid,
    input  wire [N-1:0]         m_arready,
    input  wire [N*IDW-1:0]     m_rid,
    input  wire [N*DW-1:0]      m_rdata,
    input  wire [N*2-1:0]       m_rresp,
    input  wire [N-1:0]         m_rlast,
    input  wire [N-1:0]         m_rvalid,
    output wire [N-1:0]         m_rready
);
    genvar g;

    // ================================================================ write path
    wire [PW-1:0] aw_port = s_awaddr[PSEL_LSB +: PW];
    reg  [PW-1:0] w_port;
    reg           w_busy;                 // AW accepted, holds until B returns
    wire          aw_fire = !w_busy && s_awvalid && m_awready[aw_port];

    generate for (g = 0; g < N; g = g + 1) begin : g_aw
        assign m_awvalid[g]           = !w_busy && s_awvalid && (aw_port == g[PW-1:0]);
        assign m_awid  [g*IDW +: IDW] = s_awid;
        assign m_awaddr[g*AW  +: AW]  = s_awaddr;
        assign m_awlen [g*8   +: 8]   = s_awlen;
        assign m_awsize[g*3   +: 3]   = s_awsize;
        assign m_awburst[g*2  +: 2]   = s_awburst;
    end endgenerate
    assign s_awready = !w_busy && m_awready[aw_port];

    generate for (g = 0; g < N; g = g + 1) begin : g_w
        assign m_wvalid[g]              = w_busy && s_wvalid && (w_port == g[PW-1:0]);
        assign m_wdata [g*DW +: DW]     = s_wdata;
        assign m_wstrb [g*(DW/8) +: DW/8] = s_wstrb;
        assign m_wlast [g]              = s_wlast;
    end endgenerate
    assign s_wready = w_busy && m_wready[w_port];

    assign s_bvalid = w_busy && m_bvalid[w_port];
    assign s_bid    = m_bid [w_port*IDW +: IDW];
    assign s_bresp  = m_bresp[w_port*2  +: 2];
    generate for (g = 0; g < N; g = g + 1) begin : g_b
        assign m_bready[g] = w_busy && s_bready && (w_port == g[PW-1:0]);
    end endgenerate

    always @(posedge clk) begin
        if (rst) begin
            w_busy <= 1'b0; w_port <= {PW{1'b0}};
        end else if (aw_fire) begin
            w_port <= aw_port; w_busy <= 1'b1;
        end else if (w_busy && s_bvalid && s_bready) begin
            w_busy <= 1'b0;
        end
    end

    // ================================================================= read path
    wire [PW-1:0] ar_port = s_araddr[PSEL_LSB +: PW];
    reg  [PW-1:0] r_port;
    reg           r_busy;
    wire          ar_fire = !r_busy && s_arvalid && m_arready[ar_port];

    generate for (g = 0; g < N; g = g + 1) begin : g_ar
        assign m_arvalid[g]           = !r_busy && s_arvalid && (ar_port == g[PW-1:0]);
        assign m_arid  [g*IDW +: IDW] = s_arid;
        assign m_araddr[g*AW  +: AW]  = s_araddr;
        assign m_arlen [g*8   +: 8]   = s_arlen;
        assign m_arsize[g*3   +: 3]   = s_arsize;
        assign m_arburst[g*2  +: 2]   = s_arburst;
    end endgenerate
    assign s_arready = !r_busy && m_arready[ar_port];

    assign s_rvalid = r_busy && m_rvalid[r_port];
    assign s_rid    = m_rid [r_port*IDW +: IDW];
    assign s_rdata  = m_rdata[r_port*DW +: DW];
    assign s_rresp  = m_rresp[r_port*2  +: 2];
    assign s_rlast  = m_rlast[r_port];
    generate for (g = 0; g < N; g = g + 1) begin : g_r
        assign m_rready[g] = r_busy && s_rready && (r_port == g[PW-1:0]);
    end endgenerate

    always @(posedge clk) begin
        if (rst) begin
            r_busy <= 1'b0; r_port <= {PW{1'b0}};
        end else if (ar_fire) begin
            r_port <= ar_port; r_busy <= 1'b1;
        end else if (r_busy && s_rvalid && s_rready && s_rlast) begin
            r_busy <= 1'b0;
        end
    end
endmodule

`default_nettype wire

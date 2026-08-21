// Burst-to-AXI4-Lite protocol converter, AMD PG059 semantics: one complete
// Lite AW+W+B or AR+R handshake per beat, zero-strobe write beats consumed
// but never issued (a Lite slave may legally ignore WSTRB and would corrupt),
// responses coalesced worst-case (DECERR > SLVERR > OKAY), IDs captured and
// reflected. Single-acceptance per direction; write and read channels run
// independently.

`default_nettype none

module sb_axi2lite #(
    parameter integer DW  = 32,
    parameter integer AW  = 43,
    parameter integer IDW = 4
)(
    input  wire                clk,
    input  wire                resetn,

    // ---- subordinate side: full AXI4, from the station NSU ---------------
    input  wire [IDW-1:0]      s_awid,
    input  wire [AW-1:0]       s_awaddr,
    input  wire [7:0]          s_awlen,
    input  wire                s_awvalid,
    output wire                s_awready,
    input  wire [DW-1:0]       s_wdata,
    input  wire [DW/8-1:0]     s_wstrb,
    input  wire                s_wlast,
    input  wire                s_wvalid,
    output wire                s_wready,
    output reg  [IDW-1:0]      s_bid,
    output reg  [1:0]          s_bresp,
    output reg                 s_bvalid,
    input  wire                s_bready,
    input  wire [IDW-1:0]      s_arid,
    input  wire [AW-1:0]       s_araddr,
    input  wire [7:0]          s_arlen,
    input  wire                s_arvalid,
    output wire                s_arready,
    output reg  [IDW-1:0]      s_rid,
    output reg  [DW-1:0]       s_rdata,
    output reg  [1:0]          s_rresp,
    output reg                 s_rlast,
    output reg                 s_rvalid,
    input  wire                s_rready,

    // ---- manager side: AXI4-Lite -----------------------------------------
    output wire [AW-1:0]       m_awaddr,
    output wire                m_awvalid,
    input  wire                m_awready,
    output wire [DW-1:0]       m_wdata,
    output wire [DW/8-1:0]     m_wstrb,
    output wire                m_wvalid,
    input  wire                m_wready,
    input  wire [1:0]          m_bresp,
    input  wire                m_bvalid,
    output wire                m_bready,
    output wire [AW-1:0]       m_araddr,
    output wire                m_arvalid,
    input  wire                m_arready,
    input  wire [DW-1:0]       m_rdata,
    input  wire [1:0]          m_rresp,
    input  wire                m_rvalid,
    output wire                m_rready
);
    localparam integer STEP = DW/8;

    // ------------------------------------------------------------ write walk
    localparam [2:0] WI = 3'd0, WB = 3'd1, WA = 3'd2, WR = 3'd3, WD = 3'd4;
    reg [2:0]      wst;
    reg [AW-1:0]   waddr;
    reg [7:0]      wlen, wcnt;
    reg [1:0]      wacc;
    reg [DW-1:0]   wdat;
    reg [DW/8-1:0] wstb;
    reg            aw_done, w_done;

    assign s_awready = (wst == WI);
    assign s_wready  = (wst == WB);
    assign m_awaddr  = waddr;
    assign m_awvalid = (wst == WA) && !aw_done;
    assign m_wdata   = wdat;
    assign m_wstrb   = wstb;
    assign m_wvalid  = (wst == WA) && !w_done;
    assign m_bready  = (wst == WR);

    wire w_is_last = (wcnt == wlen);
    wire aw_hs = m_awvalid && m_awready;
    wire w_hs  = m_wvalid && m_wready;

    always @(posedge clk) begin
        if (!resetn) begin
            wst      <= WI;
            s_bvalid <= 1'b0;
        end else begin
            if (s_bvalid && s_bready) s_bvalid <= 1'b0;
            case (wst)
            WI: if (s_awvalid) begin
                    waddr <= s_awaddr;
                    wlen  <= s_awlen;
                    wcnt  <= 8'd0;
                    wacc  <= 2'b00;
                    s_bid <= s_awid;
                    wst   <= WB;
                end
            WB: if (s_wvalid) begin
                    wdat <= s_wdata;
                    wstb <= s_wstrb;
                    if (|s_wstrb) begin
                        aw_done <= 1'b0;
                        w_done  <= 1'b0;
                        wst     <= WA;
                    end else begin
                        waddr <= waddr + STEP;
                        wcnt  <= wcnt + 8'd1;
                        if (w_is_last) begin
                            s_bresp  <= wacc;
                            s_bvalid <= 1'b1;
                            wst      <= WD;
                        end
                    end
                end
            WA: begin
                    if (aw_hs) aw_done <= 1'b1;
                    if (w_hs)  w_done  <= 1'b1;
                    if ((aw_done || aw_hs) && (w_done || w_hs)) wst <= WR;
                end
            WR: if (m_bvalid) begin
                    waddr <= waddr + STEP;
                    wcnt  <= wcnt + 8'd1;
                    if (m_bresp > wacc) wacc <= m_bresp;
                    if (w_is_last) begin
                        s_bresp  <= (m_bresp > wacc) ? m_bresp : wacc;
                        s_bvalid <= 1'b1;
                        wst      <= WD;
                    end else begin
                        wst <= WB;
                    end
                end
            WD: if (!s_bvalid) wst <= WI;
            default: wst <= WI;
            endcase
        end
    end

    // ------------------------------------------------------------- read walk
    localparam [1:0] RI = 2'd0, RA = 2'd1, RW = 2'd2, RF = 2'd3;
    reg [1:0]    rst_q;
    reg [AW-1:0] raddr;
    reg [7:0]    rlen, rcnt;

    assign s_arready = (rst_q == RI);
    assign m_araddr  = raddr;
    assign m_arvalid = (rst_q == RA);
    assign m_rready  = (rst_q == RW);

    always @(posedge clk) begin
        if (!resetn) begin
            rst_q    <= RI;
            s_rvalid <= 1'b0;
        end else begin
            case (rst_q)
            RI: if (s_arvalid) begin
                    raddr <= s_araddr;
                    rlen  <= s_arlen;
                    rcnt  <= 8'd0;
                    s_rid <= s_arid;
                    rst_q <= RA;
                end
            RA: if (m_arready) rst_q <= RW;
            RW: if (m_rvalid) begin
                    s_rdata  <= m_rdata;
                    s_rresp  <= m_rresp;
                    s_rlast  <= (rcnt == rlen);
                    s_rvalid <= 1'b1;
                    rst_q    <= RF;
                end
            RF: if (s_rready) begin
                    s_rvalid <= 1'b0;
                    raddr    <= raddr + STEP;
                    rcnt     <= rcnt + 8'd1;
                    rst_q    <= (rcnt == rlen) ? RI : RA;
                end
            endcase
        end
    end
endmodule

`default_nettype wire

// A surface out as an AXI4 master: WRREQ packets become AW + W beats, RDREQ
// headers become AR, and the responses go back as WRRSP / RDRSP packets on
// VC_RSP. The packet's `tag` rides on the AXI ID, so B and R name their packet
// themselves and nothing is tracked here. One request packet is issued at a
// time; responses are packetised one at a time (a read's beats stream, a B
// waits for the packet in progress). The slave must not interleave read data
// of different IDs.

`default_nettype none

module kts_axi4_out #(
    parameter integer W       = 288,
    parameter integer VC      = 2,
    parameter integer VC_REQ  = 0,
    parameter integer VC_RSP  = 1,
    parameter integer D       = 32,
    parameter integer CMAX    = 64,
    parameter integer CN_W    = 4,
    parameter         MEM     = "distributed",
    parameter integer ID_W    = 4,              // >= the tag bits in use
    parameter integer ADDR_W  = 40,
    parameter integer DATA_W  = 256,
    parameter [7:0]   SRC     = 8'd1,
    parameter integer VCW     = (VC <= 1) ? 1 : $clog2(VC)
)(
    input  wire                 clk,
    input  wire                 rst,

    input  wire                 rx_valid,
    input  wire [VCW-1:0]       rx_vc,
    input  wire                 rx_last,
    input  wire [W-1:0]         rx_flit,
    output wire                 rx_crd_valid,
    output wire [VCW-1:0]       rx_crd_vc,
    output wire [CN_W-1:0]      rx_crd_n,

    output wire                 tx_valid,
    output wire [VCW-1:0]       tx_vc,
    output wire                 tx_last,
    output wire [W-1:0]         tx_flit,
    input  wire                 tx_crd_valid,
    input  wire [VCW-1:0]       tx_crd_vc,
    input  wire [CN_W-1:0]      tx_crd_n,

    output wire [ID_W-1:0]      m_awid,
    output wire [ADDR_W-1:0]    m_awaddr,
    output wire [7:0]           m_awlen,
    output wire [2:0]           m_awsize,
    output wire [1:0]           m_awburst,
    output wire                 m_awvalid,
    input  wire                 m_awready,
    output wire [DATA_W-1:0]    m_wdata,
    output wire [DATA_W/8-1:0]  m_wstrb,
    output wire                 m_wlast,
    output wire                 m_wvalid,
    input  wire                 m_wready,
    input  wire [ID_W-1:0]      m_bid,
    input  wire [1:0]           m_bresp,
    input  wire                 m_bvalid,
    output wire                 m_bready,
    output wire [ID_W-1:0]      m_arid,
    output wire [ADDR_W-1:0]    m_araddr,
    output wire [7:0]           m_arlen,
    output wire [2:0]           m_arsize,
    output wire [1:0]           m_arburst,
    output wire                 m_arvalid,
    input  wire                 m_arready,
    input  wire [ID_W-1:0]      m_rid,
    input  wire [DATA_W-1:0]    m_rdata,
    input  wire [1:0]           m_rresp,
    input  wire                 m_rlast,
    input  wire                 m_rvalid,
    output wire                 m_rready
);
`include "kts_pkt.vh"
    localparam integer BW  = DATA_W / 8;
    localparam integer U_ADDR = KTS_H_USER_LSB;
    localparam integer U_SIZE = U_ADDR + ADDR_W;
    localparam integer U_BRST = U_SIZE + 3;
    localparam integer U_LEN  = U_BRST + 2;
    localparam integer U_ID   = U_LEN + 8;

    // ---- requests: the VC_REQ head ---------------------------------------------
    wire [VC-1:0]   hv, hl;
    wire [VC*W-1:0] hf;
    reg  [VC-1:0]   pop;
    wire [W-1:0]    head   = hf[VC_REQ*W +: W];
    wire            hvalid = hv[VC_REQ];
    wire            hlast  = hl[VC_REQ];
    wire [3:0]      hkind  = head[KTS_H_KIND_LSB +: 4];
    wire [7:0]      htag   = head[KTS_H_TAG_LSB +: 8];

    localparam [1:0] Q_HDR = 2'd0, Q_AW = 2'd1, Q_W = 2'd2, Q_AR = 2'd3;
    reg [1:0]        qs;
    reg [W-1:0]      hq;                         // the header being issued

    // The tag becomes the AXI ID (truncated or zero-extended to ID_W).
    wire [7:0] tag8  = hq[KTS_H_TAG_LSB +: 8];
    assign m_awid    = tag8;
    assign m_awaddr  = hq[U_ADDR +: ADDR_W];
    assign m_awlen   = hq[U_LEN +: 8];
    assign m_awsize  = hq[U_SIZE +: 3];
    assign m_awburst = hq[U_BRST +: 2];
    assign m_awvalid = (qs == Q_AW);
    assign m_arid    = m_awid;
    assign m_araddr  = m_awaddr;
    assign m_arlen   = m_awlen;
    assign m_arsize  = m_awsize;
    assign m_arburst = m_awburst;
    assign m_arvalid = (qs == Q_AR);
    assign m_wvalid  = (qs == Q_W) && hvalid;
    assign m_wdata   = head[DATA_W-1:0];
    assign m_wstrb   = head[DATA_W +: BW];
    assign m_wlast   = hlast;

    wire hdr_pop = (qs == Q_HDR) && hvalid;
    wire w_pop   = m_wvalid && m_wready;
    always @(posedge clk) begin
        if (rst) begin
            qs <= Q_HDR;
        end
        else begin
            case (qs)
                Q_HDR: begin
                    if (hvalid) begin
                        hq <= head;
                        qs <= (hkind == KTS_K_WRREQ) ? Q_AW
                            : (hkind == KTS_K_RDREQ) ? Q_AR : Q_HDR;
                    end
                end
                Q_AW: if (m_awready) begin
                    qs <= Q_W;
                end
                Q_W: if (w_pop && m_wlast) begin
                    qs <= Q_HDR;
                end
                Q_AR: if (m_arready) begin
                    qs <= Q_HDR;
                end
                default: qs <= Q_HDR;
            endcase
        end
    end

    // ---- responses: one packet at a time on VC_RSP ---------------------------------
    localparam [1:0] R_IDLE = 2'd0, R_RHDR = 2'd1, R_RDAT = 2'd2, R_B = 2'd3;
    reg [1:0]       rs;
    reg [W-1:0]     rh;
    reg [VC-1:0]    req_valid, req_last;
    reg [VC*W-1:0]  req_flit;
    wire [VC-1:0]   req_take;
    wire            take = req_take[VC_RSP];

    function [W-1:0] mk_rsp;
        input [3:0]      kind;
        input [ID_W-1:0] id;
        input [1:0]      resp;
        begin
            mk_rsp = {W{1'b0}};
            mk_rsp[KTS_H_KIND_LSB +: KTS_H_KIND_W] = kind;
            mk_rsp[KTS_H_VC_LSB +: KTS_H_VC_W]     = VC_RSP[3:0];
            mk_rsp[KTS_H_DST_LSB +: KTS_H_DST_W]   = 8'd0;
            mk_rsp[KTS_H_SRC_LSB +: KTS_H_SRC_W]   = SRC;
            mk_rsp[KTS_H_TAG_LSB +: KTS_H_TAG_W]   = id;
            mk_rsp[U_ADDR +: 2] = resp;
        end
    endfunction

    // A read response starts when its first beat is here; B waits its turn.
    wire r_start = (rs == R_IDLE) && m_rvalid;
    wire b_start = (rs == R_IDLE) && !m_rvalid && m_bvalid;
    assign m_bready = (rs == R_B) && take;
    assign m_rready = (rs == R_RDAT) && take;

    always @(*) begin
        req_valid = {VC{1'b0}};
        req_last  = {VC{1'b0}};
        req_flit  = {VC{{W{1'b0}}}};
        case (rs)
            R_RHDR: begin
                req_valid[VC_RSP] = 1'b1;
                req_flit[VC_RSP*W +: W] = rh;
            end
            R_RDAT: begin
                req_valid[VC_RSP] = m_rvalid;
                req_last[VC_RSP]  = m_rlast;
                req_flit[VC_RSP*W +: W] = {{(W-DATA_W-2){1'b0}}, m_rresp, m_rdata};
            end
            R_B: begin
                req_valid[VC_RSP] = m_bvalid;
                req_last[VC_RSP]  = 1'b1;
                req_flit[VC_RSP*W +: W] = mk_rsp(KTS_K_WRRSP, m_bid, m_bresp);
            end
            default: begin
            end
        endcase
    end

    always @(posedge clk) begin
        if (rst) begin
            rs <= R_IDLE;
        end
        else begin
            case (rs)
                R_IDLE: begin
                    if (r_start) begin
                        rs <= R_RHDR;
                        rh <= mk_rsp(KTS_K_RDRSP, m_rid, 2'b00);
                    end
                    else if (b_start) begin
                        rs <= R_B;
                    end
                end
                R_RHDR: if (take) begin
                    rs <= R_RDAT;
                end
                R_RDAT: if (take && m_rlast) begin
                    rs <= R_IDLE;
                end
                R_B: if (take) begin
                    rs <= R_IDLE;
                end
                default: rs <= R_IDLE;
            endcase
        end
    end

    always @(*) begin
        pop = {VC{1'b0}};
        pop[VC_REQ] = hdr_pop || w_pop;
    end

    // ---- the two ends ------------------------------------------------------------
    wire [VC*($clog2(CMAX)+1)-1:0] credits_unused;
    kts_rx #(.W(W), .VC(VC), .D(D), .CN_W(CN_W), .MEM(MEM)) u_rx (
        .clk(clk), .rst(rst),
        .rx_valid(rx_valid), .rx_vc(rx_vc), .rx_last(rx_last), .rx_flit(rx_flit),
        .out_valid(hv), .out_last(hl), .out_flit(hf), .out_pop(pop),
        .crd_valid(rx_crd_valid), .crd_vc(rx_crd_vc), .crd_n(rx_crd_n)
    );
    kts_tx #(.W(W), .VC(VC), .CMAX(CMAX), .CN_W(CN_W)) u_tx (
        .clk(clk), .rst(rst),
        .req_valid(req_valid), .req_last(req_last), .req_flit(req_flit),
        .req_take(req_take),
        .tx_valid(tx_valid), .tx_vc(tx_vc), .tx_last(tx_last), .tx_flit(tx_flit),
        .crd_valid(tx_crd_valid), .crd_vc(tx_crd_vc), .crd_n(tx_crd_n),
        .credits(credits_unused)
    );

endmodule

`default_nettype wire

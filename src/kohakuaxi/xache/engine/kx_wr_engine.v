// Write control for the fused xbar-cache over NH same-clock homes. Carries NO wide
// data: it grants one (home,master) source (one-hot `gsel`, registered), and the
// fabric ANDs-ORs that source's W straight onto the home's DRAM/array ports. The
// grant is latched and AW waits for its own ready before W starts, so a slot can
// never change under a pending AW. SAMD = NH 1 per home; SASD = NH per domain.

`default_nettype none

module kx_wr_engine #(
    parameter integer M         = 4,
    parameter integer NH        = 1,
    parameter integer AW        = 40,
    parameter integer W         = 512,
    parameter integer IDW       = 6,
    parameter integer SET_W     = 15,
    parameter integer K         = 1,
    parameter integer WBYTES_LG = $clog2(W/8),
    parameter integer SUBW      = (K <= 1) ? 0 : $clog2(K),
    parameter integer LINE_LSB  = WBYTES_LG + SUBW,
    parameter integer TAG_W     = AW - LINE_LSB - SET_W,
    parameter integer P         = NH * M,
    parameter integer PIDX_W    = (P <= 1) ? 1 : $clog2(P)
)(
    input  wire                 clk,
    input  wire                 resetn,
    input  wire [NH-1:0]        flush_busy,

    input  wire [P-1:0]         mw_qval,
    output wire [P-1:0]         mw_qrdy,
    input  wire [P*IDW-1:0]     mw_qid,
    input  wire [P*AW-1:0]      mw_qaddr,
    input  wire [P*8-1:0]       mw_qlen,
    input  wire [P*3-1:0]       mw_qsize,
    input  wire [P-1:0]         mw_wval,
    output wire [P-1:0]         mw_wrdy,
    input  wire [P-1:0]         mw_wlast,

    output reg  [P-1:0]         gsel,        // one-hot granted source (control)
    output reg  [PIDX_W-1:0]    gidx,        // BINARY granted source: the data mux select
    output reg  [NH-1:0]        hsel,        // one-hot selected home

    output reg                  b_val,
    input  wire                 b_rdy,
    output reg  [IDW-1:0]       b_id,
    output reg  [1:0]           b_resp,

    output wire [NH-1:0]        c_wr_en,
    output wire [SET_W-1:0]     c_wr_idx,
    output wire [TAG_W-1:0]     c_wr_tag,

    output wire [IDW-1:0]       m_awid,
    output wire [AW-1:0]        m_awaddr,
    output wire [7:0]           m_awlen,
    output wire [2:0]           m_awsize,
    output wire [1:0]           m_awburst,
    output reg  [NH-1:0]        m_awvalid,
    input  wire [NH-1:0]        m_awready,
    output wire [NH-1:0]        m_wvalid,
    input  wire [NH-1:0]        m_wready,
    input  wire [NH*2-1:0]      m_bresp,
    input  wire [NH-1:0]        m_bvalid,
    output wire [NH-1:0]        m_bready
);
    function [SET_W-1:0] idx_of; input [AW-1:0] a; begin idx_of = a[LINE_LSB +: SET_W]; end endfunction
    function [TAG_W-1:0] tag_of; input [AW-1:0] a; begin tag_of = a[LINE_LSB+SET_W +: TAG_W]; end endfunction

    reg  [P-1:0] warr_mask;
    wire [P-1:0] elig;
    genvar gp;
    generate for (gp = 0; gp < P; gp = gp + 1) begin : g_el
        assign elig[gp] = mw_qval[gp] && !flush_busy[gp / M];
    end endgenerate
    wire [P-1:0] hi   = elig & warr_mask;
    wire [P-1:0] pick = |hi ? (hi & (~hi + 1'b1)) : (elig & (~elig + 1'b1));
    wire         wg_v = |elig;
    reg  [PIDX_W-1:0] wg_p; integer ek;
    always @(*) begin
        wg_p = 0;
        for (ek = 0; ek < P; ek = ek + 1) begin
            if (pick[ek]) begin
                wg_p = wg_p | ek[PIDX_W-1:0];
            end
        end
    end

    localparam [1:0] W_IDLE=0, W_AW=1, W_DATA=2, W_RESP=3;
    reg [1:0]      wst;
    reg [AW-1:0]   wa;
    reg [IDW-1:0]  wid;
    reg [7:0]      wlen;
    reg [2:0]      wsz;
    reg [AW-1:0]   wa_beat;

    wire src_wval  = |(mw_wval & gsel);
    wire src_wlast = |(mw_wlast & gsel);
    wire h_wready  = |(m_wready & hsel);
    wire h_awready = |(m_awready & hsel);
    wire h_bvalid  = |(m_bvalid & hsel);
    reg [1:0] h_bresp; integer hk;
    always @(*) begin
        h_bresp = 2'b00;
        for (hk = 0; hk < NH; hk = hk + 1) begin
            if (hsel[hk]) begin
                h_bresp = h_bresp | m_bresp[hk*2 +: 2];
            end
        end
    end

    wire wr_beat = (wst == W_DATA) && src_wval && h_wready;

    assign mw_qrdy  = {P{wst == W_IDLE}} & pick;
    assign mw_wrdy  = {P{wr_beat}} & gsel;
    assign m_awid   = wid;  assign m_awaddr = wa;  assign m_awlen = wlen;
    assign m_awsize = wsz;  assign m_awburst = 2'b01;
    assign m_wvalid = {NH{(wst == W_DATA) && src_wval}} & hsel;
    assign m_bready = {NH{wst == W_RESP}} & hsel;
    assign c_wr_en  = {NH{wr_beat}} & hsel;
    assign c_wr_idx = idx_of(wa_beat);
    assign c_wr_tag = tag_of(wa_beat);

    always @(posedge clk) begin
        if (!resetn) begin
            wst <= W_IDLE; m_awvalid <= 0; b_val <= 1'b0; warr_mask <= {P{1'b1}}; gsel <= 0; gidx <= 0; hsel <= 0;
        end else begin
            if (b_val && b_rdy) begin
                b_val <= 1'b0;
            end
            case (wst)
                W_IDLE: if (wg_v) begin
                    wid <= mw_qid[wg_p*IDW +: IDW]; wa <= mw_qaddr[wg_p*AW +: AW];
                    wlen <= mw_qlen[wg_p*8 +: 8]; wsz <= mw_qsize[wg_p*3 +: 3];
                    wa_beat <= mw_qaddr[wg_p*AW +: AW];
                    gsel <= pick; gidx <= wg_p; hsel <= 0; hsel[wg_p / M] <= 1'b1;
                    warr_mask <= ~((pick << 1) - 1'b1);
                    m_awvalid <= 0; m_awvalid[wg_p / M] <= 1'b1;
                    wst <= W_AW;
                end
                W_AW: if (h_awready) begin m_awvalid <= 0; wst <= W_DATA; end
                W_DATA: if (wr_beat) begin
                    wa_beat <= wa_beat + (W/8);
                    if (src_wlast) begin
                        wst <= W_RESP;
                    end
                end
                W_RESP: if (h_bvalid) begin
                    b_id <= wid; b_resp <= h_bresp; b_val <= 1'b1; wst <= W_IDLE;
                end
                default: wst <= W_IDLE;
            endcase
        end
    end
endmodule

`default_nettype wire

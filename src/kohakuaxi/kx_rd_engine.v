// Read control for the fused xbar-cache over NH same-clock homes. Carries NO wide
// data: it issues {idx,tag,sub} to one home (one-hot), gathers only hit/done bits,
// and answers with {id, resp, last} plus r_home -- the one-hot home whose `word`
// the fabric wires straight onto the master's response link. One registered AR
// record, one-hot valid per home. SAMD = NH 1 per home; SASD = NH per domain.

`default_nettype none

module kx_rd_engine #(
    parameter integer M         = 4,
    parameter integer NH        = 1,
    parameter integer AW        = 40,
    parameter integer W         = 512,
    parameter integer IDW       = 6,
    parameter integer SET_W     = 15,
    parameter integer K         = 1,
    parameter         RAM_STYLE = "ultra",
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

    input  wire [P-1:0]         mr_qval,
    output wire [P-1:0]         mr_qrdy,
    input  wire [P*IDW-1:0]     mr_qid,
    input  wire [P*AW-1:0]      mr_qaddr,
    input  wire [P*8-1:0]       mr_qlen,
    input  wire [P*3-1:0]       mr_qsize,

    output reg                  r_val,
    input  wire                 r_rdy,
    output reg  [IDW-1:0]       r_id,
    output reg  [1:0]           r_resp,
    output reg                  r_last,
    output reg  [NH-1:0]        r_home,      // one-hot: whose `word` is the data (control)
    output reg  [((NH<=1)?1:$clog2(NH))-1:0] r_hidx,  // BINARY home index: the data mux select

    output wire [NH-1:0]        c_rd_en,
    output wire [SET_W-1:0]     c_rd_idx,
    output wire [TAG_W-1:0]     c_rd_tag,
    output wire [SUBW:0]        c_rd_sub,
    input  wire [NH-1:0]        c_hit,
    output wire [NH-1:0]        c_fill_go,
    input  wire [NH-1:0]        c_fill_done,

    output wire [IDW-1:0]       m_arid,
    output wire [AW-1:0]        m_araddr,
    output wire [7:0]           m_arlen,
    output wire [2:0]           m_arsize,
    output wire [1:0]           m_arburst,
    output reg  [NH-1:0]        m_arvalid,
    input  wire [NH-1:0]        m_arready,
    input  wire [NH*2-1:0]      m_rresp,
    output wire [NH-1:0]        m_rready
);
    localparam integer RD_LAT = (RAM_STYLE == "ultra") ? 4 : 1;
    localparam integer RD_WAIT = RD_LAT + 1;

    function [SET_W-1:0] idx_of; input [AW-1:0] a; begin idx_of = a[LINE_LSB +: SET_W]; end endfunction
    function [TAG_W-1:0] tag_of; input [AW-1:0] a; begin tag_of = a[LINE_LSB+SET_W +: TAG_W]; end endfunction
    function [SUBW:0]    sub_of; input [AW-1:0] a;
        begin sub_of = (K <= 1) ? {(SUBW+1){1'b0}} : a[WBYTES_LG +: ((SUBW<1)?1:SUBW)]; end endfunction

    reg  [P-1:0] rarr_mask;
    wire [P-1:0] elig;
    genvar gp;
    generate for (gp = 0; gp < P; gp = gp + 1) begin : g_el
        assign elig[gp] = mr_qval[gp] && !flush_busy[gp / M];
    end endgenerate
    wire [P-1:0] hi   = elig & rarr_mask;
    wire [P-1:0] pick = |hi ? (hi & (~hi + 1'b1)) : (elig & (~elig + 1'b1));
    wire         rg_v = |elig;
    reg  [PIDX_W-1:0] rg_p; integer ek;
    always @(*) begin rg_p = 0; for (ek = 0; ek < P; ek = ek + 1) if (pick[ek]) rg_p = rg_p | ek[PIDX_W-1:0]; end

    localparam [2:0] R_IDLE=0, R_ISSUE=1, R_WAIT=2, R_CHK=3, R_FETCH=4, R_DRAIN=5;
    reg [2:0]        rst;
    reg [AW-1:0]     ra;
    reg [7:0]        rleft;
    reg [IDW-1:0]    rid;
    reg [2:0]        rwait;
    reg [NH-1:0]     sel;
    localparam integer HIW = (NH <= 1) ? 1 : $clog2(NH);
    reg [HIW-1:0]    sidx;

    assign c_rd_idx  = idx_of(ra);
    assign c_rd_tag  = tag_of(ra);
    assign c_rd_sub  = sub_of(ra);
    assign c_rd_en   = {NH{rst == R_ISSUE}} & sel;
    assign c_fill_go = {NH{rst == R_FETCH}} & sel;
    assign m_rready  = c_fill_go;

    wire sel_hit  = |(c_hit & sel);
    wire sel_done = |(c_fill_done & sel);
    reg [1:0] sel_resp; integer hk;
    always @(*) begin sel_resp = 2'b00; for (hk = 0; hk < NH; hk = hk + 1) if (sel[hk]) sel_resp = sel_resp | m_rresp[hk*2 +: 2]; end

    assign m_arid    = rid;
    assign m_araddr  = {ra[AW-1:LINE_LSB], {LINE_LSB{1'b0}}};
    assign m_arlen   = K - 1;
    assign m_arsize  = WBYTES_LG[2:0];
    assign m_arburst = 2'b01;
    assign mr_qrdy   = {P{rst == R_IDLE}} & pick;

    always @(posedge clk) begin
        if (!resetn) begin
            rst <= R_IDLE; r_val <= 1'b0; m_arvalid <= 0; rarr_mask <= {P{1'b1}}; sel <= 0; sidx <= 0; r_home <= 0; r_hidx <= 0;
        end else begin
            case (rst)
            R_IDLE: begin
                r_val <= 1'b0;
                if (rg_v) begin
                    ra <= mr_qaddr[rg_p*AW +: AW]; rleft <= mr_qlen[rg_p*8 +: 8];
                    rid <= mr_qid[rg_p*IDW +: IDW];
                    sel <= 0; sel[rg_p / M] <= 1'b1; sidx <= rg_p / M;
                    rarr_mask <= ~((pick << 1) - 1'b1);
                    rst <= R_ISSUE;
                end
            end
            R_ISSUE: begin rwait <= RD_WAIT[2:0]; rst <= R_WAIT; end
            R_WAIT: if (rwait <= 3'd1) rst <= R_CHK; else rwait <= rwait - 3'd1;
            R_CHK: if (sel_hit) begin
                    r_id <= rid; r_resp <= 2'b00; r_last <= (rleft == 8'd0);
                    r_home <= sel; r_hidx <= sidx; r_val <= 1'b1; rst <= R_DRAIN;
                end else begin
                    m_arvalid <= sel; rst <= R_FETCH;
                end
            R_FETCH: begin
                m_arvalid <= m_arvalid & ~m_arready;
                if (sel_done) begin
                    r_id <= rid; r_resp <= sel_resp; r_last <= (rleft == 8'd0);
                    r_home <= sel; r_hidx <= sidx; r_val <= 1'b1; rst <= R_DRAIN;
                end
            end
            R_DRAIN: if (r_val && r_rdy) begin
                r_val <= 1'b0;
                if (r_last) rst <= R_IDLE;
                else begin ra <= ra + (W/8); rleft <= rleft - 8'd1; rst <= R_ISSUE; end
            end
            default: rst <= R_IDLE;
            endcase
        end
    end
endmodule

`default_nettype wire

// Pipelined read control for the Xache over NH same-clock homes (the RD_PIPE=1
// engine). One burst at a time per engine, but the burst STREAMS: a lookup is
// issued every cycle, each landing is taken if it is the beat the master needs
// next and hits, and a miss turns the rest of the burst into ONE DRAM read whose
// beats fill the array as they arrive while the lookups trail behind and hit.
// Nothing wide is buffered: a landing that cannot be taken (the master stalled,
// or it is not this burst's turn) is dropped and its beat re-issued -- the
// array is write-through, so any lookup may be replayed. Responses are ordered
// per master by `r_seq`; the fabric only lets the oldest burst drain.
// Carries NO wide data, like kx_rd_engine.

`default_nettype none

module kx_rd_pipe #(
    parameter integer M         = 4,
    parameter integer NH        = 1,
    parameter integer AW        = 40,
    parameter integer W         = 512,
    parameter integer IDW       = 6,
    parameter integer SET_W     = 15,
    parameter integer K         = 1,
    parameter         RAM_STYLE = "ultra",
    parameter integer SEQW      = 2,
    parameter integer WBYTES_LG = $clog2(W/8),
    parameter integer SUBW      = (K <= 1) ? 0 : $clog2(K),
    parameter integer LINE_LSB  = WBYTES_LG + SUBW,
    parameter integer TAG_W     = AW - LINE_LSB - SET_W,
    parameter integer P         = NH * M,
    parameter integer PIDX_W    = (P <= 1) ? 1 : $clog2(P),
    parameter integer OFFW      = 12 - WBYTES_LG              // beat index within 4 KB
)(
    input  wire                 clk,
    input  wire                 resetn,
    input  wire [NH-1:0]        flush_busy,

    input  wire [P-1:0]         mr_qval,
    output wire [P-1:0]         mr_qrdy,
    input  wire [P*IDW-1:0]     mr_qid,
    input  wire [P*AW-1:0]      mr_qaddr,
    input  wire [P*8-1:0]       mr_qlen,
    input  wire [P*SEQW-1:0]    mr_qseq,

    output reg                  r_val,
    input  wire                 r_rdy,
    output reg  [IDW-1:0]       r_id,
    output reg  [1:0]           r_resp,
    output reg                  r_last,
    output reg  [NH-1:0]        r_home,
    output reg  [((NH<=1)?1:$clog2(NH))-1:0] r_hidx,
    output reg  [SEQW-1:0]      r_seq,

    output wire [NH-1:0]        c_rd_en,
    output wire [SET_W-1:0]     c_rd_idx,
    output wire [TAG_W-1:0]     c_rd_tag,
    output wire [SUBW:0]        c_rd_sub,
    output wire [NH-1:0]        c_rd_take,
    input  wire [NH-1:0]        c_land,
    input  wire [NH-1:0]        c_hit_c,
    output wire [NH-1:0]        c_fill_go,
    output wire [SET_W-1:0]     c_fill_idx,
    output wire [TAG_W-1:0]     c_fill_tag,
    input  wire [NH-1:0]        c_fill_ready,
    input  wire [NH-1:0]        c_fill_done,

    output wire [IDW-1:0]       m_arid,
    output wire [AW-1:0]        m_araddr,
    output wire [7:0]           m_arlen,
    output wire [2:0]           m_arsize,
    output wire [1:0]           m_arburst,
    output reg  [NH-1:0]        m_arvalid,
    input  wire [NH-1:0]        m_arready,
    input  wire [NH-1:0]        m_rvalid,
    input  wire [NH-1:0]        m_rlast,
    input  wire [NH*2-1:0]      m_rresp,
    output wire [NH-1:0]        m_rready
);
    localparam integer RD_LAT = (RAM_STYLE == "ultra") ? 4 : 1;
    localparam integer HIW    = (NH <= 1) ? 1 : $clog2(NH);
    localparam integer NOMISS = 512;                            // fl_lim value before any miss

    function [SET_W-1:0] idx_of; input [AW-1:0] a; begin idx_of = a[LINE_LSB +: SET_W]; end endfunction
    function [TAG_W-1:0] tag_of; input [AW-1:0] a; begin tag_of = a[LINE_LSB+SET_W +: TAG_W]; end endfunction
    function [SUBW:0]    sub_of; input [AW-1:0] a;
        begin sub_of = (K <= 1) ? {(SUBW+1){1'b0}} : a[WBYTES_LG +: ((SUBW<1)?1:SUBW)]; end endfunction

    // ---------------------------------------------------------- arbitration
    reg  [P-1:0] rarr_mask;
    wire [P-1:0] elig;
    genvar gp;
    generate for (gp = 0; gp < P; gp = gp + 1) begin : g_el
        assign elig[gp] = mr_qval[gp] && !flush_busy[gp / M];
    end endgenerate
    wire [P-1:0] hi   = elig & rarr_mask;
    // lowest set bit, one-hot, as a tree of 4-wide groups: one LUT level per
    // stage, three stages cover 256 slots, unused stages fold to constants.
    // `x & (~x + 1)` over 16 slots synthesised as a 13-level LUT ripple, not a
    // carry chain, and held read-SASD 4x4 at 294 MHz; the tree runs it at 381.
    function [3:0] low4; input [3:0] x;
        begin low4 = {x[3] & ~x[2] & ~x[1] & ~x[0], x[2] & ~x[1] & ~x[0], x[1] & ~x[0], x[0]}; end
    endfunction
    function [255:0] low1; input [255:0] x;
        reg [63:0] a1; reg [15:0] a2; reg [3:0] a3;
        reg [3:0]  s3; reg [15:0] s2; reg [63:0] s1; reg [3:0] t;
        integer i;
        begin
            for (i = 0; i < 64; i = i + 1) a1[i] = |x[i*4 +: 4];
            for (i = 0; i < 16; i = i + 1) a2[i] = |a1[i*4 +: 4];
            for (i = 0; i < 4;  i = i + 1) a3[i] = |a2[i*4 +: 4];
            s3 = low4(a3);
            for (i = 0; i < 4;  i = i + 1) begin t = low4(a2[i*4 +: 4]); s2[i*4 +: 4] = t & {4{s3[i]}}; end
            for (i = 0; i < 16; i = i + 1) begin t = low4(a1[i*4 +: 4]); s1[i*4 +: 4] = t & {4{s2[i]}}; end
            for (i = 0; i < 64; i = i + 1) begin t = low4(x[i*4 +: 4]);  low1[i*4 +: 4] = t & {4{s1[i]}}; end
        end
    endfunction
    wire [255:0] hi_p   = {{(256-P){1'b0}}, hi};
    wire [255:0] elig_p = {{(256-P){1'b0}}, elig};
    wire [255:0] low_hi = low1(hi_p);
    wire [255:0] low_el = low1(elig_p);
    wire [P-1:0] pick_c = |hi ? low_hi[P-1:0] : low_el[P-1:0];
    reg  [PIDX_W-1:0] rg_c; integer ek;
    always @(*) begin rg_c = 0; for (ek = 0; ek < P; ek = ek + 1) if (pick_c[ek]) rg_c = rg_c | ek[PIDX_W-1:0]; end
    // the grant is registered: combinational, the arbiter reached the fabric's
    // AR bookkeeping at 14 levels (read-SASD 4x4 at 276 MHz). AXI holds ARVALID
    // until ARREADY, so a pick a cycle old is still valid; it is re-qualified
    // against the live valids all the same.
    reg [P-1:0]      pick;
    reg [PIDX_W-1:0] rg_p;
    always @(posedge clk) begin
        pick <= resetn ? pick_c : {P{1'b0}};
        rg_p <= rg_c;
    end
    wire         rg_v = |(pick & elig);

    // ---------------------------------------------------------- burst record
    reg              busy;
    reg [AW-1:0]     ra;                  // burst start address
    reg [8:0]        rlen;                // beats - 1, zero-extended
    reg [IDW-1:0]    rid;
    reg [NH-1:0]     sel;
    reg [8:0]        lk;                  // next beat to look up
    reg [8:0]        dr;                  // next beat to present
    reg signed [10:0] fl_lim;             // beats below this are look-up-able
    reg              miss_act;            // a DRAM read is in flight
    reg [OFFW-1:0]   fl_off;              // 4 KB-page beat offset of the next fill beat
    reg [AW-1:0]     fl_base;             // page base of the fill (bits below 12 zero)
    reg [8:0]        fl_beats;            // DRAM beats still to arrive

    assign mr_qrdy = {P{!busy && rg_v}} & pick;

    // ---------------------------------------------------------- lookup issue
    // within a 4 KB burst only the page offset moves: one OFFW-bit add per beat
    wire [OFFW-1:0]  lk_off  = ra[WBYTES_LG +: OFFW] + lk[OFFW-1:0];
    wire [AW-1:0]    lk_addr = {ra[AW-1:12], lk_off, ra[WBYTES_LG-1:0]};
    wire             issue   = busy && (lk <= rlen) && ($signed({2'b0, lk}) < fl_lim);
    assign c_rd_en  = {NH{issue}} & sel;
    assign c_rd_idx = idx_of(lk_addr);
    assign c_rd_tag = tag_of(lk_addr);
    assign c_rd_sub = sub_of(lk_addr);

    reg [RD_LAT-1:0] lv;
    reg [8:0]        lb [0:RD_LAT-1];
    integer s;
    generate if (RD_LAT == 1) begin : g_lv1
        always @(posedge clk) lv <= resetn ? issue : 1'b0;
    end else begin : g_lvn
        always @(posedge clk) lv <= resetn ? {lv[RD_LAT-2:0], issue} : {RD_LAT{1'b0}};
    end endgenerate
    always @(posedge clk) begin
        lb[0] <= lk;
        for (s = 1; s < RD_LAT; s = s + 1) lb[s] <= lb[s-1];
    end
    wire       land_v = lv[RD_LAT-1];
    wire [8:0] land_b = lb[RD_LAT-1];
    wire       hit_l  = |(c_hit_c & sel);

    // ---------------------------------------------------------- present / take
    // `accept` arrives late (the master's ready through the fabric's turn gate),
    // so every compare against dr_next is precomputed from registers for both
    // outcomes and only the 2:1 select sits behind accept. With the compares
    // behind accept, 11 levels through a 9-bit carry chain held K=2 at 304 MHz.
    wire       accept  = r_val && r_rdy;
    wire [8:0] dr_next = dr + (accept ? 9'd1 : 9'd0);
    wire       room    = accept || !r_val;
    wire [8:0] dr1     = dr + 9'd1;
    wire       eq0     = (land_b == dr);
    wire       eq1     = (land_b == dr1);
    wire       ge0     = (land_b >= dr);
    wire       ge1     = (land_b >= dr1);
    wire       gt0     = (lk > dr);
    wire       gt1     = (lk > dr1);
    wire       wanted  = land_v && (accept ? eq1 : eq0) && room;
    wire       take    = wanted && hit_l;
    wire       miss_now = wanted && !hit_l && !miss_act;
    // a landing that was not taken and is still needed: every later beat in the
    // pipe will be dropped too, so the issuer restarts at the first beat not
    // already held -- unless it is already there (a restart past a beat being
    // issued this cycle would issue it twice). need = dr_next + held_after =
    // dr + (accept || r_val).
    wire       bump    = accept || r_val;
    wire [8:0] need    = bump ? dr1 : dr;
    wire       restart = land_v && !take && (accept ? ge1 : ge0) && (bump ? gt1 : gt0);
    assign c_rd_take = {NH{take}} & sel;

    // ---------------------------------------------------------- DRAM fetch
    wire [OFFW-1:0] mb_off   = ra[WBYTES_LG +: OFFW] + land_b[OFFW-1:0];      // missing beat
    wire [AW-1:0]   mb_addr  = {ra[AW-1:12], mb_off, ra[WBYTES_LG-1:0]};
    wire [OFFW-1:0] end_off  = ra[WBYTES_LG +: OFFW] + rlen[OFFW-1:0];         // last beat
    // line-aligned span: from the missing beat's line to the last beat's line
    wire [OFFW-1:0] mb_line  = {mb_off[OFFW-1:SUBW], {SUBW{1'b0}}};
    wire [OFFW-1:0] end_line = {end_off[OFFW-1:SUBW], {SUBW{1'b0}}};
    wire [OFFW-1:0] ar_beats = (end_line - mb_line) + (K - 1);               // beats - 1
    reg  [AW-1:0]   ar_addr;
    reg  [7:0]      ar_len;
    assign m_arid    = rid;
    assign m_araddr  = ar_addr;
    assign m_arlen   = ar_len;
    assign m_arsize  = WBYTES_LG[2:0];
    assign m_arburst = 2'b01;

    wire [AW-1:0]   fl_addr = {fl_base[AW-1:12], fl_off, {WBYTES_LG{1'b0}}};
    assign c_fill_go  = {NH{miss_act}} & sel;
    assign c_fill_idx = idx_of(fl_addr);
    assign c_fill_tag = tag_of(fl_addr);
    assign m_rready   = c_fill_go & c_fill_ready;
    wire   fl_beat    = |(m_rvalid & m_rready);
    wire   fl_line    = |(c_fill_done & sel);
    wire   fl_last    = fl_beat && (fl_beats == 9'd1);
    reg [1:0] sel_resp; integer hk;
    always @(*) begin sel_resp = 2'b00; for (hk = 0; hk < NH; hk = hk + 1) if (sel[hk]) sel_resp = sel_resp | m_rresp[hk*2 +: 2]; end
    wire   miss_after = miss_act ? !fl_last : miss_now;
    wire   burst_done = (dr_next > rlen) && !miss_after;

    // ---------------------------------------------------------- state
    always @(posedge clk) begin
        if (!resetn) begin
            busy <= 1'b0; r_val <= 1'b0; m_arvalid <= 0; rarr_mask <= {P{1'b1}};
            sel <= 0; r_home <= 0; r_hidx <= 0; miss_act <= 1'b0; lk <= 0; dr <= 0; fl_lim <= 0;
        end else begin
            if (!busy) begin
                r_val <= 1'b0;
                if (rg_v) begin
                    ra   <= mr_qaddr[rg_p*AW +: AW];
                    rlen <= {1'b0, mr_qlen[rg_p*8 +: 8]};
                    rid  <= mr_qid[rg_p*IDW +: IDW];
                    r_id <= mr_qid[rg_p*IDW +: IDW];
                    r_seq <= mr_qseq[rg_p*SEQW +: SEQW];
                    sel <= 0; sel[rg_p / M] <= 1'b1;
                    r_home <= 0; r_home[rg_p / M] <= 1'b1;
                    r_hidx <= (rg_p / M);
                    rarr_mask <= ~((pick << 1) - 1'b1);
                    lk <= 0; dr <= 0; fl_lim <= NOMISS; miss_act <= 1'b0; r_resp <= 2'b00;
                    busy <= 1'b1;
                end
            end else begin
                // issuer
                if (restart)    lk <= need;
                else if (issue) lk <= lk + 9'd1;

                // presenter
                if (take) begin
                    r_val  <= 1'b1;
                    r_last <= (land_b == rlen);
                end else if (accept) begin
                    r_val  <= 1'b0;
                end
                if (accept) dr <= dr + 9'd1;

                // a miss on the beat the master needs: one DRAM read to the end
                if (miss_now) begin
                    ar_addr  <= {mb_addr[AW-1:LINE_LSB], {LINE_LSB{1'b0}}};
                    ar_len   <= {{(8-OFFW){1'b0}}, ar_beats};
                    fl_base  <= {ra[AW-1:12], 12'b0};
                    fl_off   <= mb_line;
                    fl_beats <= {{(9-OFFW){1'b0}}, ar_beats} + 9'd1;
                    // beats below the missing line's first sub-word stay look-up-able
                    fl_lim   <= $signed({2'b0, land_b}) - $signed({{(11-SUBW-1){1'b0}}, sub_of(mb_addr)});
                    miss_act <= 1'b1;
                    m_arvalid <= sel;
                end
                if (|m_arvalid) m_arvalid <= m_arvalid & ~m_arready;
                if (fl_beat) begin
                    fl_off   <= fl_off + 1'b1;
                    fl_beats <= fl_beats - 9'd1;
                    r_resp   <= r_resp | sel_resp;
                    if (fl_last) miss_act <= 1'b0;
                end
                if (fl_line) fl_lim <= fl_lim + K;

                if (burst_done) busy <= 1'b0;
            end
        end
    end
endmodule

`default_nettype wire

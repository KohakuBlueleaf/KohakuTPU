// The SRU pool: F_LANES sru_plane lanes running a pass's program of NOPS ops
// (FMA or table function, one latency) on every element of every word, on a
// static modulo schedule. scripts/py/sru/cfg.py (schedule) computes the op
// store's schedule fields; LDEP and WLEAD there are this file's timing.
//
// SLOTS. M = 16 / F_LANES chunks a word (lane l holds element l + F_LANES*e of
// chunk e); II = NOPS * M. Slot s = i*M + e of every II-cycle window issues op
// i on chunk e on all lanes. A window's first slot may take a word (want); op
// i works on the word taken KW_i windows earlier.
//
// HISTORY instead of a register file, per lane: the words' X (and B) in
// SRLs shifted once a window (depth in windows), the lane's results in H_TAPS
// SRLs shifted every cycle (depths D0, D1 in cycles: sigma_i - sigma_k - LDEP).
//
// PIPELINE from an op's slot cycle c0: c1 op fields, c2 the word's meta (row
// parity, word index -> column-constant address), c3 the history reads, c4
// operands into the lane; the lane's result is out in c20 and readable from a
// result SRL in c21, i.e. by an op whose c3 is >= c21: LDEP = 18. The last op's
// results assemble the out word; a word is out the cycle after its last chunk.

`default_nettype none

(* shreg_extract = "no" *)
module sru_pool #(
    parameter integer MODEL   = 0,
    parameter integer F_LANES = 16,
    parameter integer HAS_FN  = 1,
    parameter         TAB     = "ram",
    parameter integer HAS_B   = 1,
    parameter integer H_TAPS  = 2,     // result taps a lane: 1 | 2
    parameter integer HAS_MANT = 0,    // MANT / HALF function modes on the table
    parameter integer FIN_RCP = 0      // lane 0's mantissa ROM: the direct RCP / RSQRT
)(
    input  wire          clk,
    input  wire          rst,

    // the pass's program; restart only while nothing is in flight
    input  wire          restart,
    input  wire [3:0]    nops,
    input  wire [3:0]    opbase,
    input  wire          op_we,
    input  wire [5:0]    op_waddr,     // op * 4 + word
    input  wire [31:0]   op_wdata,
    input  wire          tab_we,
    input  wire [9:0]    tab_waddr,
    input  wire [35:0]   tab_wdata,

    // words in
    output wire          want,         // a window starts: a word may be taken now
    input  wire          take,
    input  wire [12:0]   take_meta,    // {wi[8:0], first, last, par, emit}
    input  wire [383:0]  x4,           // the taken word, unpacked, 4 cycles after take
    input  wire [383:0]  b4,
    input  wire [287:0]  wv0,          // row scalars by sub-row parity
    input  wire [287:0]  wv1,
    input  wire [8:0]    col0,
    output wire [8:0]    cc_raddr,
    input  wire [191:0]  cc_rdata,     // 2 cycles after cc_raddr

    // words out
    output reg           out_valid,
    output reg  [383:0]  out_data,
    output reg  [3:0]    out_meta,     // {first, last, par, emit}

    // one op straight into lane 0 (the finalise), only while no slot is valid:
    // an FMA, or (dir_fn) the mantissa ROM's RCP / RSQRT (dir_half) of dir_a
    input  wire          dir_valid,
    input  wire          dir_fn,
    input  wire          dir_half,
    input  wire [23:0]   dir_a,
    input  wire [23:0]   dir_b,
    input  wire [23:0]   dir_c,
    output wire          dir_ov,       // its result, 16 cycles later
    output wire [23:0]   dir_out
);
    `include "sru_cfg.vh"

    localparam integer F  = F_LANES;
    localparam integer M  = 16 / F_LANES;
    localparam integer MW = (M > 1) ? $clog2(M) : 1;
    localparam integer OUT_AT = 16;    // lane latency: c4 -> c20
    integer q;
    genvar  gl, ge;

    // ======================================================== slot clock
    reg  [3:0]    si;                  // op of the slot
    reg  [MW-1:0] se;                  // chunk of the slot
    reg  [4:0]    wcnt;                // windows started (meta SRL shifts)
    wire          lst_e = (se == M - 1);
    wire          lst_i = (si == nops - 4'd1);
    wire          ws    = (si == 4'd0) && (se == {MW{1'b0}});
    assign want = ws;
    always @(posedge clk) begin
        if (rst || restart) begin
            si <= 4'd0;
            se <= {MW{1'b0}};
        end else if (lst_e) begin
            se <= {MW{1'b0}};
            si <= lst_i ? 4'd0 : si + 4'd1;
        end else begin
            se <= se + 1'b1;
        end
        if (rst) begin
            wcnt <= 5'd0;
        end else if (ws) begin
            wcnt <= wcnt + 5'd1;
        end
    end
    wire [4:0] cur_w = ws ? wcnt : wcnt - 5'd1;

    // ======================================================== op store
    reg  [31:0] om0 [0:15];
    reg  [31:0] om1 [0:15];
    reg  [31:0] om2 [0:15];
    reg  [31:0] om3 [0:15];
    always @(posedge clk) begin
        if (op_we) begin
            case (op_waddr[1:0])
                2'd0: om0[op_waddr[5:2]] <= op_wdata;
                2'd1: om1[op_waddr[5:2]] <= op_wdata;
                2'd2: om2[op_waddr[5:2]] <= op_wdata;
                default: om3[op_waddr[5:2]] <= op_wdata;
            endcase
        end
    end
    wire [3:0]   opa  = opbase + si;
    wire [127:0] ow0  = {om3[opa], om2[opa], om1[opa], om0[opa]};

    // ---- c1: the op, the word it works on ----------------------------------
    // the c1..c4 lines are flops: a fixed-depth chain left to synthesis
    // becomes SRLs under sru_cu, and LUT outranks FF
    (* shreg_extract = "no" *) reg [127:0]  ow1;
    (* shreg_extract = "no" *) reg [4:0]    tw1;
    (* shreg_extract = "no" *) reg [MW-1:0] e1;
    (* shreg_extract = "no" *) reg          l1;
    always @(posedge clk) begin
        ow1 <= ow0;
        tw1 <= cur_w - `SRU_O_KW(ow0);
        e1  <= se;
        l1  <= lst_i;
    end

    // ---- meta, a window deep each: {wi, first, last, par, emit}; the valid
    // bits in flops with reset, so windows before reset read as empty
    (* srl_style = "srl" *) reg [12:0] msrl [0:31];
    reg [31:0] mvl;
    always @(posedge clk) begin
        if (ws) begin
            msrl[0] <= take_meta;
            for (q = 1; q < 32; q = q + 1) msrl[q] <= msrl[q-1];
        end
        if (rst) begin
            mvl <= 32'd0;
        end else if (ws) begin
            mvl <= {mvl[30:0], take};
        end
    end

    // ---- c2: the word's meta -----------------------------------------------
    (* shreg_extract = "no" *) reg [127:0]  ow2;
    (* shreg_extract = "no" *) reg [4:0]    tw2;
    (* shreg_extract = "no" *) reg [MW-1:0] e2;
    (* shreg_extract = "no" *) reg          l2;
    (* shreg_extract = "no" *) reg [13:0]   mt2;
    always @(posedge clk) begin
        ow2 <= ow1;
        tw2 <= tw1;
        e2  <= e1;
        l2  <= l1;
        // nothing is valid while held: the op store may not hold a program
        mt2 <= {~restart & mvl[wcnt - 5'd1 - tw1], msrl[wcnt - 5'd1 - tw1]};
    end
    assign cc_raddr = col0 + mt2[12:4];

    // ---- c3: the row scalars the op reads (per row, shared by the lanes) ----
    wire [287:0] rset2 = mt2[1] ? wv1 : wv0;
    (* shreg_extract = "no" *) reg  [127:0]  ow3;
    (* shreg_extract = "no" *) reg  [4:0]    tw3;
    (* shreg_extract = "no" *) reg  [MW-1:0] e3;
    (* shreg_extract = "no" *) reg           l3;
    (* shreg_extract = "no" *) reg  [13:0]   mt3;
    (* shreg_extract = "no" *) reg  [23:0]   rb3 [0:3];
    (* shreg_extract = "no" *) reg  [23:0]   rc3 [0:3];
    always @(posedge clk) begin
        ow3 <= ow2;
        tw3 <= tw2;
        e3  <= e2;
        l3  <= l2;
        mt3 <= mt2;
        for (q = 0; q < 4; q = q + 1) begin
            rb3[q] <= rset2[(3*q + `SRU_O_RB(ow2))*24 +: 24];
            rc3[q] <= rset2[(3*q + `SRU_O_RC(ow2))*24 +: 24];
        end
    end

    // X / B history: shifted 4 cycles after each window start, when the
    // window's word (or nothing) reaches x4
    reg  [3:0] wsd;
    reg  [4:0] xcnt;
    always @(posedge clk) begin
        if (rst) begin
            wsd  <= 4'd0;
            xcnt <= 5'd0;
        end else begin
            wsd <= {wsd[2:0], ws};
            if (wsd[3]) xcnt <= xcnt + 5'd1;
        end
    end
    wire       xsh  = wsd[3];
    wire [4:0] xa3  = xcnt - 5'd1 - tw3;
    wire [4:0] d0_3 = `SRU_O_D0(ow3);
    wire [4:0] d1_3 = `SRU_O_D1(ow3);

    // ---- c4: operands ------------------------------------------------------
    (* shreg_extract = "no" *) reg  [127:0]  ow4;
    (* shreg_extract = "no" *) reg  [MW-1:0] e4;
    (* shreg_extract = "no" *) reg           l4;
    (* shreg_extract = "no" *) reg  [13:0]   mt4;
    always @(posedge clk) begin
        ow4 <= ow3;
        e4  <= e3;
        l4  <= l3;
        if (rst) mt4 <= 14'd0; else mt4 <= mt3;
    end
    wire        k4    = (HAS_FN != 0) & `SRU_O_KIND(ow4);
    wire [1:0]  asel4 = `SRU_O_A(ow4);
    wire [2:0]  bsel4 = `SRU_O_B(ow4);
    wire [2:0]  csel4 = `SRU_O_C(ow4);

    wire [23:0] xr4 [0:F-1];
    wire [23:0] lo  [0:F-1];
    wire [9:0]  ta  [0:F-1];
    wire [35:0] tq  [0:F-1];

    generate
    for (gl = 0; gl < F; gl = gl + 1) begin : g_lane
        // history of this lane's elements, one SRL line a chunk
        wire [23:0] xq [0:M-1];
        wire [23:0] bq [0:M-1];
        for (ge = 0; ge < M; ge = ge + 1) begin : g_chunk
            (* srl_style = "srl" *) reg [23:0] xs [0:31];
            always @(posedge clk) begin
                if (xsh) begin
                    xs[0] <= x4[(gl + F*ge)*24 +: 24];
                    for (q = 1; q < 32; q = q + 1) xs[q] <= xs[q-1];
                end
            end
            assign xq[ge] = xs[xa3];
            if (HAS_B != 0) begin : g_b
                (* srl_style = "srl" *) reg [23:0] bs [0:31];
                always @(posedge clk) begin
                    if (xsh) begin
                        bs[0] <= b4[(gl + F*ge)*24 +: 24];
                        for (q = 1; q < 32; q = q + 1) bs[q] <= bs[q-1];
                    end
                end
                assign bq[ge] = bs[xa3];
            end else begin : g_nob
                assign bq[ge] = 24'd0;
            end
        end

        // this lane's results, H_TAPS read taps (a copy of the line each)
        (* srl_style = "srl" *) reg [23:0] h0 [0:31];
        (* shreg_extract = "no" *) reg [23:0] xr, br, h0r, h1r, rbr, rcr;
        always @(posedge clk) begin
            h0[0] <= lo[gl];
            for (q = 1; q < 32; q = q + 1) h0[q] <= h0[q-1];
            // c3 -> c4
            xr  <= xq[e3];
            br  <= bq[e3];
            h0r <= h0[d0_3];
            rbr <= rb3[(gl + F*e3) / 4];
            rcr <= rc3[(gl + F*e3) / 4];
        end
        if (H_TAPS > 1) begin : g_h1
            (* srl_style = "srl" *) reg [23:0] h1 [0:31];
            always @(posedge clk) begin
                h1[0] <= lo[gl];
                for (q = 1; q < 32; q = q + 1) h1[q] <= h1[q-1];
                h1r <= h1[d1_3];
            end
        end else begin : g_noh1
            always @(*) h1r = h0r;
        end
        assign xr4[gl] = xr;

        // c4: operands; the lane's column is fixed (F_LANES >= 4)
        wire [23:0] cb = `SRU_O_CB(ow4) ? cc_rdata[(2*(gl%4)+1)*24 +: 24]
                                       : cc_rdata[(2*(gl%4)+0)*24 +: 24];
        wire [23:0] cc = `SRU_O_CC(ow4) ? cc_rdata[(2*(gl%4)+1)*24 +: 24]
                                       : cc_rdata[(2*(gl%4)+0)*24 +: 24];
        wire [23:0] ha = `SRU_O_HA(ow4) ? h1r : h0r;
        wire [23:0] hb = `SRU_O_HB(ow4) ? h1r : h0r;
        wire [23:0] hc = `SRU_O_HC(ow4) ? h1r : h0r;
        reg  [23:0] av, bv, cv;
        always @(*) begin
            case (asel4)
                2'd0:    av = xr;
                2'd1:    av = br;
                2'd2:    av = xr4[gl ^ 1];
                default: av = ha;
            endcase
            case (bsel4)
                3'd0:    bv = `SRU_O_KB(ow4);
                3'd1:    bv = rbr;
                3'd2:    bv = cb;
                3'd3:    bv = br;
                3'd4:    bv = xr;
                default: bv = hb;
            endcase
            case (csel4)
                3'd0:    cv = `SRU_O_KC(ow4);
                3'd1:    cv = rcr;
                3'd2:    cv = cc;
                3'd3:    cv = br;
                3'd4:    cv = xr;
                default: cv = hc;
            endcase
        end

        wire        dv  = (gl == 0) && dir_valid;
        wire [23:0] la  = dv ? dir_a : {av[23] ^ `SRU_O_NA(ow4), av[22:0]};
        wire [23:0] lb  = dv ? dir_b : bv;
        wire [23:0] lc  = dv ? dir_c : {cv[23] ^ `SRU_O_NC(ow4), cv[22:0]};
        sru_plane #(.MODEL(MODEL), .HAS_FN(HAS_FN), .TAB(TAB), .HAS_MANT(HAS_MANT),
                    .MROM((gl == 0) ? FIN_RCP : 0)) u_lane (
            .clk(clk), .rst(rst), .in_valid(mt4[13] | dv),
            .op(dv ? ((FIN_RCP != 0) & dir_fn) : k4),
            .a(la), .b(lb), .c(lc),
            .fm(dv ? {1'b1, dir_half} : {`SRU_O_MANT(ow4), `SRU_O_FIX(ow4)}),
            .neg(dv | `SRU_O_NEG(ow4)), .mrom(dv),
            .pre(`SRU_O_PRE(ow4)), .cs(`SRU_O_CS(ow4)),
            .tab_addr(ta[gl]), .tab_q(tq[gl]),
            .out_valid(), .out(lo[gl])
        );
    end

    // the function table: a true-dual-port RAMB36 a lane pair, port A also
    // the write port (tables load while the pool is idle)
    if ((HAS_FN != 0) && (TAB != "rom")) begin : g_tab
        for (gl = 0; gl < F / 2; gl = gl + 1) begin : g_pair
            xpm_memory_tdpram #(
                .ADDR_WIDTH_A(10), .ADDR_WIDTH_B(10),
                .WRITE_DATA_WIDTH_A(36), .WRITE_DATA_WIDTH_B(36),
                .READ_DATA_WIDTH_A(36), .READ_DATA_WIDTH_B(36),
                .BYTE_WRITE_WIDTH_A(36), .BYTE_WRITE_WIDTH_B(36),
                .MEMORY_SIZE(1024 * 36),
                .MEMORY_PRIMITIVE("block"),
                .CLOCKING_MODE("common_clock"),
                .READ_LATENCY_A(2), .READ_LATENCY_B(2),
                .WRITE_MODE_A("read_first"), .WRITE_MODE_B("read_first"),
                .MEMORY_INIT_FILE("none"), .USE_MEM_INIT(0),
                .ECC_MODE("no_ecc"), .AUTO_SLEEP_TIME(0),
                .SIM_ASSERT_CHK(0), .WAKEUP_TIME("disable_sleep")
            ) u_tab (
                .clka(clk), .rsta(1'b0), .ena(1'b1), .regcea(1'b1),
                .wea(tab_we), .addra(tab_we ? tab_waddr : ta[2*gl]),
                .dina(tab_wdata), .douta(tq[2*gl]),
                .clkb(clk), .rstb(1'b0), .enb(1'b1), .regceb(1'b1),
                .web(1'b0), .addrb(ta[2*gl+1]),
                .dinb(36'd0), .doutb(tq[2*gl+1]),
                .injectsbiterra(1'b0), .injectdbiterra(1'b0),
                .injectsbiterrb(1'b0), .injectdbiterrb(1'b0),
                .sbiterra(), .dbiterra(), .sbiterrb(), .dbiterrb(),
                .sleep(1'b0)
            );
        end
    end else begin : g_notab
        for (gl = 0; gl < F; gl = gl + 1) begin : g_tie
            assign tq[gl] = 36'd0;
        end
    end
    endgenerate

    // the direct issue's result: lane 0, OUT_AT cycles on
    reg [OUT_AT-1:0] dvl;
    always @(posedge clk) begin
        if (rst) dvl <= {OUT_AT{1'b0}}; else dvl <= {dvl[OUT_AT-2:0], dir_valid};
    end
    assign dir_ov  = dvl[OUT_AT-1];
    assign dir_out = lo[0];

    // ======================================================== out words
    // c4 -> c20: is the slot the program's last op, its chunk, the word's meta
    (* shreg_extract = "no" *) reg [MW+4:0] od [5:OUT_AT+4];
    reg [OUT_AT-1:0] ovl;
    always @(posedge clk) begin
        od[5] <= {l4, e4, mt4[3:0]};
        for (q = 6; q <= OUT_AT + 4; q = q + 1) od[q] <= od[q-1];
        if (rst) ovl <= {OUT_AT{1'b0}}; else ovl <= {ovl[OUT_AT-2:0], mt4[13]};
    end
    wire [MW+4:0] o20  = od[OUT_AT + 4];
    wire          ol20 = o20[MW+4];
    wire [MW-1:0] oe20 = o20[MW+3:4];
    wire          ov20 = ovl[OUT_AT-1];

    always @(posedge clk) begin
        if (ol20) begin
            for (q = 0; q < F; q = q + 1) begin
                out_data[(q + F*oe20)*24 +: 24] <= lo[q];
            end
        end
        if (rst) begin
            out_valid <= 1'b0;
        end else begin
            out_valid <= ol20 && (oe20 == M - 1) && ov20;
        end
        out_meta <= o20[3:0];
    end

endmodule

`default_nettype wire

// One home's cache array: the sdpram, the hit compare, and the line fill straight
// off its own DRAM R channel. Wide data never enters an engine.
//
// ONE ROW IS ONE WORD: {valid, tag, W-bit word}. The K sub-words of a line sit at
// K CONSECUTIVE ADDRESSES, not side by side in one row, so a lookup ADDRESSES the
// sub-word it wants and the fabric never muxes K words: the BANKS read select
// narrows from K*W + meta to W + meta, and the K:1 sub-word select is gone.
// Each row carries its own tag, so presence is per word: a master write
// invalidates only the word it wrote, and a fill's landed words serve while the
// rest are still in flight.
//
// Lookups PIPELINE: one per cycle, tag carried beside the RAM's own latency so
// the compare meets its own row; a lookup lands RD_LAT cycles after rd_en
// (`land`), hit_c is that landing's compare, and hit/word are captured only when
// the engine says `rd_take`. The RAM's enable is tied high so the pipeline
// advances every cycle whether or not a lookup was issued. Fills take a per-beat
// address from the engine and yield to the write port (`fill_ready`), so a master
// write is never dropped under a fill.
//
// BANKS splits the rows over BANKS arrays on the low address bits, so a line's
// sub-words rotate over banks; the select rides the lookup pipeline and a
// registered BANKS:1 row mux costs the one extra cycle of RD_LAT. Banking is
// what shortens the URAM cascade, the one axis the device constrains: UG573
// p.116 cascades bottom-up in ONE column, UG901 p.117 caps a chain at 8, UG901
// p.118 wants rows + columns output registers (ARR_LAT).
//
// WR_ALLOC: a full-word master write installs the word (the fill/master data 2:1
// over W is its whole cost); otherwise a master write is an invalidate whose data
// never reaches the port.

`default_nettype none

module kx_carray #(
    parameter integer AW        = 40,
    parameter integer W         = 512,
    parameter integer SETS      = 32768,
    parameter integer SET_W     = 15,
    parameter integer K         = 1,
    parameter         RAM_STYLE = "ultra",
    parameter integer BANKS     = 8,
    parameter integer WPORT_REG = 0,            // 1: the write-port bundle registered
    parameter integer FILL_SERVE = 1,           // 1: a completed fill also loads `word` (the one-line engine)
    parameter integer ARR_LAT   = 0,            // primitive read latency; 0 = URAM 4 / else 1
    parameter integer SPAN_LG   = 12,           // a fill never crosses this: the AXI 4 KB page
    parameter integer WBYTES_LG = $clog2(W/8),
    parameter integer SUBW      = (K <= 1) ? 0 : $clog2(K),
    parameter integer LINE_LSB  = WBYTES_LG + SUBW,
    parameter integer CW        = K * W,
    parameter integer TAG_W     = AW - LINE_LSB - SET_W,
    parameter integer BCW       = (K <= 1) ? 1 : $clog2(K),
    parameter integer WR_ALLOC  = (K <= 1) ? 1 : 0
)(
    input  wire                 clk,
    input  wire                 resetn,

    input  wire                 rd_en,
    input  wire [SET_W-1:0]     rd_idx,
    input  wire [TAG_W-1:0]     rd_tag,
    input  wire [SUBW:0]        rd_sub,
    input  wire                 rd_take,
    output wire                 land,
    output wire                 hit_c,
    output wire                 hit,
    output wire [W-1:0]         word,

    input  wire                 fill_go,
    input  wire [W-1:0]         r_data,
    input  wire                 r_valid,
    input  wire                 r_last,
    input  wire [SET_W-1:0]     fill_idx,
    input  wire [TAG_W-1:0]     fill_tag,
    output wire                 fill_ready,
    output wire                 fill_done,

    input  wire                 wr_en,
    input  wire [SET_W-1:0]     wr_idx,
    input  wire [BCW-1:0]       wr_sub,
    input  wire [TAG_W-1:0]     wr_tag,
    input  wire [W-1:0]         wr_word,
    input  wire                 wr_full,

    output wire                 flush_busy
);
    localparam integer RAM_LAT = (ARR_LAT != 0) ? ARR_LAT : ((RAM_STYLE == "ultra") ? 4 : 1);
    localparam integer RD_LAT  = RAM_LAT + ((BANKS > 1) ? 1 : 0);
    localparam integer BLG     = (BANKS <= 1) ? 1 : $clog2(BANKS);
    localparam integer RA_W    = SET_W + SUBW;      // one address per sub-word
    localparam integer ROWS    = SETS * K;
    localparam integer ROW_L   = W + TAG_W + 1;     // {valid, tag, word}

    reg flushing; reg [RA_W-1:0] flush_a;
    always @(posedge clk) begin
        if (!resetn) begin flushing <= 1'b1; flush_a <= 0; end
        else if (flushing) begin
            flush_a <= flush_a + 1'b1;
            if (flush_a == (ROWS-1)) begin
                flushing <= 1'b0;
            end
        end
    end

    // lookup pipeline: valid, tag and sub-word ride beside the RAM's read latency
    reg [RD_LAT-1:0] rd_pipe;
    reg [TAG_W-1:0]  tag_p [0:RD_LAT-1];
    reg [SUBW:0]     sub_p [0:RD_LAT-1];
    integer s;
    generate if (RD_LAT == 1) begin : g_rp1
        always @(posedge clk) begin
            rd_pipe <= rd_en;
        end
    end else begin : g_rpn
        always @(posedge clk) begin
            rd_pipe <= {rd_pipe[RD_LAT-2:0], rd_en};
        end
    end endgenerate
    always @(posedge clk) begin
        tag_p[0] <= rd_tag; sub_p[0] <= rd_sub;
        for (s = 1; s < RD_LAT; s = s + 1) begin tag_p[s] <= tag_p[s-1]; sub_p[s] <= sub_p[s-1]; end
    end
    wire [TAG_W-1:0] q_tag = tag_p[RD_LAT-1];
    wire [SUBW:0]    q_sub = sub_p[RD_LAT-1];

    wire [ROW_L-1:0] rd_row;
    wire hit_now = rd_row[ROW_L-1] && (rd_row[W +: TAG_W] == q_tag);
    assign land  = rd_pipe[RD_LAT-1];
    assign hit_c = hit_now;

    // write-side inputs registered a cycle before the port, so the fabric's M:1
    // select and the strobe gating never sit in one cone with the port's 2:1
    reg              w_en_q;
    reg [SET_W-1:0]  w_idx_q;
    reg [BCW-1:0]    w_sub_q;
    reg [TAG_W-1:0]  w_tag_q;
    reg [W-1:0]      w_word_q;
    reg              w_full_q;
    always @(posedge clk) begin
        w_en_q <= wr_en && resetn;
        if (wr_en) begin
            w_idx_q <= wr_idx; w_sub_q <= wr_sub; w_tag_q <= wr_tag;
            w_word_q <= wr_word; w_full_q <= wr_full;
        end
    end

    // fill: every beat is a write of one whole row -- its own word, its own tag,
    // valid unless the fill is poisoned. A beat is taken only when the write port
    // is free.
    assign fill_ready = !flushing && !w_en_q;
    wire beat = fill_go && r_valid && fill_ready;
    wire          line_end;
    wire [K-1:0]  sub_oh;                   // one-hot of the beat's sub-word
    wire [BCW-1:0] bc_v;
    generate if (K <= 1) begin : g_k1
        assign line_end = 1'b1;
        assign sub_oh   = 1'b1;
        assign bc_v     = {BCW{1'b0}};
    end else begin : g_kn
        reg [BCW-1:0] bc;
        assign line_end = (bc == K-1);
        assign bc_v     = bc;
        genvar j;
        for (j = 0; j < K; j = j + 1) begin : g_oh
            assign sub_oh[j] = (bc == j[BCW-1:0]);
        end
        always @(posedge clk) begin
            if (!resetn || !fill_go) begin
                bc <= 0;
            end
            else if (beat) begin
                bc <= line_end ? {BCW{1'b0}} : bc + 1'b1;
            end
        end
    end endgenerate
    assign fill_done = beat && line_end;

    // the served word: captured on a taken landing (and, for the one-line engine,
    // on the fill it waited for -- the beat carrying q_sub is held until the last)
    wire [W-1:0]  row_word  = rd_row[W-1:0];
    wire [W-1:0]  fill_word;
    generate if ((FILL_SERVE != 0) && (K > 1)) begin : g_fs_k
        wire        fill_mine = beat && sub_oh[q_sub[BCW-1:0]];
        reg [W-1:0] fw_q;
        always @(posedge clk) begin
            if (fill_mine) begin
                fw_q <= r_data;
            end
        end
        assign fill_word = fill_mine ? r_data : fw_q;
    end else begin : g_fs_1
        assign fill_word = r_data;
    end endgenerate
    reg [W-1:0] word_q;
    reg         hit_q;
    always @(posedge clk) begin
        if (land && rd_take) begin
            hit_q <= hit_now;
        end
        if ((FILL_SERVE != 0) && fill_done) begin
            word_q <= fill_word;
        end
        else if (land && rd_take) begin
            word_q <= row_word;
        end
    end
    assign hit  = hit_q;
    assign word = word_q;

    // FILL POISON. fill_go spans the engine's AR to its last beat, so a master
    // write that lands in that window into a line the fill has still to land may
    // post-date the fill's DRAM read: the copy in flight is stale. Every row this
    // fill lands after such a write lands with valid 0 and is fetched again; rows
    // it landed BEFORE the write are the write's own to invalidate. The span is
    // the fill's remaining lines: ascending, inside one page.
    localparam integer PL = SPAN_LG - LINE_LSB;             // log2 lines per page
    wire w_in_span;
    generate if (PL <= 0) begin : g_sp0
        assign w_in_span = (w_idx_q == fill_idx);
    end else if (PL >= SET_W) begin : g_spall
        assign w_in_span = (w_idx_q >= fill_idx);
    end else begin : g_span
        assign w_in_span = (w_idx_q[SET_W-1:PL] == fill_idx[SET_W-1:PL])
                           && (w_idx_q[PL-1:0] >= fill_idx[PL-1:0]);
    end endgenerate
    reg poison;
    always @(posedge clk) begin
        if (!resetn || !fill_go) begin
            poison <= 1'b0;
        end
        else if (w_en_q && w_in_span) begin
            poison <= 1'b1;
        end
    end

    // one write port: flush > master write > fill (a fill yields via fill_ready).
    // A master write installs its word under WR_ALLOC, else marks the row invalid
    // -- and an invalid row's data is don't-care, so the port's data needs no
    // select at all.
    wire [RA_W-1:0] fill_a, wr_a, rd_a;
    generate if (SUBW == 0) begin : g_a1
        assign fill_a = fill_idx;
        assign wr_a   = w_idx_q;
        assign rd_a   = rd_idx;
    end else begin : g_an
        assign fill_a = {fill_idx, bc_v[SUBW-1:0]};
        assign wr_a   = {w_idx_q, w_sub_q[SUBW-1:0]};
        assign rd_a   = {rd_idx, rd_sub[SUBW-1:0]};
    end endgenerate

    wire             ram_we_c    = flushing || w_en_q || beat;
    wire             use_fill    = beat;
    wire [RA_W-1:0]  ram_waddr_c = flushing ? flush_a : use_fill ? fill_a : wr_a;
    wire             ram_valid_c = !flushing && ((use_fill && !poison)
                                                 || (!use_fill && (WR_ALLOC != 0) && w_full_q));
    wire [TAG_W-1:0] ram_tag_c   = use_fill ? fill_tag : w_tag_q;
    wire [W-1:0]     ram_wsrc    = (WR_ALLOC != 0) ? (use_fill ? r_data : w_word_q) : r_data;

    wire             ram_we;
    wire [RA_W-1:0]  ram_waddr;
    wire             ram_valid;
    wire [TAG_W-1:0] ram_tag;
    wire [W-1:0]     ram_wd;

    // WPORT_REG moves the whole bundle -- enable included, or a delayed enable
    // writes the wrong row -- one cycle later, so the fabric select is no longer
    // in one cone with the port itself.
    generate if (WPORT_REG == 0) begin : g_wp_comb
        assign ram_we    = ram_we_c;
        assign ram_waddr = ram_waddr_c;
        assign ram_valid = ram_valid_c;
        assign ram_tag   = ram_tag_c;
        assign ram_wd    = ram_wsrc;
    end else begin : g_wp_reg
        reg              we_q;
        reg [RA_W-1:0]   wa_q;
        reg              wv_q;
        reg [TAG_W-1:0]  wt_q;
        reg [W-1:0]      wd_q;
        always @(posedge clk) begin
            we_q <= ram_we_c && resetn;
            if (ram_we_c) begin
                wa_q <= ram_waddr_c;
                wv_q <= ram_valid_c;
                wt_q <= ram_tag_c;
                wd_q <= ram_wsrc;
            end
        end
        assign ram_we    = we_q;
        assign ram_waddr = wa_q;
        assign ram_valid = wv_q;
        assign ram_tag   = wt_q;
        assign ram_wd    = wd_q;
    end endgenerate

    wire [ROW_L-1:0] ram_row = {ram_valid, ram_tag, ram_wd};

    generate if (BANKS <= 1) begin : g_one
        kohaku_sdpram #(.WIDTH(ROW_L), .DEPTH(ROWS), .MEM_PRIM(RAM_STYLE), .READ_LAT(RD_LAT)) u_arr (
            .clk(clk), .wr_en(ram_we), .wr_addr(ram_waddr), .wr_data(ram_row),
            .rd_en(1'b1), .rd_addr(rd_a), .rd_data(rd_row)
        );
    end else begin : g_bank
        wire [ROW_L-1:0] bank_row [0:BANKS-1];
        reg  [BLG-1:0]   bsel_p [0:RAM_LAT-1];
        integer bs;
        always @(posedge clk) begin
            bsel_p[0] <= rd_a[BLG-1:0];
            for (bs = 1; bs < RAM_LAT; bs = bs + 1) begin
                bsel_p[bs] <= bsel_p[bs-1];
            end
        end

        // ONE LEVEL IS THE FLOOR. At BANKS = 8 this is 2 LUT + 1 MUXF7 per bit
        // and the F7 is free silicon, so every split is worse: two 4:1 then a
        // 2:1 is 2.5 LUT/bit, four 2:1 then a 4:1 is 3. Built as a knob and
        // withdrawn -- it also moved the array's read latency, which the
        // engines take from `land`, and the bench stopped making progress.
        reg [ROW_L-1:0] row_q;
        always @(posedge clk) begin
            row_q <= bank_row[bsel_p[RAM_LAT-1]];
        end
        assign rd_row = row_q;
        genvar b;
        for (b = 0; b < BANKS; b = b + 1) begin : g_b
            kohaku_sdpram #(.WIDTH(ROW_L), .DEPTH(ROWS/BANKS), .MEM_PRIM(RAM_STYLE), .READ_LAT(RAM_LAT)) u_arr (
                .clk(clk), .wr_en(ram_we && (ram_waddr[BLG-1:0] == b[BLG-1:0])),
                .wr_addr(ram_waddr[RA_W-1:BLG]), .wr_data(ram_row),
                .rd_en(1'b1), .rd_addr(rd_a[RA_W-1:BLG]), .rd_data(bank_row[b])
            );
        end
    end endgenerate

    assign flush_busy = flushing;
endmodule

`default_nettype wire

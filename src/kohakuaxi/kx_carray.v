// One home's cache array: the sdpram, the hit compare, the k x IO sub-word select,
// and the line fill straight off its own DRAM R channel. Wide data never enters an
// engine. The write port's data is a single 2:1 (fill line vs write word); flush
// writes valid=0 only, so it needs no data term. K=1 has no line buffer at all.

`default_nettype none

module kx_carray #(
    parameter integer AW        = 40,
    parameter integer W         = 512,
    parameter integer SETS      = 32768,
    parameter integer SET_W     = 15,
    parameter integer K         = 1,
    parameter         RAM_STYLE = "ultra",
    parameter integer WBYTES_LG = $clog2(W/8),
    parameter integer SUBW      = (K <= 1) ? 0 : $clog2(K),
    parameter integer LINE_LSB  = WBYTES_LG + SUBW,
    parameter integer CW        = K * W,
    parameter integer TAG_W     = AW - LINE_LSB - SET_W,
    parameter integer ROW_W     = 1 + TAG_W + CW,
    parameter integer BCW       = (K <= 1) ? 1 : $clog2(K)
)(
    input  wire                 clk,
    input  wire                 resetn,

    input  wire                 rd_en,
    input  wire [SET_W-1:0]     rd_idx,
    input  wire [TAG_W-1:0]     rd_tag,
    input  wire [SUBW:0]        rd_sub,
    output wire                 hit,
    output wire [W-1:0]         word,

    input  wire                 fill_go,
    input  wire [W-1:0]         r_data,
    input  wire                 r_valid,
    input  wire                 r_last,
    output wire                 fill_done,

    input  wire                 wr_en,
    input  wire [SET_W-1:0]     wr_idx,
    input  wire [TAG_W-1:0]     wr_tag,
    input  wire [W-1:0]         wr_word,
    input  wire                 wr_full,

    output wire                 flush_busy
);
    localparam integer RD_LAT = (RAM_STYLE == "ultra") ? 4 : 1;

    reg flushing; reg [SET_W-1:0] flush_idx;
    always @(posedge clk) begin
        if (!resetn) begin flushing <= 1'b1; flush_idx <= 0; end
        else if (flushing) begin
            flush_idx <= flush_idx + 1'b1;
            if (flush_idx == (SETS-1)) flushing <= 1'b0;
        end
    end

    reg [SET_W-1:0] q_idx; reg [TAG_W-1:0] q_tag; reg [SUBW:0] q_sub;
    always @(posedge clk) if (rd_en) begin q_idx <= rd_idx; q_tag <= rd_tag; q_sub <= rd_sub; end

    wire [ROW_W-1:0] rd_row;
    wire hit_now = rd_row[ROW_W-1] && (rd_row[CW +: TAG_W] == q_tag);
    reg  hit_q;
    assign hit = hit_q;

    wire beat = fill_go && r_valid;
    assign fill_done = beat && r_last;

    // K=1: the line IS the beat. K>1: buffer K-1 beats, last beat completes the line.
    wire [CW-1:0] fill_line;
    generate if (K <= 1) begin : g_k1
        assign fill_line = r_data;
    end else begin : g_kn
        reg [CW-1:0]  line_buf;
        reg [BCW-1:0] bc;
        integer fk;
        genvar j;
        for (j = 0; j < K; j = j + 1) begin : g_fl
            assign fill_line[j*W +: W] = (bc == j[BCW-1:0]) ? r_data : line_buf[j*W +: W];
        end
        always @(posedge clk) begin
            if (!resetn || rd_en) bc <= 0;
            else if (beat) begin
                for (fk = 0; fk < K; fk = fk + 1)
                    if (bc == fk[BCW-1:0]) line_buf[fk*W +: W] <= r_data;
                bc <= bc + 1'b1;
            end
        end
    end endgenerate

    // the served word is REGISTERED (FF is free; a held mux is not): captured on
    // the row-valid sample and again on the fill, so it holds through the drain.
    // Sub-word select is an INDEXED read on the binary q_sub (packs 1 LUT/bit);
    // K=1 makes it a plain wire. The 2:1 fill/row select is the only logic.
    // Two enables, two sources, NO data mux: the row-valid sample and the fill
    // never coincide (a fill follows a miss), so word_q takes rd_row on one and
    // fill_line on the other -- a clock-enable each, not a 2:1 per bit (which
    // measured 1,533 LUT as `word_q_i_1`).
    wire [W-1:0]  row_word  = (K <= 1) ? rd_row[W-1:0]   : rd_row[q_sub*W +: W];
    wire [W-1:0]  fill_word = (K <= 1) ? fill_line[W-1:0] : fill_line[q_sub*W +: W];
    reg [W-1:0] word_q;
    reg [RD_LAT:0] rd_pipe;
    always @(posedge clk) begin
        rd_pipe <= {rd_pipe[RD_LAT-1:0], rd_en};
        if (rd_pipe[RD_LAT]) hit_q <= hit_now;
    end
    always @(posedge clk) begin
        if (fill_done)            word_q <= fill_word;
        else if (rd_pipe[RD_LAT]) word_q <= row_word;
    end
    assign word = word_q;

    // The write-side inputs are REGISTERED first (FF is free): unregistered, the
    // fabric's 4-way word select, this 2:1 and the valid/strobe gating merged into
    // one 6-level cone per URAM data bit (~5 LUT/bit, measured 13.7k total).
    reg              w_en_q;
    reg [SET_W-1:0]  w_idx_q;
    reg [TAG_W-1:0]  w_tag_q;
    reg [W-1:0]      w_word_q;
    reg              w_full_q;
    always @(posedge clk) begin
        w_en_q <= wr_en && resetn;
        if (wr_en) begin w_idx_q <= wr_idx; w_tag_q <= wr_tag; w_word_q <= wr_word; w_full_q <= wr_full; end
    end

    // one write port: flush > (K>1 write-invalidate) > fill > (K=1 allocate).
    // data is fill ? line : word (one 2:1); flush/invalidate only clear valid.
    wire             inval_first = (K > 1) && w_en_q;
    wire             ram_we    = flushing || w_en_q || fill_done;
    wire             use_fill  = fill_done && !flushing && !inval_first;
    wire [SET_W-1:0] ram_waddr = flushing ? flush_idx : use_fill ? q_idx : w_idx_q;
    wire             ram_valid = !flushing && (use_fill || ((K == 1) && w_full_q));
    wire [TAG_W-1:0] ram_tag   = use_fill ? q_tag : w_tag_q;
    wire [CW-1:0]    ram_data  = use_fill ? fill_line : {K{w_word_q}};

    kohaku_sdpram #(.WIDTH(ROW_W), .DEPTH(SETS), .MEM_PRIM(RAM_STYLE), .READ_LAT(RD_LAT)) u_arr (
        .clk(clk), .wr_en(ram_we), .wr_addr(ram_waddr), .wr_data({ram_valid, ram_tag, ram_data}),
        .rd_en(rd_en), .rd_addr(rd_idx), .rd_data(rd_row)
    );

    assign flush_busy = flushing;
endmodule

`default_nettype wire

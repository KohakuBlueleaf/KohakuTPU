// One home's cache array: the sdpram, the hit compare, the k x IO sub-word select,
// and the line fill straight off its own DRAM R channel. Wide data never enters an
// engine. The write port's data is a single 2:1 (fill line vs write word); flush
// writes valid=0 only, so it needs no data term. K=1 has no line buffer at all.
//
// Lookups PIPELINE: one per cycle, tag/sub carried beside the RAM's own latency
// so the compare meets its own row; a lookup lands RD_LAT cycles after rd_en
// (`land`), hit_c is that landing's compare, and hit/word are captured only when
// the engine says `rd_take`. The RAM's enable is tied high so the pipeline
// advances every cycle whether or not a lookup was issued. Fills take a per-beat
// line address from the engine and yield to the write port (`fill_ready`), so a
// master write is never dropped under a fill.

`default_nettype none

module kx_carray #(
    parameter integer AW        = 40,
    parameter integer W         = 512,
    parameter integer SETS      = 32768,
    parameter integer SET_W     = 15,
    parameter integer K         = 1,
    parameter         RAM_STYLE = "ultra",
    parameter integer FILL_SERVE = 1,           // 1: a completed fill also loads `word` (the one-line engine)
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

    // lookup pipeline: valid, tag and sub-word ride beside the RAM's read latency
    reg [RD_LAT-1:0] rd_pipe;
    reg [TAG_W-1:0]  tag_p [0:RD_LAT-1];
    reg [SUBW:0]     sub_p [0:RD_LAT-1];
    integer s;
    generate if (RD_LAT == 1) begin : g_rp1
        always @(posedge clk) rd_pipe <= rd_en;
    end else begin : g_rpn
        always @(posedge clk) rd_pipe <= {rd_pipe[RD_LAT-2:0], rd_en};
    end endgenerate
    always @(posedge clk) begin
        tag_p[0] <= rd_tag; sub_p[0] <= rd_sub;
        for (s = 1; s < RD_LAT; s = s + 1) begin tag_p[s] <= tag_p[s-1]; sub_p[s] <= sub_p[s-1]; end
    end
    wire [TAG_W-1:0] q_tag = tag_p[RD_LAT-1];
    wire [SUBW:0]    q_sub = sub_p[RD_LAT-1];

    wire [ROW_W-1:0] rd_row;
    wire hit_now = rd_row[ROW_W-1] && (rd_row[CW +: TAG_W] == q_tag);
    assign land  = rd_pipe[RD_LAT-1];
    assign hit_c = hit_now;

    // write-side inputs registered a cycle before the port, so the fabric's M:1
    // select and the strobe gating never sit in one cone with the port's 2:1
    reg              w_en_q;
    reg [SET_W-1:0]  w_idx_q;
    reg [TAG_W-1:0]  w_tag_q;
    reg [W-1:0]      w_word_q;
    reg              w_full_q;
    always @(posedge clk) begin
        w_en_q <= wr_en && resetn;
        if (wr_en) begin w_idx_q <= wr_idx; w_tag_q <= wr_tag; w_word_q <= wr_word; w_full_q <= wr_full; end
    end

    // fill: K=1 writes every beat; K>1 buffers K-1 beats and writes on the last.
    // A beat is taken only when the write port is free this cycle.
    assign fill_ready = !flushing && !w_en_q;
    wire beat = fill_go && r_valid && fill_ready;
    wire [CW-1:0] fill_line;
    wire          line_end;
    generate if (K <= 1) begin : g_k1
        assign fill_line = r_data;
        assign line_end  = 1'b1;
    end else begin : g_kn
        reg [CW-1:0]  line_buf;
        reg [BCW-1:0] bc;
        integer fk;
        genvar j;
        for (j = 0; j < K; j = j + 1) begin : g_fl
            assign fill_line[j*W +: W] = (bc == j[BCW-1:0]) ? r_data : line_buf[j*W +: W];
        end
        assign line_end = (bc == K-1);
        always @(posedge clk) begin
            if (!resetn || !fill_go) bc <= 0;
            else if (beat) begin
                for (fk = 0; fk < K; fk = fk + 1)
                    if (bc == fk[BCW-1:0]) line_buf[fk*W +: W] <= r_data;
                bc <= line_end ? {BCW{1'b0}} : bc + 1'b1;
            end
        end
    end endgenerate
    assign fill_done = beat && line_end;

    // the served word: captured on a taken landing (and, for the one-line engine,
    // on the fill it waited for). Sub-word select on the binary q_sub; K=1 a wire.
    wire [W-1:0]  row_word  = (K <= 1) ? rd_row[W-1:0]   : rd_row[q_sub*W +: W];
    wire [W-1:0]  fill_word = (K <= 1) ? fill_line[W-1:0] : fill_line[q_sub*W +: W];
    reg [W-1:0] word_q;
    reg         hit_q;
    always @(posedge clk) begin
        if (land && rd_take) hit_q <= hit_now;
        if ((FILL_SERVE != 0) && fill_done) word_q <= fill_word;
        else if (land && rd_take)          word_q <= row_word;
    end
    assign hit  = hit_q;
    assign word = word_q;

    // one write port: flush > master write > fill (a fill yields via fill_ready).
    wire             ram_we    = flushing || w_en_q || fill_done;
    wire             use_fill  = fill_done;
    wire [SET_W-1:0] ram_waddr = flushing ? flush_idx : use_fill ? fill_idx : w_idx_q;
    wire             ram_valid = !flushing && (use_fill || ((K == 1) && w_full_q));
    wire [TAG_W-1:0] ram_tag   = use_fill ? fill_tag : w_tag_q;
    wire [CW-1:0]    ram_data  = use_fill ? fill_line : {K{w_word_q}};

    kohaku_sdpram #(.WIDTH(ROW_W), .DEPTH(SETS), .MEM_PRIM(RAM_STYLE), .READ_LAT(RD_LAT)) u_arr (
        .clk(clk), .wr_en(ram_we), .wr_addr(ram_waddr), .wr_data({ram_valid, ram_tag, ram_data}),
        .rd_en(1'b1), .rd_addr(rd_idx), .rd_data(rd_row)
    );

    assign flush_busy = flushing;
endmodule

`default_nettype wire

// kht_icache -- the SIMT PE's instruction cache: direct mapped, 32-byte lines
// (8 instructions) over DRAM, each word stored beside its predecoded control.
//
// It is the instruction window with tags. The two memories a fetch reads are
// the same shape as the window's -- the instruction and its 60-bit control,
// READ_LAT 1 -- so the core's fetch stages do not change; `rd_hit` is the only
// new output, valid on the same cycle as the data.
//
// DECODE STAYS ON THE WRITE PATH. A fill arrives as one 256-bit entry and is
// written one word a cycle through one kht_predec, so a fetch still reads
// control out of memory instead of computing it.
//
// A MISS IS THE CORE'S TO SCHEDULE. This block only fills the line it is asked
// for, one at a time, and says when; which wave sleeps on it is the core's
// decision (compute-unit.md s3.1).

`default_nettype none

module kht_icache #(
    parameter integer LINES    = 256,     // power of two
    parameter         MEM_PRIM = "block"
)(
    input  wire         clk,
    input  wire         resetn,

    // ---- fetch: the address with the array cycle, the answer the next ----
    input  wire [31:0]  rd_addr,
    output wire [31:0]  rd_instr,
    output wire [59:0]  rd_ctrl,
    output wire         rd_hit,

    // ---- miss service -----------------------------------------------------
    input  wire         miss_req,
    input  wire [31:0]  miss_addr,
    output wire         miss_ready,
    output reg          fill_done,
    output reg  [31:0]  fill_line,

    // Invalidate every line (a pulse). `busy` covers the sweep and a fill.
    input  wire         inval,
    output wire         busy,

    // ---- the memory side: one entry read per fill ----------------------------
    output reg          fill_valid,
    input  wire         fill_ready,
    output reg  [31:0]  fill_addr,
    input  wire         resp_valid,
    input  wire [255:0] resp_data
);
    localparam integer IDX  = $clog2(LINES);
    localparam integer TAGW = 32 - 5 - IDX;
    localparam integer TW   = TAGW + 1;
    localparam integer WAW  = IDX + 3;

    localparam [2:0] S_SWEEP = 3'd0, S_IDLE = 3'd1, S_REQ = 3'd2;
    localparam [2:0] S_WAIT = 3'd3, S_WR = 3'd4;
    reg [2:0] st;

    reg  [IDX:0]   scan;
    reg  [255:0]   linebuf;
    reg  [2:0]     wcnt;
    reg  [31:0]    ra_q;

    // ---- tags: {valid, tag}, distributed, read with the data array ----------
    reg            tag_we;
    reg  [IDX-1:0] tag_wa;
    reg  [TW-1:0]  tag_wd;
    wire [TW-1:0]  tag_q;

    kohaku_sdpram #(.WIDTH(TW), .DEPTH(LINES), .MEM_PRIM("distributed"),
                    .READ_LAT(1)) u_tag (
        .clk(clk),
        .wr_en(tag_we), .wr_addr(tag_wa), .wr_data(tag_wd),
        .rd_en(1'b1), .rd_addr(rd_addr[IDX+4:5]), .rd_data(tag_q)
    );

    always @(posedge clk) begin
        ra_q <= rd_addr;
    end

    // A sweep in progress answers nothing: a line it has not reached yet still
    // holds what the power-on array or the previous program left.
    assign rd_hit = (st != S_SWEEP) && tag_q[TW-1]
                 && (tag_q[TAGW-1:0] == ra_q[31:IDX+5]);

    // ---- the data: {control, instruction} per word -----------------------------
    wire [31:0] wr_word = linebuf[{wcnt, 5'd0} +: 32];
    wire [59:0] wr_ctrl;
    kht_predec u_predec (.instr(wr_word), .ctrl(wr_ctrl));

    wire [91:0] rd_q;
    kohaku_sdpram #(.WIDTH(92), .DEPTH(LINES * 8), .MEM_PRIM(MEM_PRIM),
                    .READ_LAT(1)) u_data (
        .clk(clk),
        .wr_en(st == S_WR), .wr_addr({fill_addr[IDX+4:5], wcnt}),
        .wr_data({wr_ctrl, wr_word}),
        .rd_en(1'b1), .rd_addr(rd_addr[WAW+1:2]), .rd_data(rd_q)
    );
    assign rd_instr = rd_q[31:0];
    assign rd_ctrl  = rd_q[91:32];

    assign miss_ready = (st == S_IDLE) && !inval;
    assign busy       = (st != S_IDLE);

    always @(posedge clk) begin
        if (!resetn) begin
            // The tag array is LUTRAM and has no reset, so the first thing a
            // reset does is the sweep an invalidate would.
            st         <= S_SWEEP;
            scan       <= {(IDX+1){1'b0}};
            fill_valid <= 1'b0;
            fill_done  <= 1'b0;
            tag_we     <= 1'b0;
            wcnt       <= 3'd0;
        end else begin
            tag_we    <= 1'b0;
            fill_done <= 1'b0;

            case (st)
                S_SWEEP: if (scan[IDX]) begin
                    st <= S_IDLE;
                end
                else begin
                    tag_we <= 1'b1;
                    tag_wa <= scan[IDX-1:0];
                    tag_wd <= {TW{1'b0}};
                    scan   <= scan + 1'b1;
                end

                S_IDLE: if (inval) begin
                    scan <= {(IDX+1){1'b0}};
                    st   <= S_SWEEP;
                end
                else if (miss_req) begin
                    fill_addr  <= {miss_addr[31:5], 5'd0};
                    fill_valid <= 1'b1;
                    st         <= S_REQ;
                end

                S_REQ: if (fill_ready) begin
                    fill_valid <= 1'b0;
                    st         <= S_WAIT;
                end

                S_WAIT: if (resp_valid) begin
                    linebuf <= resp_data;
                    wcnt    <= 3'd0;
                    st      <= S_WR;
                end

                // The tag goes valid with the last word, so a fetch of this line
                // can never see a word the fill has not written yet.
                S_WR: begin
                    wcnt <= wcnt + 3'd1;
                    if (wcnt == 3'd7) begin
                        tag_we    <= 1'b1;
                        tag_wa    <= fill_addr[IDX+4:5];
                        tag_wd    <= {1'b1, fill_addr[31:IDX+5]};
                        fill_done <= 1'b1;
                        fill_line <= fill_addr;
                        st        <= S_IDLE;
                    end
                end

                default: st <= S_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire

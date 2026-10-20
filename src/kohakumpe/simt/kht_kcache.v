// kht_kcache -- the SIMT PE's scalar (constant) cache: direct mapped, 32-byte
// lines over DRAM, read combinationally so `sld` answers in EX like `rdctl`.
//
// Small and in distributed RAM: kernel arguments and table constants are a few
// lines, and a combinational read is what keeps a scalar load a one-cycle
// instruction on a hit. A line is one 256-bit word, so a fill is one write and
// a read is the line and an 8:1 select.
//
// A MISS IS THE CORE'S TO SCHEDULE, exactly as in kht_icache: this block fills
// the line it is asked for, one at a time, and says when.

`default_nettype none

module kht_kcache #(
    parameter integer LINES = 16      // power of two
)(
    input  wire         clk,
    input  wire         resetn,

    // ---- lookup, combinational ----
    input  wire [31:0]  rd_addr,
    output wire [31:0]  rd_data,
    output wire         rd_hit,

    // ---- miss service ----
    input  wire         miss_req,
    input  wire [31:0]  miss_addr,
    output wire         miss_ready,
    output reg          fill_done,

    input  wire         inval,
    output wire         busy,

    // ---- memory side ----
    output reg          fill_valid,
    input  wire         fill_ready,
    output reg  [31:0]  fill_addr,
    input  wire         resp_valid,
    input  wire [255:0] resp_data
);
    localparam integer IDX  = $clog2(LINES);
    localparam integer TAGW = 32 - 5 - IDX;
    localparam integer TW   = TAGW + 1;

    localparam [1:0] S_SWEEP = 2'd0, S_IDLE = 2'd1, S_REQ = 2'd2, S_WAIT = 2'd3;
    reg [1:0]     st;
    reg [IDX:0]   scan;

    reg            tag_we;
    reg  [IDX-1:0] tag_wa;
    reg  [TW-1:0]  tag_wd;
    wire [TW-1:0]  tag_q;

    kohaku_sdpram #(.WIDTH(TW), .DEPTH(LINES), .MEM_PRIM("distributed"),
                    .READ_LAT(0)) u_tag (
        .clk(clk),
        .wr_en(tag_we), .wr_addr(tag_wa), .wr_data(tag_wd),
        .rd_en(1'b1), .rd_addr(rd_addr[IDX+4:5]), .rd_data(tag_q)
    );

    wire [255:0] line_q;
    kohaku_sdpram #(.WIDTH(256), .DEPTH(LINES), .MEM_PRIM("distributed"),
                    .READ_LAT(0)) u_data (
        .clk(clk),
        .wr_en((st == S_WAIT) && resp_valid), .wr_addr(fill_addr[IDX+4:5]),
        .wr_data(resp_data),
        .rd_en(1'b1), .rd_addr(rd_addr[IDX+4:5]), .rd_data(line_q)
    );

    assign rd_data = line_q[{rd_addr[4:2], 5'd0} +: 32];
    assign rd_hit  = (st != S_SWEEP) && tag_q[TW-1]
                  && (tag_q[TAGW-1:0] == rd_addr[31:IDX+5]);

    assign miss_ready = (st == S_IDLE) && !inval;
    assign busy       = (st != S_IDLE);

    always @(posedge clk) begin
        if (!resetn) begin
            st         <= S_SWEEP;
            scan       <= {(IDX+1){1'b0}};
            fill_valid <= 1'b0;
            fill_done  <= 1'b0;
            tag_we     <= 1'b0;
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

                // The line is written on the response's edge and the tag a cycle
                // after it, so a lookup never sees a valid tag over the old line.
                default: if (resp_valid) begin
                    tag_we    <= 1'b1;
                    tag_wa    <= fill_addr[IDX+4:5];
                    tag_wd    <= {1'b1, fill_addr[31:IDX+5]};
                    fill_done <= 1'b1;
                    st        <= S_IDLE;
                end
            endcase
        end
    end

endmodule

`default_nettype wire

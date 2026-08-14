// noc_local_cdc -- one NoC local link across two clocks, one direction.

// A BUFGCE-gated unit is the SAME clock with edges removed and needs none of
// this. A DIVIDED unit clock is a different domain and needs all of it.

// The local link is valid/busy with RETRY and no credit, so backpressure must
// be exact -- `busy` is the FIFO's own write port, no synchroniser in the path.

// DEPTH IS A THROUGHPUT KNOB, NOT A CORRECTNESS ONE: below the pointer round
// trip `full` deasserts late and the sender gaps. Curve in the bench.

`default_nettype none

module noc_local_cdc #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer DEPTH      = 16,
    parameter         MEM_TYPE   = "distributed"
)(
    input  wire                   wr_clk,
    input  wire                   wr_resetn,
    input  wire [FLIT_WIDTH-1:0]  i_data,
    input  wire                   i_valid,
    output wire                   i_busy,

    input  wire                   rd_clk,
    input  wire                   rd_resetn,
    output wire [FLIT_WIDTH-1:0]  o_data,
    output wire                   o_valid,
    input  wire                   o_busy
);
    wire rst = !wr_resetn || !rd_resetn;

    // Deassertion is asynchronous to at least one clock. Async-assert,
    // sync-deassert, per domain.
    reg [1:0] w_rst_q, r_rst_q;
    always @(posedge wr_clk or posedge rst)
        if (rst) w_rst_q <= 2'b11; else w_rst_q <= {w_rst_q[0], 1'b0};
    always @(posedge rd_clk or posedge rst)
        if (rst) r_rst_q <= 2'b11; else r_rst_q <= {r_rst_q[0], 1'b0};

    wire w_rst = w_rst_q[1];
    wire wr_full, rd_empty;

    // EXACT, not advisory: the sender retries while `busy`, so a beat is only
    // lost if `busy` is low when the FIFO cannot take it.
    assign i_busy = wr_full;

    async_fifo #(.DATA_WIDTH(FLIT_WIDTH), .FIFO_DEPTH(DEPTH),
                 .MEMORY_TYPE(MEM_TYPE)) u_fifo (
        .wr_clk(wr_clk), .wr_rst(w_rst),
        .wr_en(i_valid && !wr_full), .wr_data(i_data), .wr_full(wr_full),
        .rd_clk(rd_clk), .rd_en(!rd_empty && !o_busy),
        .rd_data(o_data), .rd_empty(rd_empty)
    );

    assign o_valid = !rd_empty;

endmodule

`default_nettype wire

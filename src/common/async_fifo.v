// Asynchronous FIFO over xpm_fifo_async -- the clock crossing, and nothing else.
//
// Separate from sync_fifo rather than a mode of it: the two have different
// ports (two clocks, two resets) and the same name for both would let a
// single-clock instantiation compile against a crossing, which is a data
// corruption nobody would find in simulation because both clocks are ideal there.
//
// FWFT and FIFO_READ_LATENCY(0) match sync_fifo, so a reader written against one
// works against the other. CDC_SYNC_STAGES is 2: the pointer synchronisers are
// the whole reason this module exists and XPM's default is the right one.

`default_nettype none

module async_fifo #(
    parameter integer DATA_WIDTH  = 64,
    parameter integer FIFO_DEPTH  = 16,           // must be a power of 2
    parameter         MEMORY_TYPE = "distributed" // "distributed" | "block"
)(
    input  wire                  wr_clk,
    input  wire                  wr_rst,          // active high, wr_clk domain
    input  wire                  wr_en,
    input  wire [DATA_WIDTH-1:0] wr_data,
    output wire                  wr_full,

    input  wire                  rd_clk,
    input  wire                  rd_en,
    output wire [DATA_WIDTH-1:0] rd_data,
    output wire                  rd_empty
);
    wire wr_rst_busy, rd_rst_busy, full, empty;

    // Reset busy folded into the flags rather than exposed. XPM holds both for
    // several cycles after reset and a writer that ignores them loses the first
    // beats, which presents as a burst that is short by a random amount.

    // One LOCAL flag per domain: the macro's rst_busy sits with the FIFO memory
    // and OR-ing it in drove failing control paths mesh-wide.
    reg wr_busy_q = 1'b1;
    always @(posedge wr_clk) begin
        if (wr_rst) wr_busy_q <= 1'b1;
        else        wr_busy_q <= wr_rst_busy;
    end
    reg rd_busy_q = 1'b1;
    always @(posedge rd_clk) rd_busy_q <= rd_rst_busy;

    assign wr_full  = full  | wr_busy_q;
    assign rd_empty = empty | rd_busy_q;

    xpm_fifo_async #(
        .CASCADE_HEIGHT(0),
        .CDC_SYNC_STAGES(2),
        .DOUT_RESET_VALUE("0"),
        .ECC_MODE("no_ecc"),
        .EN_SIM_ASSERT_ERR("warning"),
        .FIFO_MEMORY_TYPE(MEMORY_TYPE),
        .FIFO_READ_LATENCY(0),
        .FIFO_WRITE_DEPTH(FIFO_DEPTH),
        .FULL_RESET_VALUE(0),
        .PROG_EMPTY_THRESH(5),
        .PROG_FULL_THRESH(5),
        .READ_DATA_WIDTH(DATA_WIDTH),
        .READ_MODE("fwft"),
        .RELATED_CLOCKS(0),
        .SIM_ASSERT_CHK(1),
        .USE_ADV_FEATURES(13'b0000000000000),
        .WAKEUP_TIME(0),
        .WRITE_DATA_WIDTH(DATA_WIDTH)
    ) u_fifo (
        .dout(rd_data),
        .empty(empty),
        .full(full),
        .rd_rst_busy(rd_rst_busy),
        .wr_rst_busy(wr_rst_busy),
        .din(wr_data),
        .rd_clk(rd_clk),
        .rd_en(rd_en),
        .rst(wr_rst),
        .wr_clk(wr_clk),
        .wr_en(wr_en),
        .injectdbiterr(1'b0),
        .injectsbiterr(1'b0),
        .sleep(1'b0)
    );
endmodule

`default_nettype wire

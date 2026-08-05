// Synchronous FIFO over xpm_fifo_sync.
//
// MEMORY_TYPE picks the storage primitive, because the right answer differs by
// use. Router flit buffers want "distributed": depth only has to cover the
// backpressure round-trip, and an SRL32 costs ~1 LUT per bit at any depth up to
// 32, so a URAM there wastes over 97% of a 288 Kbit block. Instruction FIFOs want
// "block": a RAMB36E2's widest shape is 512x72, so a 288-bit entry is 4 BRAMs and
// depth 512 fills them exactly.

module sync_fifo #(
    parameter DATA_WIDTH        = 288,
    parameter FIFO_DEPTH        = 32,             // must be a power of 2
    parameter MEMORY_TYPE       = "distributed",  // "distributed" | "block" | "ultra"
    parameter PROG_FULL_THRESH  = FIFO_DEPTH - 5,
    parameter PROG_EMPTY_THRESH = 5
) (
    input  wire                     clk,
    input  wire                     rst,

    // Write interface
    input  wire                     wr_en,
    input  wire [DATA_WIDTH-1:0]    wr_data,
    output wire                     wr_busy,

    // Read interface
    input  wire                     rd_en,
    output wire [DATA_WIDTH-1:0]    rd_data,
    output wire                     rd_busy
);
    wire rd_rst_busy, wr_rst_busy, empty, full;
    assign wr_busy = full | wr_rst_busy;
    assign rd_busy = empty | rd_rst_busy;

    xpm_fifo_sync #(
        .CASCADE_HEIGHT(0),
        .DOUT_RESET_VALUE("0"),
        .ECC_MODE("no_ecc"),
        .EN_SIM_ASSERT_ERR("warning"),
        .FIFO_MEMORY_TYPE(MEMORY_TYPE),
        .FIFO_READ_LATENCY(0),
        .FIFO_WRITE_DEPTH(FIFO_DEPTH),
        .FULL_RESET_VALUE(0),
        .PROG_EMPTY_THRESH(PROG_EMPTY_THRESH),
        .PROG_FULL_THRESH(PROG_FULL_THRESH),
        .READ_DATA_WIDTH(DATA_WIDTH),
        .READ_MODE("fwft"),
        .SIM_ASSERT_CHK(1),
        .USE_ADV_FEATURES(13'b0000000000000),
        .WRITE_DATA_WIDTH(DATA_WIDTH)
    )
    xpm_fifo_sync_inst (
        .dout(rd_data),
        .empty(empty),
        .full(full),
        .rd_rst_busy(rd_rst_busy),
        .wr_rst_busy(wr_rst_busy),
        .din(wr_data),
        .rd_en(rd_en),
        .rst(rst),
        .wr_clk(clk),
        .wr_en(wr_en)
    );
endmodule

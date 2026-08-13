// Dual-clock FIFO over xpm_fifo_async, port-compatible with sync_fifo apart
// from wr_clk/rd_clk and the added wr_count.

// ADV bit 2 = wr_data_count, needed by inst_used; bit 0 = overflow, without
// which a write to a full FIFO is DISCARDED silently (the mag_dram_port bug).

module async_fifo #(
    parameter DATA_WIDTH  = 288,
    parameter FIFO_DEPTH  = 32,             // power of 2
    parameter MEMORY_TYPE = "distributed",
    parameter CDC_STAGES  = 2
) (
    input  wire                  wr_clk,
    input  wire                  rd_clk,
    input  wire                  rst,       // asserted in the WRITE domain

    input  wire                  wr_en,
    input  wire [DATA_WIDTH-1:0] wr_data,
    output wire                  wr_busy,
    output wire                  wr_overflow,
    output wire [$clog2(FIFO_DEPTH):0] wr_count,

    input  wire                  rd_en,
    output wire [DATA_WIDTH-1:0] rd_data,
    output wire                  rd_busy
);
    wire rd_rst_busy, wr_rst_busy, empty, full;
    assign wr_busy = full | wr_rst_busy;
    assign rd_busy = empty | rd_rst_busy;

    xpm_fifo_async #(
        .CASCADE_HEIGHT(0),
        .CDC_SYNC_STAGES(CDC_STAGES),
        .DOUT_RESET_VALUE("0"),
        .ECC_MODE("no_ecc"),
        .EN_SIM_ASSERT_ERR("warning"),
        .FIFO_MEMORY_TYPE(MEMORY_TYPE),
        .FIFO_READ_LATENCY(0),
        .FIFO_WRITE_DEPTH(FIFO_DEPTH),
        .FULL_RESET_VALUE(0),
        .READ_DATA_WIDTH(DATA_WIDTH),
        .READ_MODE("fwft"),
        .RELATED_CLOCKS(0),
        .SIM_ASSERT_CHK(1),
        // A HEX STRING, not a bit vector: bit0 overflow + bit2 wr_data_count.
        // A vector parses as garbage and XPM rejects it at elaboration.
        .USE_ADV_FEATURES("0005"),
        .WRITE_DATA_WIDTH(DATA_WIDTH),
        .WR_DATA_COUNT_WIDTH($clog2(FIFO_DEPTH)+1)
    ) u_fifo (
        .dout(rd_data),
        .empty(empty),
        .full(full),
        .overflow(wr_overflow),
        .wr_data_count(wr_count),
        .rd_rst_busy(rd_rst_busy),
        .wr_rst_busy(wr_rst_busy),
        .din(wr_data),
        .rd_clk(rd_clk),
        .rd_en(rd_en),
        .rst(rst),
        .wr_clk(wr_clk),
        .wr_en(wr_en)
    );
endmodule

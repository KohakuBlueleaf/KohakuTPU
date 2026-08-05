module uram_fifo #(
    parameter DATA_WIDTH = 512,
    parameter FIFO_DEPTH = 1024,  // Must be power of 2
    parameter PROG_FULL_THRESH = FIFO_DEPTH - 5,
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
        .FIFO_MEMORY_TYPE("ultra"),
        .FIFO_READ_LATENCY(0),
        .FIFO_WRITE_DEPTH(FIFO_DEPTH),
        .FULL_RESET_VALUE(0),
        .PROG_EMPTY_THRESH(PROG_EMPTY_THRESH),
        .PROG_FULL_THRESH(PROG_FULL_THRESH),
        .READ_DATA_WIDTH(DATA_WIDTH),
        .READ_MODE("fwft"),
        .SIM_ASSERT_CHK(1),
        .USE_ADV_FEATURES(13'b0000000000000), // No ADV features for simplicity
        .WRITE_DATA_WIDTH(DATA_WIDTH)
    )
    xpm_fifo_sync_inst (
        .dout(rd_data),
        .empty(empty),
        .full(full),
        .rd_data_count(rd_data_count),
        .rd_rst_busy(rd_rst_busy),
        .wr_rst_busy(wr_rst_busy),
        .din(wr_data),
        .rd_en(rd_en),
        .rst(rst),
        .wr_clk(clk),
        .wr_en(wr_en)
    );
endmodule
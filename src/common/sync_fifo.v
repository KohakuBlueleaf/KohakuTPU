// Synchronous FIFO over xpm_fifo_sync.
//
// MEMORY_TYPE picks the storage primitive, because the right answer differs by
// use. Instruction FIFOs want "block": a RAMB36E2's widest shape is 512x72, so a
// 288-bit entry is 4 BRAMs and depth 512 fills them exactly.
//
// ROUTER FLIT BUFFERS NOW WANT "block" TOO, and that reverses the argument this
// comment used to make. A 32-deep 288-bit buffer really does waste 94% of the 4
// BRAMs it occupies -- but it is 168 LUT in distributed RAM, the design is
// LUT-bound and only LUT, and BRAM sits near empty. Measured on one NoCRouter at
// 320 MHz: 3,751 -> 2,911 LUT for 20 BRAM tiles, 451 -> 410 MHz. The waste is
// the point of the trade, not an argument against it.

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
    // NOT A MARGIN, despite the name and despite PROG_FULL_THRESH being passed:
    // USE_ADV_FEATURES below is zero, so XPM ties `prog_full` low and this
    // reduces to `wr_busy`. It never asserts early.
    //
    // Survivable only because the NoC link RETRIES -- sender holds `valid` until
    // a cycle with `busy` low, receiver accepts exactly then -- which needs no
    // margin (docs/noc/spec.md s2.1). Anything wanting a real margin must COUNT
    // FOR ITSELF, as MAG does with Q_MARGIN; turning the feature on means
    // editing USE_ADV_FEATURES, and nothing should depend on this bit until it
    // is.
    output wire                     wr_almost,

    // Read interface
    input  wire                     rd_en,
    output wire [DATA_WIDTH-1:0]    rd_data,
    output wire                     rd_busy
);
    wire rd_rst_busy, wr_rst_busy, empty, full, prog_full;

    // A LOCAL copy. The macro's own rst_busy sits with the FIFO memory, and
    // OR-ing it into wr_busy made it the top driver of failing control paths.
    reg rst_busy_q = 1'b1;
    always @(posedge clk) begin
        if (rst) rst_busy_q <= 1'b1;
        else     rst_busy_q <= wr_rst_busy | rd_rst_busy;
    end

    assign wr_busy   = full | rst_busy_q;
    assign wr_almost = full | prog_full | rst_busy_q;
    assign rd_busy   = empty | rst_busy_q;

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
        .prog_full(prog_full),
        .rd_rst_busy(rd_rst_busy),
        .wr_rst_busy(wr_rst_busy),
        .din(wr_data),
        .rd_en(rd_en),
        .rst(rst),
        .wr_clk(clk),
        .wr_en(wr_en)
    );
endmodule

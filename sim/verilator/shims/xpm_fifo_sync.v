// A stand-in for xpm_fifo_sync, used only under Verilator. See
// xpm_memory_sdpram.v for why the Xilinx source cannot be used directly.
//
// Models the slice sync_fifo.v pins: READ_MODE "fwft", FIFO_READ_LATENCY 0,
// no ECC, USE_ADV_FEATURES 0. `prog_full`/`prog_empty` are tied LOW because
// USE_ADV_FEATURES 0 is what XPM does -- sync_fifo.v's own comment depends on
// that exact behaviour for wr_almost.

`default_nettype none

module xpm_fifo_sync #(
    parameter integer CASCADE_HEIGHT    = 0,
    parameter         DOUT_RESET_VALUE  = "0",
    parameter         ECC_MODE          = "no_ecc",
    parameter         EN_SIM_ASSERT_ERR = "warning",
    parameter         FIFO_MEMORY_TYPE  = "distributed",
    parameter integer FIFO_READ_LATENCY = 0,
    parameter integer FIFO_WRITE_DEPTH  = 32,
    parameter integer FULL_RESET_VALUE  = 0,
    parameter integer PROG_EMPTY_THRESH = 5,
    parameter integer PROG_FULL_THRESH  = 27,
    parameter integer READ_DATA_WIDTH   = 288,
    parameter         READ_MODE         = "fwft",
    parameter integer SIM_ASSERT_CHK    = 0,
    parameter [12:0]  USE_ADV_FEATURES  = 13'b0,
    parameter integer WRITE_DATA_WIDTH  = 288
)(
    output wire [READ_DATA_WIDTH-1:0] dout,
    output wire                       empty,
    output wire                       full,
    output wire                       prog_full,
    output wire                       prog_empty,
    output wire                       rd_rst_busy,
    output wire                       wr_rst_busy,
    output wire                       overflow,
    output wire                       underflow,
    output wire                       wr_ack,
    output wire                       data_valid,
    output wire [$clog2(FIFO_WRITE_DEPTH):0] rd_data_count,
    output wire [$clog2(FIFO_WRITE_DEPTH):0] wr_data_count,
    input  wire [WRITE_DATA_WIDTH-1:0] din,
    input  wire                        rd_en,
    input  wire                        rst,
    input  wire                        wr_clk,
    input  wire                        wr_en,
    input  wire                        injectsbiterr,
    input  wire                        injectdbiterr,
    input  wire                        sleep
);
    localparam integer AW = $clog2(FIFO_WRITE_DEPTH);

    initial begin
        if (READ_MODE != "fwft")
            $fatal(1, "xpm_fifo_sync shim: READ_MODE=%s not modelled", READ_MODE);
        if (FIFO_READ_LATENCY != 0)
            $fatal(1, "xpm_fifo_sync shim: FIFO_READ_LATENCY=%0d not modelled", FIFO_READ_LATENCY);
        if (ECC_MODE != "no_ecc")
            $fatal(1, "xpm_fifo_sync shim: ECC_MODE=%s not modelled", ECC_MODE);
        if (USE_ADV_FEATURES != 13'b0)
            $fatal(1, "xpm_fifo_sync shim: USE_ADV_FEATURES=%b not modelled", USE_ADV_FEATURES);
    end

    // CAPACITY IS DEPTH+2, NOT DEPTH. In fwft the real cell carries two words in
    // output stages beyond the array, so it accepts DEPTH+2 before `full`.
    // MEASURED, both simulators on sim/verilator/examples/vlt_sync_fifo_tb.v:
    // the Xilinx cell peaks at 18 words in flight for FIFO_WRITE_DEPTH 16, this
    // shim peaked at 16 before the fix. Two words shallow is not a margin issue
    // -- mag_link sizes link credit against the real depth, so the sender pushes
    // credit the FIFO cannot hold and the link deadlocks with no error.
    localparam integer CAP = FIFO_WRITE_DEPTH + 2;

    reg [WRITE_DATA_WIDTH-1:0] mem [0:CAP-1];
    reg [31:0] wptr = 32'd0, rptr = 32'd0, cnt = 32'd0;

    wire do_wr = wr_en && !full;
    wire do_rd = rd_en && !empty;

    always @(posedge wr_clk) begin
        if (rst) begin
            wptr <= 32'd0;
            rptr <= 32'd0;
            cnt  <= 32'd0;
        end
        else begin
            if (do_wr) begin
                mem[wptr] <= din;
                wptr <= (wptr == CAP - 1) ? 32'd0 : wptr + 32'd1;
            end
            if (do_rd) rptr <= (rptr == CAP - 1) ? 32'd0 : rptr + 32'd1;
            cnt <= cnt + (do_wr ? 32'd1 : 32'd0) - (do_rd ? 32'd1 : 32'd0);
        end
    end

    // XPM holds rst_busy for a few cycles after reset; sync_fifo.v registers it
    // into its own rst_busy_q, so a one-cycle model is enough to keep that path.
    reg busy = 1'b1;
    always @(posedge wr_clk) busy <= rst;

    assign full  = (cnt == CAP);
    assign empty = (cnt == 32'd0);
    assign dout  = mem[rptr];

    assign wr_rst_busy = busy;
    assign rd_rst_busy = busy;
    assign prog_full   = 1'b0;
    assign prog_empty  = 1'b0;
    assign overflow    = 1'b0;
    assign underflow   = 1'b0;
    assign wr_ack      = 1'b0;
    assign data_valid  = 1'b0;
    assign rd_data_count = cnt[$clog2(FIFO_WRITE_DEPTH):0];
    assign wr_data_count = cnt[$clog2(FIFO_WRITE_DEPTH):0];

endmodule

`default_nettype wire

// Dual-clock first-word-fall-through FIFO over xpm_fifo_async: the receive
// buffer of a surface whose two ends run on different clocks. Its own module,
// so a single-clock instantiation can never compile against a crossing.

`default_nettype none

module kts_afifo #(
    parameter integer W     = 288,
    parameter integer DEPTH = 32,               // power of 2, >= 16 for xpm
    parameter         MEM   = "distributed"     // "distributed"|"block"
)(
    input  wire         wr_clk,
    input  wire         wr_rst,                 // active high, wr_clk domain
    input  wire         wr_en,
    input  wire [W-1:0] wr_data,
    output wire         full,

    input  wire         rd_clk,
    input  wire         rd_en,
    output wire [W-1:0] rd_data,
    output wire         empty
);
    wire wr_rst_busy, rd_rst_busy, xfull, xempty;

    reg wr_busy_q = 1'b1;
    always @(posedge wr_clk) begin
        if (wr_rst) begin
            wr_busy_q <= 1'b1;
        end
        else begin
            wr_busy_q <= wr_rst_busy;
        end
    end
    reg rd_busy_q = 1'b1;
    always @(posedge rd_clk) begin
        rd_busy_q <= rd_rst_busy;
    end

    assign full  = xfull  | wr_busy_q;
    assign empty = xempty | rd_busy_q;

    xpm_fifo_async #(
        .CASCADE_HEIGHT(0),
        .CDC_SYNC_STAGES(2),
        .DOUT_RESET_VALUE("0"),
        .ECC_MODE("no_ecc"),
        .EN_SIM_ASSERT_ERR("warning"),
        .FIFO_MEMORY_TYPE(MEM),
        .FIFO_READ_LATENCY(0),
        .FIFO_WRITE_DEPTH(DEPTH),
        .FULL_RESET_VALUE(0),
        .PROG_EMPTY_THRESH(5),
        .PROG_FULL_THRESH(DEPTH - 5),
        .READ_DATA_WIDTH(W),
        .READ_MODE("fwft"),
        .RELATED_CLOCKS(0),
        .SIM_ASSERT_CHK(1),
        .USE_ADV_FEATURES(13'b0000000000000),
        .WAKEUP_TIME(0),
        .WRITE_DATA_WIDTH(W)
    ) u_fifo (
        .dout(rd_data),
        .empty(xempty),
        .full(xfull),
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

// Synchronous first-word-fall-through FIFO over xpm_fifo_sync: the receive
// buffer of a surface. Explicit primitive, never inferred; DEPTH a power of 2.

`default_nettype none

module kts_fifo #(
    parameter integer W     = 288,
    parameter integer DEPTH = 32,               // power of 2, >= 16 for xpm
    parameter         MEM   = "distributed"     // "distributed"|"block"|"ultra"
)(
    input  wire         clk,
    input  wire         rst,                    // active high

    input  wire         wr_en,
    input  wire [W-1:0] wr_data,
    output wire         full,

    input  wire         rd_en,
    output wire [W-1:0] rd_data,
    output wire         empty
);
    wire wr_rst_busy, rd_rst_busy, xfull, xempty;

    // A local copy: the macro's rst_busy sits with the FIFO memory and is a
    // long wire into every flag it is OR-ed into.
    reg busy_q = 1'b1;
    always @(posedge clk) begin
        if (rst) begin
            busy_q <= 1'b1;
        end
        else begin
            busy_q <= wr_rst_busy | rd_rst_busy;
        end
    end

    assign full  = xfull  | busy_q;
    assign empty = xempty | busy_q;

    xpm_fifo_sync #(
        .CASCADE_HEIGHT(0),
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
        .SIM_ASSERT_CHK(1),
        .USE_ADV_FEATURES(13'b0000000000000),
        .WRITE_DATA_WIDTH(W)
    ) u_fifo (
        .dout(rd_data),
        .empty(xempty),
        .full(xfull),
        .prog_full(),
        .rd_rst_busy(rd_rst_busy),
        .wr_rst_busy(wr_rst_busy),
        .din(wr_data),
        .rd_en(rd_en),
        .rst(rst),
        .wr_clk(clk),
        .wr_en(wr_en)
    );

endmodule

`default_nettype wire

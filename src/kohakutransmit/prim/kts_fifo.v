// Synchronous first-word-fall-through FIFO: the receive buffer of a surface.
// Explicit primitive, never inferred; DEPTH a power of 2.
//
// IMPL "ring" is ours: a distributed-RAM ring with a combinational read, which
// IS fall-through, and pointers carrying one extra bit so full and empty are a
// compare rather than a count. IMPL "xpm" is xpm_fifo_sync in fwft mode, whose
// internal flag path is 7 LUT levels between its own flops and is not ours to
// restructure.

`default_nettype none

module kts_fifo #(
    parameter integer W     = 288,
    parameter integer DEPTH = 32,               // power of 2, >= 16 for xpm
    parameter         MEM   = "distributed",    // "distributed"|"block"|"ultra"
    parameter         IMPL  = "xpm"             // "ring"|"xpm"
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
    // A fall-through read is only free on LUTRAM, so a caller asking for block
    // or ultra storage gets the macro whatever IMPL says.
    localparam integer RING = ((IMPL == "ring") && (MEM == "distributed")) ? 1 : 0;

    generate if (RING != 0) begin : g_ring
        localparam integer AW = (DEPTH <= 1) ? 1 : $clog2(DEPTH);
        reg  [AW:0] wp, rp;
        wire        f_i = (wp[AW] != rp[AW]) && (wp[AW-1:0] == rp[AW-1:0]);
        wire        e_i = (wp == rp);
        // held over reset, as the macro's rst_busy did, so no side moves while
        // the pointers are being cleared
        reg         busy_q = 1'b1;
        always @(posedge clk) begin
            busy_q <= rst;
            if (rst) begin
                wp <= {(AW+1){1'b0}};
                rp <= {(AW+1){1'b0}};
            end else begin
                if (wr_en && !f_i) begin wp <= wp + 1'b1; end
                if (rd_en && !e_i) begin rp <= rp + 1'b1; end
            end
        end
        assign full  = f_i | busy_q | rst;
        assign empty = e_i | busy_q | rst;

        // xpm directly, as the macro above is: this library names its own
        // primitives and depends on nothing outside itself
        xpm_memory_sdpram #(
            .ADDR_WIDTH_A(AW), .ADDR_WIDTH_B(AW),
            .WRITE_DATA_WIDTH_A(W), .READ_DATA_WIDTH_B(W),
            .BYTE_WRITE_WIDTH_A(W),
            .MEMORY_SIZE(W * DEPTH),
            .MEMORY_PRIMITIVE(MEM),
            .CLOCKING_MODE("common_clock"),
            .READ_LATENCY_B(0),
            .WRITE_MODE_B("read_first"),
            .MEMORY_INIT_FILE("none"), .USE_MEM_INIT(0),
            .ECC_MODE("no_ecc"), .AUTO_SLEEP_TIME(0),
            .CASCADE_HEIGHT(0), .SIM_ASSERT_CHK(0),
            .WAKEUP_TIME("disable_sleep")
        ) u_ram (
            .clka(clk), .ena(wr_en && !f_i && !rst), .wea(1'b1),
            .addra(wp[AW-1:0]), .dina(wr_data),
            .clkb(clk), .enb(1'b1), .addrb(rp[AW-1:0]), .doutb(rd_data),
            .rstb(1'b0), .regceb(1'b1),
            .injectsbiterra(1'b0), .injectdbiterra(1'b0),
            .sbiterrb(), .dbiterrb(), .sleep(1'b0)
        );
    end else begin : g_xpm
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
    end endgenerate

endmodule

`default_nettype wire

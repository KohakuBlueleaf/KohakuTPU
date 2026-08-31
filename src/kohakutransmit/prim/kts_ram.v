// Simple dual-port RAM over xpm_memory_sdpram: the retention buffer of a
// reliable link and the row store of a switch. Explicit primitive; the read
// latency is a parameter the caller builds its pipeline on.

`default_nettype none

module kts_ram #(
    parameter integer W        = 288,
    parameter integer DEPTH    = 64,
    parameter         MEM      = "distributed", // "distributed"|"block"|"ultra"
    parameter integer READ_LAT = 1              // 0 only for distributed
)(
    input  wire                     clk,

    input  wire                     wr_en,
    input  wire [$clog2(DEPTH)-1:0] wr_addr,
    input  wire [W-1:0]             wr_data,

    input  wire                     rd_en,
    input  wire [$clog2(DEPTH)-1:0] rd_addr,
    output wire [W-1:0]             rd_data
);
    localparam integer AW = $clog2(DEPTH);

    xpm_memory_sdpram #(
        .ADDR_WIDTH_A(AW),
        .ADDR_WIDTH_B(AW),
        .WRITE_DATA_WIDTH_A(W),
        .READ_DATA_WIDTH_B(W),
        .BYTE_WRITE_WIDTH_A(W),
        .MEMORY_SIZE(W * DEPTH),
        .MEMORY_PRIMITIVE(MEM),
        .CLOCKING_MODE("common_clock"),
        .READ_LATENCY_B(READ_LAT),
        .WRITE_MODE_B("read_first"),
        .MEMORY_INIT_FILE("none"),
        .USE_MEM_INIT(0),
        .ECC_MODE("no_ecc"),
        .AUTO_SLEEP_TIME(0),
        .CASCADE_HEIGHT(0),
        .SIM_ASSERT_CHK(0),
        .WAKEUP_TIME("disable_sleep")
    ) u_ram (
        .clka(clk),
        .ena(wr_en),
        .wea(1'b1),
        .addra(wr_addr),
        .dina(wr_data),

        .clkb(clk),
        .enb(rd_en),
        .addrb(rd_addr),
        .doutb(rd_data),
        .rstb(1'b0),
        .regceb(1'b1),

        .injectsbiterra(1'b0), .injectdbiterra(1'b0),
        .sbiterrb(), .dbiterrb(),
        .sleep(1'b0)
    );

endmodule

`default_nettype wire

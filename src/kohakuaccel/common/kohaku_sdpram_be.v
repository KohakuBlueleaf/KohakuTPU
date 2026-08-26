// Simple dual-port RAM with BYTE write enables: kohaku_sdpram's shape, plus a
// strobe per byte on the write port. Same explicit-primitive rule.
//
// A separate module rather than a parameter on kohaku_sdpram, so its 80-odd
// instantiations keep their whole-word port and none of them acquires an
// input nobody drives. The one caller is the staging store, whose port B takes
// AXI beats: a 64-bit store from the processor is one lane of a 256-bit word,
// and without strobes the other three lanes took the same value -- which is a
// page table with three of every four entries overwritten.

`default_nettype none

module kohaku_sdpram_be #(
    parameter integer WIDTH    = 256,
    parameter integer DEPTH    = 512,
    parameter         MEM_PRIM = "block",       // "block"|"ultra"
    parameter integer READ_LAT = 1
)(
    input  wire                     clk,

    input  wire                     wr_en,
    input  wire [$clog2(DEPTH)-1:0] wr_addr,
    input  wire [WIDTH-1:0]         wr_data,
    input  wire [WIDTH/8-1:0]       wr_strb,

    input  wire                     rd_en,
    input  wire [$clog2(DEPTH)-1:0] rd_addr,
    output wire [WIDTH-1:0]         rd_data
);
    localparam integer AW = $clog2(DEPTH);

    xpm_memory_sdpram #(
        .ADDR_WIDTH_A(AW),
        .ADDR_WIDTH_B(AW),
        .WRITE_DATA_WIDTH_A(WIDTH),
        .READ_DATA_WIDTH_B(WIDTH),
        .BYTE_WRITE_WIDTH_A(8),
        .MEMORY_SIZE(WIDTH * DEPTH),         // BITS, not words
        .MEMORY_PRIMITIVE(MEM_PRIM),
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
        .wea(wr_strb),
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

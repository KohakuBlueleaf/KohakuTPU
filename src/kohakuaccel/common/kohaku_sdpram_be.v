// Simple dual-port RAM with write lanes: kohaku_sdpram's shape, plus a strobe
// per BYTE_W-bit lane on the write port. Same explicit-primitive rule.
//
// A separate module rather than a parameter on kohaku_sdpram, so its 80-odd
// instantiations keep their whole-word port and none of them acquires an
// input nobody drives. The staging store's port B takes AXI beats: a 64-bit
// store from the processor is one lane of a 256-bit word, and without strobes
// the other three lanes took the same value -- a page table with three of
// every four entries overwritten. The Xache array lands one sub-word of a wide
// row the same way, with no line buffer and no read-modify-write.
//
// BYTE_W per UG974: 8 (WIDTH a multiple of 8), 9 (a multiple of 9) -- RAMB36
// and URAM288 carry both in silicon, 72 = 8 x 9 (UG573 p.115) -- or WIDTH,
// one strobe for the whole word.

`default_nettype none

module kohaku_sdpram_be #(
    parameter integer WIDTH    = 256,
    parameter integer DEPTH    = 512,
    parameter integer BYTE_W   = 8,             // 8 | 9 | WIDTH
    parameter         MEM_PRIM = "block",       // "distributed"|"block"|"ultra"
    parameter integer READ_LAT = 1,             // 0 only legal for distributed
    parameter integer CASCADE  = 0,             // xpm CASCADE_HEIGHT; 0 = the tool's (8 for URAM, UG901 p.117)
    parameter         WR_MODE  = "read_first",  // "read_first"|"no_change"|"write_first"
    parameter integer NSTRB    = WIDTH / BYTE_W
)(
    input  wire                     clk,

    input  wire                     wr_en,
    input  wire [NSTRB-1:0]         wr_strb,
    input  wire [$clog2(DEPTH)-1:0] wr_addr,
    input  wire [WIDTH-1:0]         wr_data,

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
        .BYTE_WRITE_WIDTH_A(BYTE_W),
        .MEMORY_SIZE(WIDTH * DEPTH),         // BITS, not words
        .MEMORY_PRIMITIVE(MEM_PRIM),
        .CLOCKING_MODE("common_clock"),
        .READ_LATENCY_B(READ_LAT),
        .WRITE_MODE_B(WR_MODE),
        .MEMORY_INIT_FILE("none"),
        .USE_MEM_INIT(0),                    // no init: URAM cannot be initialised
        .ECC_MODE("no_ecc"),
        .AUTO_SLEEP_TIME(0),
        .CASCADE_HEIGHT(CASCADE),
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

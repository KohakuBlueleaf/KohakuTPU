// Simple dual-port RAM: one write port, one read port, one clock.
//
// EXPLICIT, NOT INFERRED. The storage primitive is named by the caller and
// passed straight to xpm_memory_sdpram -- it is never left to synthesis to
// decide from the shape of a reg array.
//
// This matters more than it sounds. Whether an array becomes LUTRAM, BRAM or
// URAM otherwise depends on a reset clause, a read latency, or a heuristic that
// can change between tool versions -- so both the resource cost AND the read
// latency of the design can move without the RTL changing. Read latency is not
// a detail here: it sets the accumulator's read-after-write distance, which
// sets the bank count. That has to be a decision in the source.
//
//   MEM_PRIM   "distributed"   LUTRAM. Wide and shallow. READ_LAT 0 or 1.
//              "block"         RAMB36E2. 512 x 72 at its widest in SDP.
//              "ultra"         URAM288. 4096 x 72, fixed. Deep and narrow.
//
// Same convention as src/common/sync_fifo.v, which names FIFO_MEMORY_TYPE for
// the same reason. XPM simulates under xsim with -L xpm (the runners already
// pass it) and synthesises out-of-context without IP packaging, so there is no
// flow reason to fall back on inference.
//
// Choosing a primitive for a given shape:
//
//   width x depth        distributed      block           ultra
//   1024 x 96            ~1024 LUT        15 BRAM (2%)    15 URAM (0.3%)
//    352 x 512           ~2100 LUT        5 BRAM (98%)    5 URAM (12%)
//    352 x 4096          impractical      40 BRAM         5 URAM (98%)
//
// Wide-and-shallow wants distributed; the block/ultra ports are only 72 bits,
// so width is what forces the primitive count, and depth then comes free up to
// 512 (block) or 4096 (ultra).

`default_nettype none

module kohaku_sdpram #(
    parameter integer WIDTH    = 256,
    parameter integer DEPTH    = 512,
    parameter         MEM_PRIM = "block",       // "distributed"|"block"|"ultra"
    parameter integer READ_LAT = 1              // 0 only legal for distributed
)(
    input  wire                     clk,

    input  wire                     wr_en,
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
        .BYTE_WRITE_WIDTH_A(WIDTH),          // no byte enables; whole word
        .MEMORY_SIZE(WIDTH * DEPTH),         // BITS, not words
        .MEMORY_PRIMITIVE(MEM_PRIM),
        .CLOCKING_MODE("common_clock"),
        .READ_LATENCY_B(READ_LAT),
        .WRITE_MODE_B("read_first"),
        .MEMORY_INIT_FILE("none"),
        .USE_MEM_INIT(0),                    // no init: URAM cannot be initialised
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

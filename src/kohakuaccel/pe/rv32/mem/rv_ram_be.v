// rv_ram_be -- the PE's one memory primitive: a true dual-port RAM with byte
// enables on both ports.
//
// Every array in this PE that holds bytes is this module. Three of them exist:
// the data scratchpad (external L1 data), the internal L1's data array, and
// nothing else -- the instruction window is read-only to the core and uses the
// project's kohaku_sdpram instead.
//
// WHY 32 BITS AND NOT 256. A NoC flit carries 256 bits and a cache line is 256
// bits, so a 256-bit port looks natural. It is not: a RAMB36E2 in true-dual-port
// mode is 36 bits wide per port, so a 256-bit true-dual-port array is built from
// eight BRAMs whose 32-bit face is the only one the CPU ever uses, and every
// read of it needs an 8:1 32-bit mux -- ~64 LUT, on the load path, in a design
// whose objective is minimum LUT. Walking a line as eight 32-bit words instead
// costs 8 cycles per fill against a DRAM latency of hundreds, and costs zero
// LUT. docs/arch/pe/README.md s5.
//
// PRIMITIVE NAMED, NEVER INFERRED. See kohaku_sdpram's header: left to
// inference, both the resource and the READ LATENCY can move between tool
// versions, and read latency is pipeline structure here -- the address is
// presented in EX so that the data is out in MEM.

`default_nettype none

module rv_ram_be #(
    parameter integer WORDS    = 2048,          // 32-bit words
    parameter         MEM_PRIM = "block",       // "distributed" | "block" | "ultra"
    // Set to 1 only by a caller that guards its own answer: whether the
    // colliding read is bypassed (rv_spad) or discarded (rv_l1) is visible
    // there and not here. Otherwise the assertion below fires.
    parameter integer XPORT_OK = 0
)(
    input  wire                      clk,

    input  wire                      a_en,
    input  wire [3:0]                a_we,      // byte enables; 0 = read
    input  wire [$clog2(WORDS)-1:0]  a_addr,
    input  wire [31:0]               a_wdata,
    output wire [31:0]               a_rdata,

    input  wire                      b_en,
    input  wire [3:0]                b_we,
    input  wire [$clog2(WORDS)-1:0]  b_addr,
    input  wire [31:0]               b_wdata,
    output wire [31:0]               b_rdata
);
    localparam integer AW = $clog2(WORDS);

    // "no_change" on both ports: neither port ever reads the address it is
    // writing in the same cycle, and no_change is the mode that lets the tool
    // keep the output register quiet -- the read-during-write modes force a
    // pass-through path the load stage would have to wait on.
    xpm_memory_tdpram #(
        .ADDR_WIDTH_A(AW),          .ADDR_WIDTH_B(AW),
        .WRITE_DATA_WIDTH_A(32),    .WRITE_DATA_WIDTH_B(32),
        .READ_DATA_WIDTH_A(32),     .READ_DATA_WIDTH_B(32),
        .BYTE_WRITE_WIDTH_A(8),     .BYTE_WRITE_WIDTH_B(8),
        .MEMORY_SIZE(32 * WORDS),   // BITS, not words
        .MEMORY_PRIMITIVE(MEM_PRIM),
        .CLOCKING_MODE("common_clock"),
        .READ_LATENCY_A(1),         .READ_LATENCY_B(1),
        .WRITE_MODE_A("no_change"), .WRITE_MODE_B("no_change"),
        .MEMORY_INIT_FILE("none"),
        .USE_MEM_INIT(0),
        .ECC_MODE("no_ecc"),
        .AUTO_SLEEP_TIME(0),
        .CASCADE_HEIGHT(0),
        .SIM_ASSERT_CHK(0),
        .USE_EMBEDDED_CONSTRAINT(0),
        .WAKEUP_TIME("disable_sleep")
    ) u_ram (
        .clka(clk),   .rsta(1'b0),  .ena(a_en),  .regcea(1'b1),
        .wea(a_we),   .addra(a_addr), .dina(a_wdata), .douta(a_rdata),
        .clkb(clk),   .rstb(1'b0),  .enb(b_en),  .regceb(1'b1),
        .web(b_we),   .addrb(b_addr), .dinb(b_wdata), .doutb(b_rdata),
        .injectsbiterra(1'b0), .injectdbiterra(1'b0),
        .injectsbiterrb(1'b0), .injectdbiterrb(1'b0),
        .sbiterra(), .dbiterra(), .sbiterrb(), .dbiterrb(),
        .sleep(1'b0)
    );

`ifndef SYNTHESIS
    // A TRUE DUAL PORT COLLISION IS UNDEFINED IN SILICON, not just in the model:
    // one port writing the address the other reads gives invalid read data. The
    // scratchpad hit this on every doorbell and nothing noticed for a long time,
    // so the invariant is guarded here for good.
    always @(posedge clk) begin
        if ((XPORT_OK == 0) && a_en && (|a_we) && b_en && (a_addr == b_addr)) begin
            $display("%0t ERROR rv_ram_be: port A writes word %0d while port B reads it -- the read data is undefined",
                     $time, a_addr);
        end
    end
`endif

endmodule

`default_nettype wire

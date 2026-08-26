// rv64_ram_be -- a true dual-port array of 64-bit words with byte enables.
//
// The 64-bit counterpart of `rv_ram_be`. Same contract, same guard: a true dual
// port collision is undefined in SILICON, not just in the model, so one port
// writing the word the other reads is checked rather than assumed away. The RV32
// scratchpad hit exactly that on every doorbell and nothing noticed for a long
// time.
//
// `no_change` on both ports: neither port reads the address it is writing in the
// same cycle, and the read-during-write modes force a pass-through path the load
// stage would have to wait on.

`default_nettype none

module rv64_ram_be #(
    parameter integer WORDS    = 1024,          // 64-bit words
    parameter         MEM_PRIM = "block",       // "distributed" | "block" | "ultra"
    parameter integer XPORT_OK = 0
)(
    input  wire                      clk,

    input  wire                      a_en,
    input  wire [7:0]                a_we,      // byte enables; 0 = read
    input  wire [$clog2(WORDS)-1:0]  a_addr,
    input  wire [63:0]               a_wdata,
    output wire [63:0]               a_rdata,

    input  wire                      b_en,
    input  wire [7:0]                b_we,
    input  wire [$clog2(WORDS)-1:0]  b_addr,
    input  wire [63:0]               b_wdata,
    output wire [63:0]               b_rdata
);
    localparam integer AW = $clog2(WORDS);

    xpm_memory_tdpram #(
        .ADDR_WIDTH_A(AW),          .ADDR_WIDTH_B(AW),
        .WRITE_DATA_WIDTH_A(64),    .WRITE_DATA_WIDTH_B(64),
        .READ_DATA_WIDTH_A(64),     .READ_DATA_WIDTH_B(64),
        .BYTE_WRITE_WIDTH_A(8),     .BYTE_WRITE_WIDTH_B(8),
        .MEMORY_SIZE(64 * WORDS),   // BITS, not words
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
    always @(posedge clk) begin
        if ((XPORT_OK == 0) && a_en && (|a_we) && b_en && (a_addr == b_addr)) begin
            $display("%0t ERROR rv64_ram_be: port A writes word %0d while port B reads it -- the read data is undefined",
                     $time, a_addr);
        end
    end
`endif

endmodule

`default_nettype wire

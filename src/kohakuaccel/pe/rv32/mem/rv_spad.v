// rv_spad -- the data scratchpad: external L1 data.
//
// Real SRAM mapped into the global address space, NoC-writable, no tags. The
// address-region decode IS the lookup, so a scratchpad access always hits in
// one cycle and can never miss, evict, or need a coherence rule.
//
// TWO PORTS, TWO OWNERS, NO ARBITRATION. Port A belongs to the NoC receive path
// and port B to the core, and neither ever waits for the other. That is what
// lets a peer push land while this core is running, which is the whole basis of
// the push-and-doorbell protocol: the consumer polls a word in its own
// scratchpad with an ordinary load, at one cycle and zero NoC traffic.
//
// A byte enable on each port, because a peer's `sb` is as legal as a local one.

`default_nettype none

module rv_spad #(
    parameter integer WORDS    = 2048,
    parameter         MEM_PRIM = "block"
)(
    input  wire                     clk,

    input  wire                     a_en,
    input  wire [3:0]               a_we,
    input  wire [$clog2(WORDS)-1:0] a_addr,
    input  wire [31:0]              a_wdata,
    // Bench-only: nothing in the machine reads a window remotely, because
    // remote reads of a peer window do not exist. A component bench does need
    // to check what a push landed as, and the array's port is already there.
    output wire [31:0]              a_rdata,

    input  wire [$clog2(WORDS)-1:0] b_addr,
    input  wire [3:0]               b_we,
    input  wire [31:0]              b_wdata,
    output wire [31:0]              b_rdata
);
    wire [31:0] b_raw;

    rv_ram_be #(.WORDS(WORDS), .MEM_PRIM(MEM_PRIM), .XPORT_OK(1)) u_mem (
        .clk(clk),
        .a_en(a_en), .a_we(a_we), .a_addr(a_addr), .a_wdata(a_wdata),
        .a_rdata(a_rdata),
        .b_en(1'b1), .b_we(b_we), .b_addr(b_addr), .b_wdata(b_wdata),
        .b_rdata(b_raw)
    );

    // WRITE-THROUGH ACROSS THE PORTS, and it is not optional. A true dual port
    // collision returns UNDEFINED data in silicon, and on this array the
    // collision IS the doorbell: a peer pushes the word a poll loop is reading,
    // which is the common case rather than a corner. Without this the consumer
    // samples an indeterminate value, and a value that happened to match the
    // sentinel would release it early.
    //
    // Byte by byte, because a peer's `sb` is as legal as a local one.
    reg        byp;
    reg [3:0]  byp_we;
    reg [31:0] byp_d;
    always @(posedge clk) begin
        byp    <= a_en && (|a_we) && (a_addr == b_addr);
        byp_we <= a_we;
        byp_d  <= a_wdata;
    end

    genvar g;
    generate
    for (g = 0; g < 4; g = g + 1) begin : g_byp
        assign b_rdata[g*8 +: 8] = (byp && byp_we[g]) ? byp_d[g*8 +: 8]
                                                      : b_raw[g*8 +: 8];
    end
    endgenerate

endmodule

`default_nettype wire

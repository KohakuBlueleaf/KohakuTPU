// rv_imem -- the instruction window: external L1 program text.
//
// It is the HOME of its addresses, not a cache of anything. Nothing tags it and
// nothing fills it: the NoC writes a program image into it and the core fetches
// from it, so no coherence case exists by construction (design note s16.8).
//
// LOADING IT IS NOT SPECIAL. A program image arrives as an ordinary CU_DATA
// burst addressed at this PE, which is the same write path every unit in the
// framework already has. Boot, argument passing and inter-core messages are one
// mechanism, not three.
//
// The data side of the core cannot reach this window: the address decoder in
// rv_mem faults on 0x0xxx_xxxx. That keeps the fetch port exclusive, so fetch
// never contends with a load, and it makes self-modifying code a fault rather
// than a race.

`default_nettype none

module rv_imem #(
    parameter integer WORDS    = 2048,
    parameter         MEM_PRIM = "block"
)(
    input  wire                     clk,

    // NoC-side writes, one 32-bit word at a time
    input  wire                     wr_en,
    input  wire [$clog2(WORDS)-1:0] wr_addr,
    input  wire [31:0]              wr_data,

    // fetch
    input  wire [$clog2(WORDS)-1:0] rd_addr,
    output wire [31:0]              rd_data
);
    kohaku_sdpram #(.WIDTH(32), .DEPTH(WORDS), .MEM_PRIM(MEM_PRIM), .READ_LAT(1))
    u_mem (
        .clk(clk),
        .wr_en(wr_en), .wr_addr(wr_addr), .wr_data(wr_data),
        .rd_en(1'b1), .rd_addr(rd_addr), .rd_data(rd_data)
    );

endmodule

`default_nettype wire

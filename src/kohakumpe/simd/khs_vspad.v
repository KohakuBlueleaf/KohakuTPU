// khs_vspad -- the vector scratchpad: the SIMD PE's wide external L1.
//
// The machine's word is 256 bits -- one flit, one MAG entry, one cache line --
// and this array is the only place in the PE that is that wide on its own face.
// It is built as SIMD BANKS of 32 bits rather than one wide array because a
// RAMB36E2 is 36 bits per port in true-dual-port mode, so a wide face is
// several tiles either way; banking makes that explicit and lets the NoC write
// one 32-bit word without a read-modify-write.
//
//   bank b holds every word whose index mod SIMD is b, at row index/SIMD
//   port A   the NoC window writer ALONE: one 32-bit word, byte enabled, one
//            bank. Nothing the core does reaches this port, which is what keeps
//            the receive FIFO's state out of the core's stall network.
//   port B   the CORE: `vld`, `vst`, and the scalar `sw` that stages data --
//            one row, per-bank byte enables, read or write
//
// EVERY TILE IS FULLY DEPTH-UTILISED. ENTRIES is 1024 by default because that
// is a RAMB36E2's natural depth at the 1K x 36 aspect a 32-bit port selects, so
// the tile count is exactly SIMD and scales with the datapath rather than with
// a capacity guess.
//
// IT DOES NOT BYPASS A CROSS-PORT COLLISION, and that is the opposite answer to
// rv_spad's -- deliberately, because the case is the opposite. On the scalar
// scratchpad the collision IS the doorbell: a peer pushes exactly the word a
// poll loop is reading, every time, so that array carries a byte-wise
// write-through and pays 38 LUT for it. Here the doorbell still lives in the
// scalar scratchpad and this array carries bulk data, which push-and-doorbell
// orders: the payload is written BEFORE the doorbell the consumer is waiting
// on, so a program that reads a row while the NoC writes it has violated the
// protocol rather than used it. XPORT_OK(0) leaves rv_ram_be's permanent
// assertion in place to say so, at zero LUT.

`default_nettype none

module khs_vspad #(
    parameter integer SIMD     = 8,             // 32-bit lanes; VW = 32*SIMD
    parameter integer ENTRIES  = 1024,          // rows, one per tile depth
    parameter         MEM_PRIM = "block"
)(
    input  wire                          clk,

    // NoC window writes: a flat 32-bit word index into the whole array.
    input  wire                          a_en,
    input  wire [3:0]                    a_we,
    input  wire [$clog2(ENTRIES*SIMD)-1:0] a_word,
    input  wire [31:0]                   a_wdata,

    // The vector unit: one row, every bank. `b_en` is a REAL signal and not a
    // constant 1, which is what makes the collision assertion below mean
    // something: with the port always enabled it reads whatever row the EX
    // adder happened to produce for a scalar instruction, so any NoC write
    // could collide with a read nobody wanted and the assertion would fire
    // continuously in a working machine.
    //
    // `b_we` is PER BANK, four bits each, because this port carries the scalar
    // core's `sw` into the window as well as `vst`: a 32-bit store enables the
    // bytes of one bank and a `vst` enables all of them.
    input  wire                          b_en,
    input  wire [$clog2(ENTRIES)-1:0]    b_row,
    input  wire [4*SIMD-1:0]             b_we,
    input  wire [32*SIMD-1:0]            b_wdata,
    output wire [32*SIMD-1:0]            b_rdata
);
    localparam integer VW  = 32 * SIMD;
    localparam integer RAW = $clog2(ENTRIES);
    localparam integer BW  = (SIMD > 1) ? $clog2(SIMD) : 1;

    wire [BW-1:0]  a_bank = (SIMD > 1) ? a_word[BW-1:0] : {BW{1'b0}};
    wire [RAW-1:0] a_row  = a_word[RAW+BW-1 -: RAW];

    genvar b;
    generate
    for (b = 0; b < SIMD; b = b + 1) begin : g_bank
        wire sel = (SIMD == 1) || (a_bank == b[BW-1:0]);
        rv_ram_be #(.WORDS(ENTRIES), .MEM_PRIM(MEM_PRIM), .XPORT_OK(0)) u_mem (
            .clk(clk),
            .a_en(a_en && sel),
            .a_we((a_en && sel) ? a_we : 4'd0),
            .a_addr(a_row),
            .a_wdata(a_wdata),
            .a_rdata(),
            .b_en(b_en),
            .b_we(b_en ? b_we[4*b +: 4] : 4'd0),
            .b_addr(b_row),
            .b_wdata(b_wdata[32*b +: 32]),
            .b_rdata(b_rdata[32*b +: 32])
        );
    end
    endgenerate

endmodule

`default_nettype wire

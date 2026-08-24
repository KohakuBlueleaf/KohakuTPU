// kht_vregfile -- the PER-THREAD register file: 32 registers x WAVES waves,
// each holding LANES 32-bit values. Two read ports (three at RD3), one write
// port, latency 1.
//
// The address is {wave_id, register_index}, so the decode is SHARED across
// lanes and only the storage is replicated -- widening the machine adds banks,
// never address logic.
//
// WHY THE WAVE COUNT MAY BE NEARLY FREE, and why WAVES is a parameter from the
// first gate rather than added at the second. A RAMB18E2 in simple-dual-port is
// 512 x 36, so 32 registers x 16 waves = 512 entries fills one exactly at 32 of
// 36 useful bits. Up to 16 waves the marginal BRAM cost of a wave is therefore
// ZERO, and the count buys latency tolerance rather than storage. That is the
// same "an array that earns a tile fills the tile" rule the instruction window,
// the scratchpad and the L1 are all sized by.
//
// 2R1W is TWO MIRRORED 1W1R ARRAYS written in lockstep, because no primitive
// offers two independent read ports -- the construction both the scalar and the
// DSP register files use.
//
// NO WRITE-THROUGH BYPASS. At LANES x 32 bits a bypass mux is the widest path
// in the unit; the hazard is handled a stage earlier by a stall, which is the
// vector file's verdict rather than the scalar file's, and for the same reason.

`default_nettype none

module kht_vregfile #(
    parameter integer LANES = 8,
    parameter integer WAVES = 16,
    parameter         PRIM  = "block",
    // A THIRD READ PORT COSTS A THIRD MIRROR, so it is a parameter and not a
    // fixture: only `vfma` needs it (vd = vs1*vs2 + vd is three reads against
    // two ports), and an integer-only build must not pay LANES block RAMs for
    // an instruction it does not have.
    parameter integer RD3   = 0
)(
    input  wire                          clk,

    // Held low, the array keeps its last value -- which is what re-reading a
    // held address produced, and it puts the stall on a clock-enable arc
    // instead of in front of the address.
    input  wire                          ra_en,
    input  wire [$clog2(WAVES*32)-1:0]   ra1,
    input  wire [$clog2(WAVES*32)-1:0]   ra2,
    input  wire [$clog2(WAVES*32)-1:0]   ra3,
    output wire [32*LANES-1:0]           rd1,
    output wire [32*LANES-1:0]           rd2,
    output wire [32*LANES-1:0]           rd3,

    input  wire                          we,
    input  wire [$clog2(WAVES*32)-1:0]   wa,
    // Per LANE, so a masked-off lane keeps its previous value without the
    // write path needing a read-modify-write.
    input  wire [LANES-1:0]              wlane,
    input  wire [32*LANES-1:0]           wd
);
    localparam integer VW    = 32 * LANES;
    localparam integer DEPTH = WAVES * 32;

    genvar b;
    generate
    for (b = 0; b < LANES; b = b + 1) begin : g_bank
        kohaku_sdpram #(.WIDTH(32), .DEPTH(DEPTH), .MEM_PRIM(PRIM),
                        .READ_LAT(1)) u_p1 (
            .clk(clk),
            .wr_en(we && wlane[b]), .wr_addr(wa), .wr_data(wd[32*b +: 32]),
            .rd_en(ra_en), .rd_addr(ra1), .rd_data(rd1[32*b +: 32])
        );
        kohaku_sdpram #(.WIDTH(32), .DEPTH(DEPTH), .MEM_PRIM(PRIM),
                        .READ_LAT(1)) u_p2 (
            .clk(clk),
            .wr_en(we && wlane[b]), .wr_addr(wa), .wr_data(wd[32*b +: 32]),
            .rd_en(ra_en), .rd_addr(ra2), .rd_data(rd2[32*b +: 32])
        );
        if (RD3 != 0) begin : g_p3
            kohaku_sdpram #(.WIDTH(32), .DEPTH(DEPTH), .MEM_PRIM(PRIM),
                            .READ_LAT(1)) u_p3 (
                .clk(clk),
                .wr_en(we && wlane[b]), .wr_addr(wa), .wr_data(wd[32*b +: 32]),
                .rd_en(ra_en), .rd_addr(ra3), .rd_data(rd3[32*b +: 32])
            );
        end else begin : g_nop3
            assign rd3[32*b +: 32] = 32'd0;
        end
    end
    endgenerate

endmodule

`default_nettype wire

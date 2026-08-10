// One lane's slice of the vector register file: 3 read ports, 1 write port.
//
// docs/compute/vector-core.md s8. The file is striped BY LANE -- lane i holds
// elements i, i+16, i+32 -- so a slice is entirely lane-local and 3R1W is three
// duplicated single-read arrays rather than a 48-port monolith. Writes go to all
// three copies; each answers one port.
//
//   16 vector registers x 8 elements per lane = 128 entries of 24 bit
//   address = {vreg[3:0], chunk[2:0]}
//
// READ_LAT = 1, so an address issued in cycle N presents data in cycle N+1.
// That number is load-bearing for the sequencer's issue pipeline, which is why
// the primitive is named rather than inferred -- see kohaku_sdpram.v.
//
// "block", not "distributed": as LUTRAM this cost 168 LUT per slice, 2,688
// across the lanes, and the core is LUT-bound with BRAM idle. 48 RAMB18 per
// core is 24 of an SLR's 672 tiles. URAM is worse -- 4096 x 72 is its smallest
// shape, so the same 48 ports would spend 15% of an SLR's URAM on 49 Kb.
//
// PORT A IS THE EXCEPTION AND STAYS LUTRAM. b and c feed ALU operands, which
// get a whole cycle. a also feeds `ls_rdata` -> the store converters, and a
// RAMB18's CLKARDCLK -> DO (~1.5 ns on -2L) in series with a 16-lane E8->FP16
// normalise measured 286.0 MHz -- below the 300 floor. Registering between the
// two would cost a cycle on every VST beat, on a machine that is drain-bound.

`default_nettype none

module vec_regfile #(
    parameter integer AW  = 7,
    parameter integer DW  = 24,
    parameter         PRIM = "distributed",
    // Port A alone, because it is the only one with a consumer that cannot
    // afford a block RAM's clock-to-out -- see the note above.
    parameter         PRIM_A = "distributed"
)(
    input  wire          clk,

    input  wire          wr_en,
    input  wire [AW-1:0] wr_addr,
    input  wire [DW-1:0] wr_data,

    input  wire [AW-1:0] ra_addr,
    input  wire [AW-1:0] rb_addr,
    input  wire [AW-1:0] rc_addr,
    output wire [DW-1:0] ra_data,
    output wire [DW-1:0] rb_data,
    output wire [DW-1:0] rc_data
);
    localparam integer DEPTH = (1 << AW);

    kohaku_sdpram #(.WIDTH(DW), .DEPTH(DEPTH), .MEM_PRIM(PRIM_A), .READ_LAT(1))
    u_a (.clk(clk), .wr_en(wr_en), .wr_addr(wr_addr), .wr_data(wr_data),
         .rd_en(1'b1), .rd_addr(ra_addr), .rd_data(ra_data));

    kohaku_sdpram #(.WIDTH(DW), .DEPTH(DEPTH), .MEM_PRIM(PRIM), .READ_LAT(1))
    u_b (.clk(clk), .wr_en(wr_en), .wr_addr(wr_addr), .wr_data(wr_data),
         .rd_en(1'b1), .rd_addr(rb_addr), .rd_data(rb_data));

    kohaku_sdpram #(.WIDTH(DW), .DEPTH(DEPTH), .MEM_PRIM(PRIM), .READ_LAT(1))
    u_c (.clk(clk), .wr_en(wr_en), .wr_addr(wr_addr), .wr_data(wr_data),
         .rd_en(1'b1), .rd_addr(rc_addr), .rd_data(rc_data));

endmodule

`default_nettype wire

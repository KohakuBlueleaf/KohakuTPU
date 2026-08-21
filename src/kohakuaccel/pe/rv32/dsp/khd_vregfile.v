// khd_vregfile -- the vector register file: VREGS x VW bits, two read ports,
// one write port, read latency 1.
//
// The same 2R1W construction as rv_regfile -- two mirrored 1W1R arrays written
// in lockstep, because no primitive offers two independent read ports -- and the
// same reason for the shape: read latency is PIPELINE STRUCTURE. The addresses
// are captured at the end of EX so the data is out in MEM, which is exactly
// where the scalar file's operands arrive relative to its own stages.
//
// WHY THE ENTRY COUNT MAY BE FREE, and why it is a parameter rather than a
// decision. A distributed-RAM primitive is 32 deep, so a file of 8 entries
// leaves 24 of every 32 unused while costing the same LUTs as 32 would. That is
// the mirror image of the base core's register-file lesson (a 32-entry file in
// a 1K-deep BRAM is 3.1% depth-utilised and the LUTRAM form ships instead), and
// it points the opposite way: here the SMALL file may be the wasteful one.
// docs/arch/pe/performance.md carries the measurement.
//
// NO WRITE-THROUGH BYPASS, deliberately, and it is the one place this file
// differs from rv_regfile. The scalar file needs one because a write lands at
// the same edge that captures a read four instructions behind it; here the
// hazard is handled a stage earlier by a scoreboard stall, because a bypass mux
// at VW bits is 256 LUT on the widest path in the unit against 32 for the
// scalar one. `khd_unit` owns that stall and asserts on the case this file
// cannot answer.

`default_nettype none

module khd_vregfile #(
    parameter integer VREGS = 8,
    parameter integer VW    = 256,
    parameter         PRIM  = "distributed"
)(
    input  wire                      clk,

    input  wire                      ra_en,
    input  wire [$clog2(VREGS)-1:0]  ra1,
    input  wire [$clog2(VREGS)-1:0]  ra2,
    output wire [VW-1:0]             rd1,
    output wire [VW-1:0]             rd2,

    input  wire                      we,
    input  wire [$clog2(VREGS)-1:0]  wa,
    input  wire [VW-1:0]             wd
);
    // A STALL HOLDS THE OUTPUT REGISTER, NOT THE ADDRESS, and the difference is
    // measurable at the PE. Holding the address puts the MEM stage's stall --
    // which carries a cache miss and a push handshake -- in front of the array,
    // so the whole of it lands on the read path: the assembled PE bound there
    // at 318.3 MHz. `rd_en` reaches the primitive's enable instead, which is a
    // clock-enable arc, and the address arrives straight from the instruction.
    // The two are equivalent: with the enable low the array holds its last
    // value, which is exactly what re-reading a held address produced.
    kohaku_sdpram #(.WIDTH(VW), .DEPTH(VREGS), .MEM_PRIM(PRIM), .READ_LAT(1))
    u_p1 (.clk(clk), .wr_en(we), .wr_addr(wa), .wr_data(wd),
          .rd_en(ra_en), .rd_addr(ra1), .rd_data(rd1));

    kohaku_sdpram #(.WIDTH(VW), .DEPTH(VREGS), .MEM_PRIM(PRIM), .READ_LAT(1))
    u_p2 (.clk(clk), .wr_en(we), .wr_addr(wa), .wr_data(wd),
          .rd_en(ra_en), .rd_addr(ra2), .rd_data(rd2));

endmodule

`default_nettype wire

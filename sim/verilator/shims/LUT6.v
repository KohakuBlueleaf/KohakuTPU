// A stand-in for the UNISIM LUT6, used only under Verilator.
//
// The first word of this comment is not "Verilator": that spelling is parsed as
// a verilator metacomment and fails the build. Vivado's own unisims are not on
// this flow's search path, and kohaku_mux names LUT6 directly because the
// mapper will not build a 4:1 in one LUT from a combinational select.
//
// The INIT bit an input pattern selects is the whole cell: an unknown input
// makes the index unknown and the output follows, which is what the real
// primitive does in simulation.

`default_nettype none

module LUT6 #(
    parameter [63:0] INIT = 64'h0000_0000_0000_0000
)(
    output wire O,
    input  wire I0,
    input  wire I1,
    input  wire I2,
    input  wire I3,
    input  wire I4,
    input  wire I5
);
    assign O = INIT[{I5, I4, I3, I2, I1, I0}];
endmodule

`default_nettype wire

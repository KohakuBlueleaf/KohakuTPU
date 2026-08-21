// Four PEs -- one MAG and one NoC is the ceiling for four, so this is the top
// of the level-4 sweep. See rv_mc2_tb.v for why the wrapper exists.

`default_nettype none
`timescale 1ns/1ps

module rv_mc4_tb;
    rv_mc_body #(.NPE(4)) u_body ();
endmodule

`default_nettype wire

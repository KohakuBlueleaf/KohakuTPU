// One PE running the SAME programs as the 2- and 4-core runs: the uncontended
// floor their cycle counts are read against. rv_mc2_tb.v says why this exists.

`default_nettype none
`timescale 1ns/1ps

module rv_mc1_tb;
    rv_mc_body #(.NPE(1)) u_body ();
endmodule

`default_nettype wire

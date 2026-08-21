// Two PEs. The bench is rv_mc_tb.v and the core count is its parameter; xelab
// needs a named top per configuration, which is all this file is.

`default_nettype none
`timescale 1ns/1ps

module rv_mc2_tb;
    rv_mc_body #(.NPE(2)) u_body ();
endmodule

`default_nettype wire

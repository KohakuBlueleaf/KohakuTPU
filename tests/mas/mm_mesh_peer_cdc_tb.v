// The cluster draining INTO the vector core on DIFFERENT rates: the peer burst
// leaves the matmul domain, crosses the NoC's, and enters the vector one.

`default_nettype none
`timescale 1ns/1ps

// Matmul 366.8 MHz, vector 214.6 MHz, mesh 250 MHz.
module mm_mesh_peer_cdc_tb;
    mm_mesh_peer_tb #(.UNIT_CDC(1), .MHP(1.363), .VHP(2.330)) u_tb ();
endmodule

`default_nettype wire

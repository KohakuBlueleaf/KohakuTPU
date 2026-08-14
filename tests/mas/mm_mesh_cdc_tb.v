// mm_mesh with ONE RATE PER COMPONENT TYPE: matmul, vector and the NoC are
// three separate asynchronous domains in one mesh.

// The three half periods share no ratio with each other, so no edge alignment
// can hide a missing synchroniser. Mesh 2.000, matmul 1.363, vector 2.330 ns.

`default_nettype none
`timescale 1ns/1ps

// Matmul 366.8 MHz, vector 214.6 MHz, mesh 250 MHz.
module mm_mesh_cdc_tb;
    mm_mesh_tb #(.UNIT_CDC(1), .MHP(1.363), .VHP(2.330)) u_tb ();
endmodule

// Both unit types BELOW the mesh rate -- the direction where the outbound
// crossing is the one that fills. Matmul 160.4 MHz, vector 137.2 MHz.
module mm_mesh_cdc_slow_tb;
    mm_mesh_tb #(.UNIT_CDC(1), .MHP(3.117), .VHP(3.645)) u_tb ();
endmodule

// Async unit clocks AND the converged L2 in one design: each was proven alone,
// and a store reached across a CDC is neither.
module mm_mesh_cdc_l2_tb;
    mm_mesh_tb #(.UNIT_CDC(1), .MHP(1.363), .VHP(2.330), .L2MAG(1)) u_tb ();
endmodule

`default_nettype wire

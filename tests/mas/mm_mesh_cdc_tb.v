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

// The shipping rates: fabric 250, matmul 300, vector 350.
module mm_mesh_vfast_tb;
    mm_mesh_tb #(.UNIT_CDC(1), .MHP(1.667), .VHP(1.429)) u_tb ();
endmodule

// MAG at 233 against the NoC's 250, so no ratio can align them. Recorded as
// failing until MAG's NoC port got the CDC every other endpoint already had.
module mm_mesh_magclk_tb;
    mm_mesh_tb #(.UNIT_CDC(1), .MAG_CDC(1), .GHP(2.146)) u_tb ();
endmodule

// All four component rates at once, each behind its own endpoint CDC: MAG 233,
// NoC 250, vector 300, matmul 500/250 off one divider.
module mm_mesh_5clk_tb;
    mm_mesh_tb #(.UNIT_CDC(1), .MAG_CDC(1), .PUMP(1),
                 .GHP(2.146), .M2HP(1.000), .VHP(1.667)) u_tb ();
endmodule

// The same with the converged MAG L2, so async endpoints and a store that
// crosses one are exercised in a single design rather than two.
module mm_mesh_5clk_l2_tb;
    mm_mesh_tb #(.UNIT_CDC(1), .MAG_CDC(1), .PUMP(1), .GHP(2.146),
                 .M2HP(1.000), .VHP(1.667), .L2MAG(1)) u_tb ();
endmodule

`default_nettype wire

// mag_dram_port at R=1, where packing is an identity. RLOG is 1 even there, so
// the phase, the alignment and the burst-length divide all have to be bypassed.

`default_nettype none

module mag_dram_port_r1_tb;
    mag_dram_port_tb #(.MW(256)) u_tb ();
endmodule

`default_nettype wire

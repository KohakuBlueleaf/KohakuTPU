# Block-level synthesis strategy: area-optimise the memory agent alone. The
# node's worst path is the RV64 core (wb_val_reg -> d_valid_reg, 12 levels at
# -0.016 ns), and a whole-node area directive costs it 1.3-2.1 ns; MAG has
# slack to spend. Read before synth_design (ooc_sysnode.tcl argv 3).
set_property BLOCK_SYNTH.STRATEGY {AREA_OPTIMIZED} [get_cells u_mag]

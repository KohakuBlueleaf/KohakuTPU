# multimesh v8t6 -- v8t5's shape, tiers and knobs exactly; the build carries
# the RTL of the routed-v8t5 fixes (results.md 5.10, 5.11): the staging
# dispatch registers per bank and off the reset's enable, the walkers'
# element queue, the DRAM port's head register, the mover's configuration
# register, the core's interrupt register, ASYNC_REG on the ring
# synchronisers, and the clock groups without the XDMA duplicate.

set here6 [file dirname [file normalize [info script]]]
source [file dirname $here6]/v8t5/00_config.tcl

set design_name multimesh_v8t6
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v8t6

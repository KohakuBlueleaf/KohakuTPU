# multimesh v9t6 -- v9t5 at 2+2 a die with the compute box replaced by
# placement clusters: ONE soft, non-exclusive system pblock per die (the node,
# its Xache partition and station, X4-X7 beside the MIG) and a USER_CLUSTER per
# local unit; routers and crossings (trunks, pipes, station links) unconstrained.

set here9t6 [file dirname [file normalize [info script]]]
source [file dirname $here9t6]/v9t5/00_config.tcl

set design_name multimesh_v9t6
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v9t6

# Fit, this set applied to the v9t5 placed netlist: pb_sn holds 42.6k-52.8k
# LUT of 215k and 129 URAM of 192 on every die.
set CMP_PLACE    cluster
set SN_CMP_COLS  {}
set SN_CMP_UNITS {0 {}  1 {}  2 {}  3 {}}
set UC_UNITS {
    u_cu0 u_cu1 u_vec0 u_vec1
    u_mag/u_pe/u_cpu u_mag/u_mag u_mag/u_pe/u_mover u_mag/u_pe/u_xform
    xache
}
set USER_CLUSTERS [list 0 $UC_UNITS 1 $UC_UNITS 2 $UC_UNITS 3 $UC_UNITS]

# Trunk rings back on the receiving die, as v9t4 (on card: every pair but the
# 2<->3 fold bug): one die crossing a cycle instead of the tx ring's read
# round trip (routed trunk TNS -56 vs -1,288 ns, same latency).
set TRUNK_RING_SIDE rx
set TRUNK_RING_PIN  0
# The interlink FIFOs the same way: LUTRAM on the die that reads it.
set IL_RAM_SIDE     rx

# Every kh_rst_sync output is a plain register (its sync stages alone carry
# ASYNC_REG), so place_design replicates it toward its loads.
lappend REPL_NETS {.*/u_rs_\w+/q_reg}

set REPORT_BASE v9t5

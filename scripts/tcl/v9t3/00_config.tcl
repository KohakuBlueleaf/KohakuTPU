# multimesh v9t3 -- v9t2's 4+2 dies, station 1 and XDMA, with the V2.1 vector
# core (VEC_CORE v21: 128 registers, two register-file read copies a bank, a
# LUTRAM scoreboard; vector-core-v2.md s10).

set here9t3 [file dirname [file normalize [info script]]]
source [file dirname $here9t3]/v9t2/00_config.tcl

set design_name multimesh_v9t3
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v9t3

set VEC_CORE    v21

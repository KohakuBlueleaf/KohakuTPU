# multimesh v9t2 -- v9 with station 1 off block RAM, XDMA x4/128 and the host
# interrupts. Same arguments as multimesh_v8t3_bd.tcl.
set here [file dirname [file normalize [info script]]]
set V8_CONFIG $here/v9t2/00_config.tcl
set V8_STAGES $here/v8t3
source $here/multimesh_v8t3_bd.tcl

# multimesh v9 -- four 1x2 meshes of 4 clusters + 2 V2 vector cores. Same
# arguments as multimesh_v8t3_bd.tcl.
set here [file dirname [file normalize [info script]]]
set V8_CONFIG $here/v9/00_config.tcl
set V8_STAGES $here/v8t3
source $here/multimesh_v8t3_bd.tcl

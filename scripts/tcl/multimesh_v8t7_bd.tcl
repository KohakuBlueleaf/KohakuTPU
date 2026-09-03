# multimesh v8t7 -- v8t6's RTL and tiers, placed: three pipe stages a half and
# a compute-half pblock per die. Same arguments as multimesh_v8t3_bd.tcl.
set here [file dirname [file normalize [info script]]]
set V8_CONFIG $here/v8t7/00_config.tcl
set V8_STAGES $here/v8t3
source $here/multimesh_v8t3_bd.tcl

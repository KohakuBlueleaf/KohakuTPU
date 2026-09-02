# multimesh v8t4 -- v8t3's stages, v8t4's config (a sysnode and a bus clock per
# die). Same arguments as multimesh_v8t3_bd.tcl:
#   vivado -mode batch -source scripts/tcl/multimesh_v8t4_bd.tcl \
#     ?-tclargs rebuild|synth ?jobs N? ?analyze??
set here [file dirname [file normalize [info script]]]
set V8_CONFIG $here/v8t4/00_config.tcl
set V8_STAGES $here/v8t3
source $here/multimesh_v8t3_bd.tcl

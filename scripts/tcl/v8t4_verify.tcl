# v8t4 block-design verify: every clock, reset and interface against the config.
#   vivado -mode batch -source scripts/tcl/v8t4_verify.tcl
set here [file dirname [file normalize [info script]]]
source $here/v8t4/00_config.tcl
source $here/v8t3/75_verify_bd.tcl

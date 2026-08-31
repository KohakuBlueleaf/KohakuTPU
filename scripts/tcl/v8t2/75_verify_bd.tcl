# v8t's verifier against the v8t2 project: load this config, then the stage.
#   vivado -mode batch -source scripts/tcl/v8t2/75_verify_bd.tcl
set here [file dirname [file normalize [info script]]]
source $here/00_config.tcl
source $here/../v8t/75_verify_bd.tcl

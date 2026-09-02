# v8t4 congestion attribution on the routed run.
#   vivado -mode batch -source scripts/tcl/v8t4_congestion.tcl
set here [file dirname [file normalize [info script]]]
source $here/v8t4/00_config.tcl
source $here/v8t3/86_congestion.tcl

# v8t4 synthesis analysis, read-only, beside the implementation session.
#   vivado -mode batch -source scripts/tcl/v8t4_analyze.tcl
set here [file dirname [file normalize [info script]]]
set V8_CONFIG $here/v8t4/00_config.tcl
set V8_STAGES $here/v8t3
source $here/v8t3_analyze.tcl

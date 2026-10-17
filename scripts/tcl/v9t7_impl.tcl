# v9t7 implementation and post-route report, then the per-item report
# against v9t6.
set here [file dirname [file normalize [info script]]]
set V8_CONFIG $here/v9t7/00_config.tcl
set V8_STAGES $here/v8t3
source $here/v8t3_impl.tcl
source $here/v9t7/report_items.tcl

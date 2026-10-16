# v9t5 implementation (its IMPL_DIRS variant) and post-route
# report, then the per-item report against v9t4.
set here [file dirname [file normalize [info script]]]
set V8_CONFIG $here/v9t5/00_config.tcl
set V8_STAGES $here/v8t3
source $here/v8t3_impl.tcl
source $here/v9t5/report_items.tcl

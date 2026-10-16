# v9t6 implementation (its IMPL_DIRS variant) and post-route
# report, then the per-item report against v9t5.
set here [file dirname [file normalize [info script]]]
set V8_CONFIG $here/v9t6/00_config.tcl
set V8_STAGES $here/v8t3
source $here/v8t3_impl.tcl
source $here/v9t6/report_items.tcl

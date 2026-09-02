# The synthesis path census (v8t3/87_synth_paths.tcl) on a READ-ONLY open of
# the project, so it runs beside the implementation session.
#   vivado -mode batch -source scripts/tcl/v8t3_paths.tcl ?-tclargs margin 1.0?
set here [file dirname [file normalize [info script]]]
if {![info exists V8_CONFIG]} { set V8_CONFIG $here/v8t3/00_config.tcl }
if {![info exists V8_STAGES]} { set V8_STAGES $here/v8t3 }
source $V8_CONFIG
open_project -read_only $proj_dir/${design_name}.xpr
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 is [get_property PROGRESS [get_runs synth_1]], not complete"
}
source $V8_STAGES/87_synth_paths.tcl

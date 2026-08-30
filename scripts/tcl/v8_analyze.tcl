# v8: re-run the post-synthesis analysis on a completed synth_1 without
# resynthesising -- after a floorplan or clock-group edit, 60_constraints is
# re-sourced (it rewrites the two generated XDCs in place) and then the checks.
#   vivado -mode batch -source scripts/tcl/v8_analyze.tcl
set here [file dirname [file normalize $argv0]]
if {![file exists $here/v8/00_config.tcl]} { set here [file dirname [file normalize [info script]]] }
source $here/v8/00_config.tcl
open_project $proj_dir/${design_name}.xpr
if {[get_property top [current_fileset]] ne "${design_name}_wrapper"} {
    error "top is [get_property top [current_fileset]], not the BD wrapper"
}
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 is [get_property PROGRESS [get_runs synth_1]], not complete"
}
source $here/v8/60_constraints.tcl
source $here/v8/70_analyze.tcl

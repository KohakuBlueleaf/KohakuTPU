# v8: impl through write_bitstream from a completed, analysed synthesis, then
# the report. Bypasses the multimesh_v8_bd.tcl preamble (its mref-cache
# deletion forces a repackage on open).
#   vivado -mode batch -source scripts/tcl/v8_impl.tcl ?-tclargs route?
set here [file dirname [file normalize [info script]]]
source $here/v8/00_config.tcl
set impl_step [expr {[lsearch $argv route] >= 0 ? "route_design" : "write_bitstream"}]

open_project $proj_dir/${design_name}.xpr
if {[get_property top [current_fileset]] ne "${design_name}_wrapper"} {
    error "top is [get_property top [current_fileset]], not the BD wrapper"
}
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 is [get_property PROGRESS [get_runs synth_1]], not complete"
}
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    launch_runs impl_1 -to_step $impl_step -jobs 4
    wait_on_run impl_1
}
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    error "impl failed: see ${design_name}.runs/impl_1/runme.log"
}
puts "@@@ v8 impl done ($impl_step)"
source $here/v8/80_report.tcl

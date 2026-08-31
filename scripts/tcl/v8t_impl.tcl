# v8t: impl through write_bitstream from a completed, analysed synthesis, then
# the report. Bypasses the multimesh_v8t_bd.tcl preamble (its mref-cache
# deletion forces a repackage on open).
#   vivado -mode batch -source scripts/tcl/v8t_impl.tcl ?-tclargs route?
set here [file dirname [file normalize [info script]]]
source $here/v8t/00_config.tcl
set impl_step [expr {[lsearch $argv route] >= 0 ? "route_design" : "write_bitstream"}]

open_project $proj_dir/${design_name}.xpr
if {[get_property top [current_fileset]] ne "${design_name}_wrapper"} {
    error "top is [get_property top [current_fileset]], not the BD wrapper"
}
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 is [get_property PROGRESS [get_runs synth_1]], not complete"
}
set ir [get_runs impl_1]
if {[get_property PROGRESS $ir] != "100%"} {
    # a run whose launcher died stays "Running" and refuses a relaunch
    if {![string match "Not started" [get_property STATUS $ir]]} { reset_run impl_1 }
    puts "@@@ v8t impl_1: launched to $impl_step at [clock format [clock seconds] -format %H:%M:%S]"
    launch_runs impl_1 -to_step $impl_step -jobs 8
    wait_on_run impl_1
}
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    error "impl failed: see ${design_name}.runs/impl_1/runme.log"
}
puts "@@@ v8t impl done ($impl_step) at [clock format [clock seconds] -format %H:%M:%S]"
source $here/v8t/80_report.tcl

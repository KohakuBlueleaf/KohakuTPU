# Re-route a run that died IN the router, reusing its placement.
#   vivado -mode batch -source mesh_probe_resume.tcl -tclargs <tag> ?run?
#
# A bare `reset_run` deletes the placement, so recovery must name the step.
# `open_checkpoint` on the physopt DCP comes up with ZERO clocks.

set tag [lindex $argv 0]
set run [lindex $argv 1]
if {$run eq ""} { set run impl_1 }
set design_name mp_$tag

set_param general.maxThreads 16
open_project ${design_name}.xpr

set prog [get_property PROGRESS [get_runs $run]]
puts "@@@ probe $tag resume $run at $prog"

# Placement is what we are protecting; without it there is nothing to resume
# and a full run is the honest answer, not a silent restart from scratch.
set dir [get_property DIRECTORY [get_runs $run]]
if {![file exists $dir/${design_name}_wrapper_physopt.dcp] &&
    ![file exists $dir/${design_name}_wrapper_placed.dcp]} {
    error "no placed/physopt checkpoint in $dir -- resume cannot help, run impl"
}

if {[catch {reset_run $run -from_step route_design} msg]} {
    error "reset_run -from_step route_design failed: $msg"
}
launch_runs $run -to_step route_design -jobs 4
wait_on_run $run
set prog [get_property PROGRESS [get_runs $run]]
puts "@@@ probe $tag resume $run done $prog"
if {$prog != "100%"} { error "resume failed: $dir/runme.log" }

set census [file dirname [info script]]/path_census.tcl
if {[file exists $census]} {
    source $census
    open_run $run -name routed_${tag}_$run
    path_census $dir/census
    close_design
}
puts "@@@ probe $tag done"

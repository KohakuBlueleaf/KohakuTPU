# Synth+impl for a project from mesh_probe_bd.tcl, run in the probe's dir:
#   vivado -mode batch -source mesh_probe_impl.tcl -tclargs <tag>

# Separate because launch_runs -jobs spawns OOC children, and those alone
# make a concurrent create_bd_design fail reading the install tree.

set tag [lindex $argv 0]
set design_name mp_$tag
# Optional. A strategy sweep otherwise costs a BD rebuild and a fresh synthesis
# per point; set here, it reuses the one already in the project.
set strat [lindex $argv 1]

set_param general.maxThreads 16

open_project ${design_name}.xpr

if {[get_property top [current_fileset]] ne "${design_name}_wrapper"} {
    error "top is [get_property top [current_fileset]], not the BD wrapper"
}

# A STRATEGY GETS ITS OWN RUN: setting one on impl_1 forces a reset_run, which
# DELETES the 3-7 hour baseline it is meant to be compared against.
set impl impl_1
if {$strat ne "" && $strat ne "none"} {
    set impl impl_$strat
    if {[llength [get_runs -quiet $impl]] == 0} {
        create_run $impl -parent_run synth_1 \
                   -flow [get_property FLOW [get_runs impl_1]]
    }
    set_property strategy $strat [get_runs $impl]
    puts "@@@ probe $tag impl run $impl strategy $strat"
}

# Idempotent retry: a finished synthesis is reused, never relaunched -- Vivado
# refuses to launch a completed run, and redoing it costs an hour.
if {[get_property PROGRESS [get_runs $impl]] != "100%"} { reset_run $impl }
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    reset_run synth_1
    launch_runs synth_1 -jobs 4
    wait_on_run synth_1
    if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
        error "synth failed: [get_property DIRECTORY [get_runs synth_1]]/runme.log"
    }
}
launch_runs $impl -to_step route_design -jobs 4
wait_on_run $impl
puts "@@@ probe $tag impl [get_property PROGRESS [get_runs $impl]]"

# Free in slot terms: launch_runs' child has exited, so opening the routed
# design here reuses the memory it just released instead of adding a 7th run.
set census [file dirname [info script]]/path_census.tcl
if {[file exists $census] && [get_property PROGRESS [get_runs $impl]] == "100%"} {
    source $census
    open_run $impl -name routed_${tag}_$impl
    path_census [get_property DIRECTORY [get_runs $impl]]/census
    close_design
}
puts "@@@ probe $tag done"

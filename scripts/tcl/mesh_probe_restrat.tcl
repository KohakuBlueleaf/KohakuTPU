# Re-implement an existing probe under a different strategy, reusing its synth.
# tclargs: <tag> <strat> <run> [<post>] [<place>] [<physopt>] [<route>]

# The project holds the RTL snapshot import_files took at BD time, so this is
# the only way to build the ORIGINAL netlist once the RTL has moved on.

set tag   [lindex $argv 0]
set strat [lindex $argv 1]
set run   [lindex $argv 2]
if {$run eq ""} { set run impl_strat }

set_param general.maxThreads 16
open_project mp_$tag.xpr

if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 is not complete; this script never re-synthesises"
}
if {[llength [get_runs -quiet $run]]} { reset_run $run ; delete_run $run }
create_run $run -parent_run synth_1 -flow [get_property FLOW [get_runs impl_1]] \
                -strategy $strat

# Per-step directives, so a sweep can move ONE knob. A named strategy bundles
# place, phys_opt and route together and cannot say which of them paid.
foreach {idx step} {4 PLACE_DESIGN 5 PHYS_OPT_DESIGN 6 ROUTE_DESIGN} {
    set d [lindex $argv $idx]
    if {$d eq "" || $d eq "-"} { continue }
    set_property STEPS.${step}.ARGS.DIRECTIVE $d [get_runs $run]
    puts "@@@ probe $tag $run $step directive $d"
}

# Every probe so far stopped at route_design, discarding the one step meant to
# recover routed slack -- and routed WNS is the metric we judge on.
set post [lindex $argv 3]
if {$post ne "" && $post ne "-"} {
    set_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED true [get_runs $run]
    set_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.ARGS.DIRECTIVE $post [get_runs $run]
    puts "@@@ probe $tag $run post-route phys_opt $post"
    # Vivado names this step with a space and parentheses, not an identifier.
    launch_runs $run -to_step {phys_opt_design (Post-Route)} -jobs 4
} else {
    launch_runs $run -to_step route_design -jobs 4
}
wait_on_run $run

# Same census the impl flow writes, so a swept run is comparable to a fresh one.
set census [file dirname [info script]]/path_census.tcl
if {[file exists $census] && [get_property PROGRESS [get_runs $run]] == "100%"} {
    source $census
    open_run $run -name routed_$run
    path_census [get_property DIRECTORY [get_runs $run]]/census
    close_design
}
puts "@@@ probe $tag $run [get_property PROGRESS [get_runs $run]] strategy $strat"
puts "@@@ probe $tag done"

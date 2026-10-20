# v8t3: impl through write_bitstream from a completed, analysed synthesis, then
# the report. Bypasses the multimesh_v8t3_bd.tcl preamble (its mref-cache
# deletion forces a repackage on open).
#   vivado -mode batch -source scripts/tcl/v8t3_impl.tcl ?-tclargs route?
# impl_1 takes the config's IMPL_DIRS variant only before a launch, and a run
# property is set only where it differs, so a finished run is never made
# stale. A run failed in a step after opt_design resumes there.
set here [file dirname [file normalize [info script]]]
if {![info exists V8_CONFIG]} { set V8_CONFIG $here/v8t3/00_config.tcl }
if {![info exists V8_STAGES]} { set V8_STAGES $here/v8t3 }
source $V8_CONFIG
set impl_step [expr {[lsearch $argv route] >= 0 ? "route_design" : "write_bitstream"}]

proc v8_impl_set {run prop val} {
    if {[get_property $prop $run] ne $val} { set_property $prop $val $run }
}
# The variant's directives and post-route phys_opt; every step's Tcl hook cleared.
proc v8_impl_setup {run} {
    global IMPL_DIRS
    set d [v8_impl_dirs]
    foreach {key step} {opt OPT_DESIGN place PLACE_DESIGN phys_opt PHYS_OPT_DESIGN
                        route ROUTE_DESIGN} {
        v8_impl_set $run STEPS.${step}.ARGS.DIRECTIVE [dict get $d $key]
    }
    if {[dict get $d phys_opt] ne "Default"} {
        if {![string is true [get_property STEPS.PHYS_OPT_DESIGN.IS_ENABLED $run]]} {
            set_property STEPS.PHYS_OPT_DESIGN.IS_ENABLED true $run
        }
    }
    set post [dict get $d post_route]
    set on   [get_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED $run]
    if {[string is true $on] != ($post ne "")} {
        set_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED [expr {$post ne ""}] $run
    }
    if {$post ne ""} {
        v8_impl_set $run STEPS.POST_ROUTE_PHYS_OPT_DESIGN.ARGS.DIRECTIVE $post
    }
    # No step runs a Tcl hook: everything the run needs is in its constraints.
    foreach p [list_property $run -regexp {^STEPS\..*\.TCL\.(PRE|POST)$}] {
        v8_impl_set $run $p {}
    }
    puts "@@@ impl_1 IMPL_DIRS $IMPL_DIRS: $d"
}

open_project $proj_dir/${design_name}.xpr
if {[get_property top [current_fileset]] ne "${design_name}_wrapper"} {
    error "top is [get_property top [current_fileset]], not the BD wrapper"
}
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 is [get_property PROGRESS [get_runs synth_1]], not complete"
}
set ir [get_runs impl_1]
if {[get_property PROGRESS $ir] != "100%"} {
    # A run that failed in a step resumes from that step's input checkpoint; a
    # run cut off mid-step (the machine went down) resumes after the newest
    # step checkpoint it wrote; anything else starts over.
    set st  [get_property STATUS $ir]
    set dir [get_property DIRECTORY $ir]
    set top ${design_name}_wrapper
    set cut ""
    if {[file exists $dir/${top}_physopt.dcp]} {
        set cut route_design
    } elseif {[file exists $dir/${top}_placed.dcp]} {
        set cut [expr {[string is true [get_property STEPS.PHYS_OPT_DESIGN.IS_ENABLED $ir]]
                       ? "phys_opt_design" : "route_design"}]
    } elseif {[file exists $dir/${top}_opt.dcp]} {
        set cut place_design
    }
    if {[regexp {^(\S+) ERROR$} $st -> failed] && $failed ne "opt_design"} {
        puts "@@@ impl_1 '$st': reset from $failed"
        reset_run impl_1 -from_step $failed
    } elseif {![string match "Not started" $st] && ![regexp {ERROR} $st] && $cut ne ""} {
        puts "@@@ impl_1 '$st' cut off: resume from $cut"
        reset_run impl_1 -from_step $cut
    } elseif {![string match "Not started" $st]} {
        reset_run impl_1
    }
    v8_impl_setup $ir
    # A route-only run still takes the post-route phys_opt it enables; Vivado
    # names that step with a space and parentheses.
    if {$impl_step eq "route_design"
        && [string is true [get_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED $ir]]} {
        set impl_step {phys_opt_design (Post-Route)}
    }
    puts "@@@ ${design_name} impl_1: launched to $impl_step at [clock format [clock seconds] -format %H:%M:%S]"
    launch_runs impl_1 -to_step $impl_step -jobs 6
    wait_on_run impl_1
}
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    error "impl failed: see ${design_name}.runs/impl_1/runme.log"
}
puts "@@@ ${design_name} impl done ($impl_step) at [clock format [clock seconds] -format %H:%M:%S]"
source $V8_STAGES/80_report.tcl

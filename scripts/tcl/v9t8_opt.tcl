# v9t8 impl_1 through opt_design only: the checkpoint the placement probes
# (audit/place_probe.tcl) start from, with impl_1's own constraints and settings.
set here [file dirname [file normalize [info script]]]
set V8_CONFIG $here/v9t8/00_config.tcl
source $V8_CONFIG

open_project $proj_dir/${design_name}.xpr
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 is [get_property PROGRESS [get_runs synth_1]], not complete"
}
set ir [get_runs impl_1]
set d [v8_impl_dirs]
set_property STEPS.OPT_DESIGN.ARGS.DIRECTIVE [dict get $d opt] $ir
foreach p [list_property $ir -regexp {^STEPS\..*\.TCL\.(PRE|POST)$}] {
    if {[get_property $p $ir] ne ""} { set_property $p {} $ir }
}
if {![string match "Not started" [get_property STATUS $ir]]} { reset_run impl_1 }
launch_runs impl_1 -to_step opt_design -jobs 6
wait_on_run impl_1
set dcp [get_property DIRECTORY $ir]/${design_name}_wrapper_opt.dcp
if {![file exists $dcp]} { error "no opt checkpoint: $dcp" }
puts "@@@ v9t8 opt checkpoint: $dcp"

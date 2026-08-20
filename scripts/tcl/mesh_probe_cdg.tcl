# Wave 2.1: CLOCK_DELAY_GROUP on the pump 1x/2x BUFGCE_DIV pair, then
# re-place+route from the existing synth. One knob vs impl_1's defaults.
#   vivado -mode batch -source mesh_probe_cdg.tcl -tclargs <tag>

set tag [lindex $argv 0]
set_param general.maxThreads 12
open_project mp_$tag.xpr

if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 is not complete"
}

set xdc [pwd]/cdg_pair.xdc
set fh [open $xdc w]
puts $fh "set_property CLOCK_DELAY_GROUP grp_pump \\"
puts $fh "    \[get_nets -of \[get_pins \{mp_${tag}_i/pump_mesh/inst/u_1x/O mp_${tag}_i/pump_mesh/inst/u_2x/O\}\]\]"
close $fh
add_files -fileset constrs_1 $xdc
set_property USED_IN {implementation} [get_files $xdc]

set run impl_cdg
if {[llength [get_runs -quiet $run]]} { reset_run $run ; delete_run $run }
create_run $run -parent_run synth_1 -flow [get_property FLOW [get_runs impl_1]]
launch_runs $run -to_step route_design -jobs 4
wait_on_run $run
puts "@@@ probe $tag $run [get_property PROGRESS [get_runs $run]] cdg grp_pump"
puts "@@@ probe $tag done"

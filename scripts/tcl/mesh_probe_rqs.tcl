# Wave 2.3: re-implement with the machine-curated RQS suggestions applied
# (FORCE_MAX_FANOUT on the named critical nets, USER_CLOCK_ROOT, SRL remap).
# Only impl-scope suggestions apply from synth_1's netlist; that is the point.
#   vivado -mode batch -source mesh_probe_rqs.tcl -tclargs <tag> <rqs-file>

set tag [lindex $argv 0]
set rqs [lindex $argv 1]
set_param general.maxThreads 12
open_project mp_$tag.xpr

if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 is not complete"
}
read_qor_suggestions $rqs
puts "@@@ rqs loaded: [llength [get_qor_suggestions]] suggestions"

set run impl_rqs
if {[llength [get_runs -quiet $run]]} { reset_run $run ; delete_run $run }
create_run $run -parent_run synth_1 -flow [get_property FLOW [get_runs impl_1]]
set_property RQS_FILES $rqs [get_runs $run]
launch_runs $run -to_step route_design -jobs 4
wait_on_run $run
puts "@@@ probe $tag $run [get_property PROGRESS [get_runs $run]] rqs-applied"
puts "@@@ probe $tag done"

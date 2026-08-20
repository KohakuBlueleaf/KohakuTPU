# Wave 2.2: forced replication of the pump-phase control nets, applied
# POST-PLACE (Vivado_Tcl 4-265: -force_replication_on_nets is not supported
# post-route), then a fresh route. Compare the routed report against impl_1's.
# open_checkpoint, not open_project: no project lock, runs beside RQS.
#   vivado -mode batch -source mesh_probe_replic.tcl -tclargs <physopt.dcp> <outdir> <prefix>

set dcp [lindex $argv 0]
set out [lindex $argv 1]
set pfx [lindex $argv 2]
if {$out eq ""} { set out [file dirname $dcp]/replic }
file mkdir $out

set_param general.maxThreads 12
open_checkpoint $dcp

set nets [list]
foreach cu {u_cu0 u_cu1 u_cu2 u_cu3 u_cu4 u_cu5 u_cu6 u_cu7} {
    foreach sig {ph1_d tog_qq} {
        set n [get_nets -quiet "$pfx/mesh_u/inst/$cu/g_p2.u_node/u_mgr/$sig"]
        if {[llength $n]} { lappend nets $n }
    }
}
puts "@@@ replic targets: [llength $nets] nets"
if {[llength $nets] < 8} { error "too few target nets found -- wrong prefix?" }

phys_opt_design -force_replication_on_nets [get_nets $nets]
route_design
report_timing_summary -max_paths 4 -file $out/replic_routed.rpt
write_checkpoint -force $out/replic_routed.dcp

puts "@@@ replic done: $out/replic_routed.rpt vs impl_1's routed summary"

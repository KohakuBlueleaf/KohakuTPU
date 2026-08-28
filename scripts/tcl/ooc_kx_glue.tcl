# Exact cost of kx_mempath_e's own glue (crossbar selects + aggregation): synth the
# top with kx_carray / kx_rd_engine / kx_wr_engine as black boxes, so nothing of
# theirs is attributed to it. Pairs with the standalone block synths for a
# component table that ADDS UP to the fused total.
#   vivado -mode batch -source ooc_kx_glue.tcl -tclargs <generics|-> <file>...
set gspec [lindex $argv 0]
set files [lrange $argv 1 end]
set part  xcvu13p-fhgb2104-2L-e
set generics {}
if {$gspec ne "-"} {
    foreach kv [split $gspec "+"] { set p [split $kv ":"]; lappend generics "[lindex $p 0]=[lindex $p 1]" }
}
foreach f $files { read_verilog $f }
foreach bb {kx_carray kx_rd_engine kx_wr_engine} {
    catch { set_property black_box true [get_files -quiet *${bb}.v] }
}
set cmd [list synth_design -top kx_mempath_e -part $part -mode out_of_context]
foreach g $generics { lappend cmd -generic $g }
# Black-box by removing the bodies: read only the top + link/cdc files, the
# three blocks resolve as unknown modules and become black boxes.
if {[catch {eval $cmd} err]} { puts "SYNTH FAILED: $err"; exit 1 }
foreach line [split [report_utilization -return_string] "\n"] {
    if {[regexp {CLB LUTs|CLB Registers} $line]} { puts "GLUE $line" }
}
report_utilization -hierarchical -hierarchical_depth 2 -file kx_glue_hier.rpt
puts "GLUE done"

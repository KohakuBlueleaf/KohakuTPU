# v8 post-route report: per-SLR resources, SLL per boundary, WNS per clock
# group, the worst path, everything the pblock claims. Run from v8_impl.tcl
# or alone:  vivado -mode batch -source scripts/tcl/v8/80_report.tcl
if {![info exists design_name]} {
    set here [file dirname [file normalize [info script]]]
    source $here/00_config.tcl
    open_project $proj_dir/${design_name}.xpr
}
open_run impl_1 -name v8_impl
set out $root/build
file mkdir $out
set top ${design_name}_i
set rpt $out/${design_name}_impl
report_timing_summary -file ${rpt}_timing.rpt
report_utilization -file ${rpt}_util.rpt
report_utilization -slr -file ${rpt}_util_slr.rpt
report_utilization -hierarchical -hierarchical_depth 3 -file ${rpt}_util_hier.rpt
report_utilization -pblocks [get_pblocks] -file ${rpt}_util_pblocks.rpt
report_route_status -file ${rpt}_route.rpt
report_design_analysis -congestion -file ${rpt}_congestion.rpt
report_power -file ${rpt}_power.rpt

puts "\n=== route status ==="
set rs [report_route_status -return_string]
foreach l [split $rs \n] { if {[regexp {nets with routing errors|unrouted|Fully routed nets} $l]} { puts "  [string trim $l]" } }

puts "\n=== per-SLR usage ==="
set slr_rpt [report_utilization -slr -return_string]
foreach l [split $slr_rpt \n] {
    if {[regexp {^\|\s*(CLB LUTs|CLB Registers|Block RAM Tile|URAM|DSPs|Laguna)} $l]} { puts "  [string trim $l]" }
}
puts "\n=== per-pblock usage ==="
foreach pb [lsort [get_pblocks]] {
    if {[catch {report_utilization -pblocks $pb -return_string} r]} { puts "  $pb: $r" ; continue }
    foreach row {"CLB LUTs" "CLB Registers" "Block RAM Tile" "URAM" "DSPs"} {
        if {[regexp "\\|\\s*$row\\*?\\s*\\|\\s*(\[0-9.\]+)\\s*\\|\\s*(\[0-9.\]+)\\s*\\|\\s*(\[0-9.\]+)\\s*\\|\\s*(\[0-9.\]+)" $r all used fixed avail pct]} {
            puts [format "  %-8s %-16s %10s / %-10s %6s%%" $pb $row $used $avail $pct]
        }
    }
}

# Every net that crosses a die: tally per boundary, and per owner so the
# station line, the interlink and the Xache's crossings are told apart.
puts "\n=== SLL per boundary ==="
array unset xb ; array unset xo
set xnets [get_nets -quiet -hier -filter {CROSSING_SLRS != ""}]
foreach n $xnets {
    set cs [get_property CROSSING_SLRS $n]
    foreach pair [split $cs " "] { if {$pair ne ""} { incr xb($pair) } }
    set p [split [get_property NAME $n] /]
    set owner [lindex $p 1]
    if {$owner eq "xache"} { set owner "xache/[lindex $p 4]" }
    if {$owner eq "station_bus"} { set owner "station_bus/[lindex $p 4]" }
    incr xo($owner)
}
puts "  crossing nets [llength $xnets]"
foreach k [lsort [array names xb]] { puts [format "  %-14s %6d" $k $xb($k)] }
puts "  by owner:"
set rows {}
foreach {k v} [array get xo] { lappend rows [list $v $k] }
foreach r [lsort -integer -index 0 -decreasing $rows] { puts [format "  %6d  %s" [lindex $r 0] [lindex $r 1]] }
set lag [get_sites -quiet -filter {SITE_TYPE =~ LAGUNA*}]
set lagc [get_cells -quiet -of_objects $lag]
puts "  laguna cells [llength $lagc] of [llength $lag] sites"

puts "\n=== WNS per clock ==="
puts [format "  %-52s %9s %9s %9s %s" clock period WNS TNS levels]
set worst_all ""
foreach c [lsort -dictionary [get_property NAME [get_clocks]]] {
    set p [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type max -sort_by slack -to [get_clocks $c]]
    if {![llength $p]} { continue }
    set tns [get_timing_paths -quiet -max_paths 200 -delay_type max -slack_lesser_than 0 -to [get_clocks $c]]
    set t 0.0 ; foreach q $tns { set t [expr {$t + [get_property SLACK $q]}] }
    puts [format "  %-52s %9.3f %+9.3f %+9.3f %s" $c [get_property PERIOD [get_clocks $c]] \
          [get_property SLACK $p] $t [get_property LOGIC_LEVELS $p]]
}
set wp [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type max -sort_by slack]
if {[llength $wp]} {
    puts "\n=== worst path ==="
    puts [format "  slack %+.3f  levels %s  group %s" [get_property SLACK $wp] [get_property LOGIC_LEVELS $wp] [get_property GROUP $wp]]
    puts "  from [get_property STARTPOINT_PIN $wp]"
    puts "  to   [get_property ENDPOINT_PIN $wp]"
    puts "  data path [get_property DATAPATH_DELAY $wp] ns, logic [get_property DATAPATH_LOGIC_DELAY $wp], net [get_property DATAPATH_NET_DELAY $wp]"
}
set hp [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type min -sort_by slack]
if {[llength $hp]} { puts [format "  hold WHS %+.3f" [get_property SLACK $hp]] }
# ten worst, so a group's failure is attributed at a glance
puts "\n=== ten worst setup paths ==="
foreach p [get_timing_paths -quiet -max_paths 10 -nworst 1 -delay_type max -sort_by slack] {
    puts [format "  %+8.3f lv %2s  %s -> %s" [get_property SLACK $p] [get_property LOGIC_LEVELS $p] \
          [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
}
set bit $proj_dir/${design_name}.runs/impl_1/${design_name}_wrapper.bit
puts "\n@@@ bitstream [expr {[file exists $bit] ? "$bit [file size $bit] bytes" : "NOT WRITTEN"}]"
puts "@@@ reports in $out"

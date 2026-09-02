# Place and route ONE sysnode out of context in one SLR: its own congestion,
# occupancy and worst paths, free of the xache and the mesh. OOC mode keeps
# the ports off physical IO.
#   -tclargs <tag> <generics NAME:VALUE+...|-> <period_ns> <file>...

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set tag   [lindex $argv 0]
set gspec [lindex $argv 1]
set per   [lindex $argv 2]
set files [lrange $argv 3 end]
if {$tag   eq ""} { set tag   sn }
if {$gspec eq ""} { set gspec - }
if {$per   eq ""} { set per   3.333 }
set out $root/build/impl_sysnode_$tag
file mkdir $out

set_param general.maxThreads 8

foreach f $files { read_verilog [file join $root $f] }

set generics {}
if {$gspec ne "-"} {
    foreach kv [split $gspec "+"] {
        set p [split $kv ":"]
        lappend generics "[lindex $p 0]=[lindex $p 1]"
    }
}
puts "@@@ impl_sysnode $tag period $per generics {$generics}"
set cmd [list synth_design -top sysnode -part $part -mode out_of_context]
foreach g $generics { lappend cmd -generic $g }
if {[catch {eval $cmd} err]} { puts "SYNTH FAILED: $err"; exit 1 }
report_utilization -file $out/util_synth.rpt

set names {}
foreach cp [get_ports -quiet -filter {DIRECTION == IN}] {
    set nm [get_property NAME $cp]
    if {[string match "*clk*" $nm]} {
        create_clock -name $nm -period $per [get_ports $nm]
        lappend names $nm
    }
}
if {[llength $names] > 1} {
    set grp {}
    foreach nm $names { lappend grp -group [get_clocks $nm] }
    set_clock_groups -asynchronous {*}$grp
}
puts "@@@ clocks: $names"

create_pblock pb_sn
resize_pblock [get_pblocks pb_sn] -add "CLOCKREGION_X0Y4:CLOCKREGION_X7Y7"
add_cells_to_pblock [get_pblocks pb_sn] [get_cells -quiet -hier -filter IS_PRIMITIVE]

opt_design
place_design
report_design_analysis -congestion -file $out/congestion_place.rpt
phys_opt_design
route_design
report_design_analysis -congestion -file $out/congestion_route.rpt
write_checkpoint -force $out/routed.dcp

foreach {what cmd} [list \
    route_status  [list report_route_status -file $out/route_status.rpt] \
    util          [list report_utilization -file $out/util.rpt] \
    hier          [list report_utilization -hierarchical -hierarchical_depth 4 -file $out/hier.rpt] \
    timing_sum    [list report_timing_summary -file $out/timing_summary.rpt] \
    timing        [list report_timing -delay_type max -max_paths 100 -nworst 1 -sort_by slack -input_pins -file $out/timing.rpt]] {
    if {[catch {eval $cmd} err]} { puts "@@@ WARNING report $what failed: $err" }
}

# EVERY failing endpoint, not report_timing's 100-path sample
set ef [open $out/endpoints.tsv w]
puts $ef "SLACK\tLEVELS\tSTART\tEND"
foreach tp [get_timing_paths -quiet -max_paths 200000 -nworst 1 -setup -slack_lesser_than 0] {
    puts $ef "[get_property SLACK $tp]\t[get_property LOGIC_LEVELS $tp]\t[get_property STARTPOINT_PIN $tp]\t[get_property ENDPOINT_PIN $tp]"
}
close $ef

# every placed primitive's LOC: the occupancy analysis reads this offline
set f [open $out/cell_loc.tsv w]
puts $f "LOC\tNAME"
foreach c [get_cells -quiet -hier -filter IS_PRIMITIVE] {
    set l [get_property -quiet LOC $c]
    if {$l ne ""} { puts $f "$l\t[get_property NAME $c]" }
}
close $f
puts "@@@ impl_sysnode done $tag -> $out"

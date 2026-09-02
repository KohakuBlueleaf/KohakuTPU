# Place and route ktpu_quad_ref: 4 sysnodes one per SLR, the interlink chain
# across every boundary, and with XACHE the KTS-line Xache -- the both-sided
# stretch reference. OOC keeps ports off physical IO.
#   -tclargs <tag> <generics NAME:VALUE+...|-> <period_ns> <file>...

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set tag   [lindex $argv 0]
set gspec [lindex $argv 1]
set per   [lindex $argv 2]
set files [lrange $argv 3 end]
if {$tag   eq ""} { set tag   q1 }
if {$gspec eq ""} { set gspec - }
if {$per   eq ""} { set per   3.333 }
set out $root/build/impl_quad_$tag
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
puts "@@@ impl_quad $tag period $per generics {$generics}"
set cmd [list synth_design -top ktpu_quad_ref -part $part -mode out_of_context \
    -include_dirs [file join $root src/kohakutransmit/packet]]
foreach g $generics { lappend cmd -generic $g }
if {[catch {eval $cmd} err]} { puts "SYNTH FAILED: $err"; exit 1 }
report_utilization -file $out/util_synth.rpt

create_clock -name clk -period $per [get_ports clk]

set rows {0 {Y0 Y3} 1 {Y4 Y7} 2 {Y8 Y11} 3 {Y12 Y15}}
set NP 4
for {set p 0} {$p < $NP} {incr p} {
    lassign [dict get $rows $p] ylo yhi
    create_pblock pb_slr$p
    resize_pblock [get_pblocks pb_slr$p] \
        -add "CLOCKREGION_X0${ylo}:CLOCKREGION_X7${yhi}"
    set_property CONTAIN_ROUTING false [get_pblocks pb_slr$p]
}
proc pin {p what cells} {
    if {[llength $cells]} {
        add_cells_to_pblock [get_pblocks pb_slr$p] $cells
        puts "@@@ pin SLR$p [llength $cells] cells: $what"
    } else {
        puts "@@@ WARNING pin SLR$p matched no cells: $what"
    }
}
for {set p 0} {$p < $NP} {incr p} {
    pin $p "node $p" [get_cells -quiet -hier -filter \
        "NAME =~ g_n?$p?.u_mag/* && IS_PRIMITIVE"]
    if {$p > 0} {
        pin [expr {$p - 1}] "pipe up b[expr {$p-1}] tx" [get_cells -quiet -hier -filter \
            "NAME =~ g_n?$p?.g_dn.u_pu/u_tx*"]
        pin $p "pipe up b[expr {$p-1}] rx" [get_cells -quiet -hier -filter \
            "NAME =~ g_n?$p?.g_dn.u_pu/u_rx*"]
        pin $p "pipe dn b[expr {$p-1}] tx" [get_cells -quiet -hier -filter \
            "NAME =~ g_n?$p?.g_dn.u_pd/u_tx*"]
        pin [expr {$p - 1}] "pipe dn b[expr {$p-1}] rx" [get_cells -quiet -hier -filter \
            "NAME =~ g_n?$p?.g_dn.u_pd/u_rx*"]
    }
    foreach {what pat} [list \
        "xache home $p"  "g_kx.u_kx/g_home?$p?.*" \
        "xache m $p"     "g_kx.u_kx/g_m?$p?.*" \
        "xache medge $p" "g_kx.u_kx/g_medge?$p?.*" \
        "xache hedge $p" "g_kx.u_kx/g_hedge?$p?.*" \
        "xache kts $p"   "g_kx.u_kx/g_kts.g_p?$p?.*"] {
        pin $p $what [get_cells -quiet -hier -filter "NAME =~ $pat && IS_PRIMITIVE"]
    }
}

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
    util_slr      [list report_utilization -slr -file $out/util_slr.rpt] \
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

set lf [open $out/cell_loc.tsv w]
puts $lf "LOC\tNAME"
foreach c [get_cells -quiet -hier -filter IS_PRIMITIVE] {
    set l [get_property -quiet LOC $c]
    if {$l ne ""} { puts $lf "$l\t[get_property NAME $c]" }
}
close $lf
puts "@@@ impl_quad done $tag -> $out"

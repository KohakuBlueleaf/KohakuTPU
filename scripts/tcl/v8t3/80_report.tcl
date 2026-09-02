# v8t3 post-route report: per-SLR and per-pblock resources, the big cells per
# die, SLL per boundary and owner, WNS per clock, the worst paths with their
# properties (endpoints, clocks, levels, delays, die crossings), hold.
# Run from v8t3_impl.tcl or alone:
#   vivado -mode batch -source scripts/tcl/v8t3/80_report.tcl
if {![info exists design_name]} {
    set here [file dirname [file normalize [info script]]]
    source $here/00_config.tcl
    open_project $proj_dir/${design_name}.xpr
}
open_run impl_1 -name ${design_name}_impl
set out $root/build
file mkdir $out
set top ${design_name}_i
set rpt $out/${design_name}_impl
report_timing_summary -file ${rpt}_timing.rpt
report_utilization -file ${rpt}_util.rpt
report_utilization -slr -file ${rpt}_util_slr.rpt
report_utilization -hierarchical -hierarchical_depth 3 -file ${rpt}_util_hier.rpt
# one pblock per call: report_utilization -pblocks takes exactly one
file delete -force ${rpt}_util_pblocks.rpt
foreach pb [lsort [get_pblocks]] {
    report_utilization -pblocks $pb -append -file ${rpt}_util_pblocks.rpt
}
report_route_status -file ${rpt}_route.rpt
report_design_analysis -congestion -file ${rpt}_congestion.rpt
report_design_analysis -complexity -hierarchical_depth 2 -file ${rpt}_complexity.rpt
report_power -file ${rpt}_power.rpt
report_timing -max_paths 50 -nworst 1 -delay_type max -sort_by slack -file ${rpt}_worst50.rpt
report_timing -max_paths 10 -nworst 1 -delay_type min -sort_by slack -file ${rpt}_hold10.rpt

set pfh [open ${rpt}_paths.txt w]
proc say {s} { global pfh ; puts $s ; puts $pfh $s }

say "\n=== route status ==="
set rs [report_route_status -return_string]
foreach l [split $rs \n] { if {[regexp {nets with routing errors|unrouted|Fully routed nets} $l]} { say "  [string trim $l]" } }

say "\n=== per-SLR usage ==="
set slr_rpt [report_utilization -slr -return_string]
foreach l [split $slr_rpt \n] {
    if {[regexp {^\|\s*(CLB LUTs|CLB Registers|CLB\s|Block RAM Tile|URAM|DSPs|Laguna)} $l]} { say "  [string trim $l]" }
}
say "\n=== per-pblock usage ==="
foreach pb [lsort [get_pblocks]] {
    if {[catch {report_utilization -pblocks $pb -return_string} r]} { say "  $pb: $r" ; continue }
    foreach row {"CLB LUTs" "CLB Registers" "CLB" "Block RAM Tile" "URAM" "DSPs"} {
        if {[regexp "\\|\\s*$row\\*?\\s*\\|\\s*(\[0-9.\]+)\\s*\\|\\s*(\[0-9.\]+)\\s*\\|\\s*(\[0-9.\]+)\\s*\\|\\s*(\[0-9.\]+)" $r all used fixed avail pct]} {
            say [format "  %-8s %-16s %10s / %-10s %6s%%" $pb $row $used $avail $pct]
        }
    }
}
# The big cells, one row each: what each die's numbers are made of.
say "\n=== cells ==="
say [format "  %-34s %9s %9s %8s %6s %6s" cell LUT FF BRAM URAM DSP]
proc v8_cell_row {name pat} {
    set cells [get_cells -quiet -hier -filter "NAME =~ $pat"]
    if {![llength $cells]} { say [format "  %-34s %s" $name "(no cells)"] ; return }
    if {[catch {report_utilization -cells $cells -return_string} r]} { say "  $name: $r" ; return }
    set v {}
    foreach row {"CLB LUTs" "CLB Registers" "Block RAM Tile" "URAM" "DSPs"} {
        if {[regexp "\\|\\s*$row\\*?\\s*\\|\\s*(\[0-9.\]+)" $r all n]} { lappend v $n } else { lappend v - }
    }
    say [format "  %-34s %9s %9s %8s %6s %6s" $name {*}$v]
}
foreach {mid mod} $MESHES {
    v8_cell_row mesh_$mid $top/mesh_$mid
    v8_cell_row "  sysnode $mid" $top/mesh_$mid/inst/g_mag*
    v8_cell_row "station g_stn\[$mid\]" "$top/station_bus/inst/u_line/g_stn\[$mid\].*"
    v8_cell_row ddr4_$mid $top/ddr4_$mid
    v8_cell_row dwc_ctrl$mid $top/dwc_ctrl$mid
}
v8_cell_row xache $top/xache
v8_cell_row "station g_link (all)" "$top/station_bus/inst/u_line/g_link*"
v8_cell_row "interlink pipes" "$top/pipe_*"
v8_cell_row xdma_0 $top/xdma_0
v8_cell_row jtag_ctrl $top/jtag_ctrl

# Congestion, with the cells that sit in each congested window: the tables of
# report_design_analysis carry the region, level and the hierarchy breakdown.
say "\n=== congestion ==="
set cgl 0
foreach l [split [report_design_analysis -congestion -return_string] \n] {
    set t [string trim $l]
    if {$t eq ""} { continue }
    if {[regexp {congestion|Congest|^\+\-|^\| } $l]} {
        say "  $l"
        if {[incr cgl] >= 160} { say "  ... (rest in ${rpt}_congestion.rpt)" ; break }
    }
}
say "  full tables: ${rpt}_congestion.rpt, complexity: ${rpt}_complexity.rpt"

# Every net that crosses a die: per boundary, and per owner.
say "\n=== SLL per boundary ==="
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
say "  crossing nets [llength $xnets]"
foreach k [lsort [array names xb]] { say [format "  %-14s %6d" $k $xb($k)] }
say "  by owner:"
set rows {}
foreach {k v} [array get xo] { lappend rows [list $v $k] }
foreach r [lsort -integer -index 0 -decreasing $rows] { say [format "  %6d  %s" [lindex $r 0] [lindex $r 1]] }
set lag [get_sites -quiet -filter {SITE_TYPE =~ LAGUNA*}]
set lagc [get_cells -quiet -of_objects $lag]
say "  laguna cells [llength $lagc] of [llength $lag] sites"

# A path's properties in one line: slack, levels, clocks, delays, crossings.
proc v8_path_line {p} {
    set x 0
    foreach n [get_nets -quiet -of_objects $p] {
        if {[get_property -quiet CROSSING_SLRS $n] ne ""} { incr x }
    }
    set sc [get_property -quiet STARTPOINT_CLOCK $p]
    set ec [get_property -quiet ENDPOINT_CLOCK $p]
    set sn [expr {[llength $sc] ? [get_property NAME $sc] : "-"}]
    set en [expr {[llength $ec] ? [get_property NAME $ec] : "-"}]
    # a BSCAN/JTAG path can lack any float property; print - instead of dying
    set v {}
    foreach {prop fmt} {SLACK %+7.3f DATAPATH_DELAY %6.3f DATAPATH_LOGIC_DELAY %5.3f
                        DATAPATH_NET_DELAY %5.3f REQUIREMENT %6.3f SKEW %+6.3f} {
        set n [get_property -quiet $prop $p]
        lappend v [expr {$n eq "" ? "-" : [format $fmt $n]}]
    }
    lassign $v slk dp lg nt rq sk
    return [format "slack %s lv %2s xslr %d dp %s (logic %s net %s) req %s skew %s  %s -> %s  %s -> %s" \
        $slk [get_property -quiet LOGIC_LEVELS $p] $x $dp $lg $nt $rq $sk $sn $en \
        [get_property -quiet STARTPOINT_PIN $p] [get_property -quiet ENDPOINT_PIN $p]]
}

say "\n=== WNS per clock ==="
say [format "  %-52s %9s %9s %9s %s" clock period WNS TNS levels]
foreach c [lsort -dictionary [get_property NAME [get_clocks]]] {
    set p [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type max -sort_by slack -to [get_clocks $c]]
    if {![llength $p]} { continue }
    set tns [get_timing_paths -quiet -max_paths 200 -delay_type max -slack_lesser_than 0 -to [get_clocks $c]]
    set t 0.0 ; foreach q $tns { set t [expr {$t + [get_property SLACK $q]}] }
    say [format "  %-52s %9.3f %+9.3f %+9.3f %s" $c [get_property PERIOD [get_clocks $c]] \
          [get_property SLACK $p] $t [get_property LOGIC_LEVELS $p]]
}
say "\n=== worst setup path per clock ==="
foreach c [lsort -dictionary [get_property NAME [get_clocks]]] {
    set p [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type max -sort_by slack -to [get_clocks $c]]
    if {![llength $p]} { continue }
    say "  $c"
    say "    [v8_path_line $p]"
}
say "\n=== twenty worst setup paths ==="
foreach p [get_timing_paths -quiet -max_paths 20 -nworst 1 -delay_type max -sort_by slack] {
    say "  [v8_path_line $p]"
}

# The 200 worst, bucketed by kind and owner: which module, which type. A
# reset-family path starts in a reset cell or ends on a reset pin; everything
# else is control/data and is the kind that matters.
proc v8_owner {pin} {
    set parts [split $pin /]
    set o [lindex $parts 1]
    if {$o eq "xache"} { set o "xache/[lindex $parts 4]" }
    if {$o eq "station_bus"} { set o "station_bus/[lindex $parts 4]" }
    if {[string match "mesh_*" $o] && [lindex $parts 3] ne ""} {
        set o "$o/[string range [lindex $parts 3] 0 27]"
    }
    return $o
}
say "\n=== path classes (200 worst setup) ==="
array unset PC
foreach p [get_timing_paths -quiet -max_paths 200 -nworst 1 -delay_type max -sort_by slack] {
    set sp [get_property STARTPOINT_PIN $p]
    set ep [get_property ENDPOINT_PIN $p]
    set so [v8_owner $sp]
    set eo [v8_owner $ep]
    set epin [lindex [split $ep /] end]
    set kind ctrl/data
    if {[string match "*rst*" $so] || [lsearch {CLR PRE S R SR SRST RST} $epin] >= 0} { set kind reset }
    set x 0
    foreach n [get_nets -quiet -of_objects $p] { if {[get_property -quiet CROSSING_SLRS $n] ne ""} { incr x } }
    set k [format "%-9s %s -> %s%s" $kind $so $eo [expr {$x ? "  (xSLR)" : ""}]]
    if {![info exists PC($k)]} { set PC($k) [list 0 99.0] }
    lassign $PC($k) n w
    incr n
    set s [get_property SLACK $p]
    if {$s < $w} { set w $s }
    set PC($k) [list $n $w]
}
set rows {}
foreach {k v} [array get PC] { lappend rows [list [lindex $v 1] [lindex $v 0] $k] }
foreach r [lsort -real -index 0 $rows] {
    say [format "  worst %+8.3f  n=%-4d  %s" [lindex $r 0] [lindex $r 1] [lindex $r 2]]
}
say "\n=== ten worst hold paths ==="
foreach p [get_timing_paths -quiet -max_paths 10 -nworst 1 -delay_type min -sort_by slack] {
    say "  [v8_path_line $p]"
}
# The die-crossing paths on their own: the register pairs the floorplan pins.
say "\n=== worst die-crossing setup paths ==="
set nx 0
foreach p [get_timing_paths -quiet -max_paths 400 -nworst 1 -delay_type max -sort_by slack] {
    set x 0
    foreach n [get_nets -quiet -of_objects $p] { if {[get_property -quiet CROSSING_SLRS $n] ne ""} { incr x } }
    if {!$x} { continue }
    say "  [v8_path_line $p]"
    if {[incr nx] >= 15} { break }
}
if {!$nx} { say "  none among the 400 worst" }
close $pfh
set bit $proj_dir/${design_name}.runs/impl_1/${design_name}_wrapper.bit
puts "\n@@@ bitstream [expr {[file exists $bit] ? "$bit [file size $bit] bytes" : "NOT WRITTEN"}]"
puts "@@@ reports in $out (paths: ${rpt}_paths.txt)"

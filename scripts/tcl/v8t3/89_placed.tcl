# Placed-checkpoint measurement in its own Vivado (v8t_placed.ps1 starts it):
#   vivado -mode batch -source scripts/tcl/v8t3/89_placed.tcl -tclargs <config> <dcp>

lassign $argv v8_cfg v8_dcp
source $v8_cfg
set_param tcl.collectionResultDisplayLimit 0
open_checkpoint $v8_dcp
set v8_pre $root/build/${design_name}_placed

set v8_cs [open ${v8_pre}_clock_span.rpt w]
puts $v8_cs "checkpoint $v8_dcp"
set v8_t [clock seconds]
set v8_nets [get_nets -quiet -hierarchical -top_net_of_hierarchical_group \
                 -filter {TYPE == GLOBAL_CLOCK}]
puts $v8_cs "global clock nets [llength $v8_nets]"
set v8_span 0
foreach v8_n $v8_nets {
    set v8_cells [get_cells -quiet -of_objects \
                      [get_pins -quiet -leaf -of_objects $v8_n -filter {DIRECTION == IN}]]
    set v8_slrs [lsort [get_slrs -quiet -of_objects $v8_cells]]
    if {[llength $v8_slrs] <= 1} { continue }
    incr v8_span
    puts $v8_cs "\n$v8_n  loads [llength $v8_cells]  SLRs $v8_slrs"
    set v8_g [dict create]
    # bit indices off the leaf (rx_d_reg[5] -> rx_d_reg), generate indices kept
    foreach v8_name [get_property NAME $v8_cells] {
        regsub {(\[\d+\])+$} $v8_name {} v8_k
        dict lappend v8_g [join [lrange [split $v8_k /] 0 7] /] $v8_name
    }
    # the SLR holding the most loads is the clock's home; list every group
    # with a load anywhere else
    set v8_per [dict create]
    set v8_gs [dict create]
    dict for {v8_k v8_names} $v8_g {
        set v8_s [lsort [get_slrs -quiet -of_objects [get_cells -quiet $v8_names]]]
        dict set v8_gs $v8_k $v8_s
        foreach v8_x $v8_s { dict incr v8_per $v8_x [llength $v8_names] }
    }
    set v8_home [lindex [lsort -stride 2 -index 1 -integer -decreasing $v8_per] 0]
    puts $v8_cs "  home $v8_home  (loads by SLR, groups split across SLRs counted in each: $v8_per)"
    dict for {v8_k v8_s} $v8_gs {
        if {$v8_s eq [list $v8_home]} { continue }
        puts $v8_cs [format "  %7d  %-24s %s" [llength [dict get $v8_g $v8_k]] $v8_s $v8_k]
    }
}
puts $v8_cs "\nnets spanning SLRs $v8_span  ([expr {[clock seconds] - $v8_t}] s)"
close $v8_cs

set v8_rp [open ${v8_pre}_repl.rpt w]
puts $v8_rp "checkpoint $v8_dcp"
puts $v8_rp [format "%9s %9s %-14s %s" registers replicas mode pattern]
foreach v8_re $REPL_NETS {
    set v8_n [eval [v8_repl_nets $v8_re]]
    set v8_modes [lsort -unique [get_property -quiet MAX_FANOUT_MODE $v8_n]]
    set v8_reps [get_cells -quiet -hierarchical -regexp "${v8_re}_replica.*" \
                     -filter {IS_PRIMITIVE && REF_NAME =~ "FD.*"}]
    puts $v8_rp [format "%9d %9d %-14s %s" [llength $v8_n] [llength $v8_reps] $v8_modes $v8_re]
}
close $v8_rp

# Each USER_CLUSTER's placed footprint: clock regions, the X and Y range they
# span, SLRs, and the leaves on each side of the MIG I/O column X4.
set v8_uc [open ${v8_pre}_clusters.rpt w]
puts $v8_uc "checkpoint $v8_dcp"
puts $v8_uc [format "%-30s %7s %4s %-9s %-9s %-10s %7s %7s" cluster leaves CRs X Y SLRs "X<4" "X>=4"]
# USER_CLUSTER sits on leaves (ignored by the placer, v9t6) or on hierarchical
# instances (v9t7): a tagged instance stands for every placed leaf under it.
set v8_all [get_cells -quiet -hierarchical -filter {USER_CLUSTER != ""}]
set v8_byuc [dict create]
foreach v8_c $v8_all v8_k [get_property -quiet USER_CLUSTER $v8_all] {
    if {[get_property IS_PRIMITIVE [get_cells $v8_c]]} {
        dict lappend v8_byuc $v8_k $v8_c
    } else {
        foreach v8_l [get_cells -quiet -hierarchical -filter "IS_PRIMITIVE && NAME =~ \"$v8_c/*\""] {
            dict lappend v8_byuc $v8_k $v8_l
        }
    }
}
if {![dict size $v8_byuc]} { puts $v8_uc "no cell carries USER_CLUSTER" }
foreach v8_k [lsort -dictionary [dict keys $v8_byuc]] {
    set v8_cs [get_cells [dict get $v8_byuc $v8_k]]
    set v8_crs [get_clock_regions -quiet -of_objects [get_sites -quiet -of_objects $v8_cs]]
    set v8_xs {} ; set v8_ys {}
    foreach v8_r [get_property NAME $v8_crs] {
        regexp {X(\d+)Y(\d+)} $v8_r -> v8_x v8_y
        lappend v8_xs $v8_x ; lappend v8_ys $v8_y
    }
    set v8_xs [lsort -integer -unique $v8_xs] ; set v8_ys [lsort -integer -unique $v8_ys]
    set v8_st [get_sites -quiet -of_objects $v8_cs]
    set v8_crof [dict create]
    foreach v8_s [get_property NAME $v8_st] v8_cr [get_property CLOCK_REGION $v8_st] { dict set v8_crof $v8_s $v8_cr }
    set v8_lo 0 ; set v8_hi 0
    foreach v8_s [get_property SITE $v8_cs] {
        if {![dict exists $v8_crof $v8_s]} { continue }
        regexp {X(\d+)} [dict get $v8_crof $v8_s] -> v8_x
        if {$v8_x < 4} { incr v8_lo } else { incr v8_hi }
    }
    puts $v8_uc [format "%-30s %7d %4d %-9s %-9s %-10s %7d %7d" $v8_k [llength $v8_cs] [llength $v8_crs] \
        "[lindex $v8_xs 0]-[lindex $v8_xs end]" "[lindex $v8_ys 0]-[lindex $v8_ys end]" \
        [join [lsort [get_slrs -quiet -of_objects $v8_cs]] ,] $v8_lo $v8_hi]
}
close $v8_uc

# Die containment: every pb_slr member placed outside its SLR.
set v8_dc [open ${v8_pre}_die_leak.rpt w]
puts $v8_dc "checkpoint $v8_dcp"
foreach v8_pb [lsort [get_pblocks -quiet pb_slr*]] {
    regexp {pb_slr(\d+)} $v8_pb -> v8_d
    puts $v8_dc "$v8_pb IS_SOFT [get_property IS_SOFT $v8_pb]"
    set v8_bad 0
    foreach v8_c [get_cells -quiet -of_objects $v8_pb -filter {IS_PRIMITIVE && PRIMITIVE_LEVEL != INTERNAL}] {
        set v8_s [get_property SITE $v8_c]
        if {$v8_s eq ""} { continue }
        set v8_slr [get_property SLR_INDEX [get_slrs -of_objects [get_sites $v8_s]]]
        if {$v8_slr == $v8_d} { continue }
        incr v8_bad
        if {$v8_bad <= 100} { puts $v8_dc "  SLR$v8_slr [get_property REF_NAME $v8_c] $v8_c" }
    }
    puts $v8_dc "$v8_pb: $v8_bad placed outside SLR$v8_d"
}
close $v8_dc
puts "@@@ placed measurement: ${v8_pre}_clock_span.rpt ${v8_pre}_repl.rpt ${v8_pre}_clusters.rpt ${v8_pre}_die_leak.rpt"

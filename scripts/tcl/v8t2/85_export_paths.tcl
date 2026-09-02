# The ONE post-route export of impl_1: every physical fact and every failing
# path in a single design open (an open costs minutes to hours; never split).
set here [file dirname [file normalize [info script]]]
source $here/00_config.tcl
open_project -read_only $proj_dir/${design_name}.xpr
open_run impl_1 -name v8t2_export
set out $root/build
set top ${design_name}_i

# ---- 1. URAM + BRAM placement, with cascade chain membership --------------
set f [open $out/${design_name}_uram_loc.tsv w]
puts $f [join {LOC CASCADE_ORDER_A CASCADE_ORDER_B SELF_ADDR_A NAME} \t]
foreach c [get_cells -hier -filter {REF_NAME =~ URAM288*}] {
    puts $f [join [list [get_property LOC $c] [get_property -quiet CASCADE_ORDER_A $c] \
        [get_property -quiet CASCADE_ORDER_B $c] [get_property -quiet SELF_ADDR_A $c] \
        [get_property NAME $c]] \t]
}
close $f
set f [open $out/${design_name}_bram_loc.tsv w]
puts $f [join {LOC NAME} \t]
foreach c [get_cells -hier -filter {REF_NAME =~ RAMB36E2 || REF_NAME =~ RAMB18E2}] {
    puts $f "[get_property LOC $c]\t[get_property NAME $c]"
}
close $f
puts "@@@ ram placement done"

# ---- 2. Laguna: every site, every used cell -------------------------------
set lag_sites [get_sites -filter {SITE_TYPE =~ LAGUNA*}]
set f [open $out/${design_name}_laguna_sites.txt w]
foreach s $lag_sites { puts $f [get_property NAME $s] }
close $f
set f [open $out/${design_name}_laguna_cells.tsv w]
puts $f [join {LOC NAME} \t]
foreach c [get_cells -quiet -of_objects $lag_sites] {
    puts $f "[get_property LOC $c]\t[get_property NAME $c]"
}
close $f
puts "@@@ laguna done"

# ---- 3. Physical SLL column spread per module, from routed nodes ----------
proc v8_owner {name} {
    set p [split $name /]
    set o [lindex $p 1]
    if {$o eq "xache"} { return "xache/[lindex $p 4]" }
    if {$o eq "station_bus"} { return "station_bus/[lindex $p 4]" }
    return $o
}
array unset H
foreach n [get_nets -quiet -hier -filter {CROSSING_SLRS != ""}] {
    set o [v8_owner [get_property NAME $n]]
    set r [get_property -quiet ROUTE $n]
    array unset seen
    foreach {full x y} [regexp -all -inline {LAG[A-Z_]*_X(\d+)Y(\d+)} $r] {
        if {![info exists seen($x,$y)]} { set seen($x,$y) 1 ; incr H($o\t$x) }
    }
}
set f [open $out/${design_name}_sll_columns.tsv w]
puts $f [join {OWNER LAG_TILE_X NODES} \t]
foreach k [lsort [array names H]] { puts $f "$k\t$H($k)" }
close $f
puts "@@@ sll columns done"

# ---- 4. Module bounding boxes over placed SLICEs --------------------------
set f [open $out/${design_name}_module_bbox.tsv w]
puts $f [join {MODULE N_SLICE X_MIN X_MAX Y_MIN Y_MAX} \t]
set PATS [list]
foreach {mid mod} $MESHES {
    lappend PATS mesh_${mid}/u_cpu "$top/mesh_$mid/inst/u_mag/u_pe/u_cpu/*" \
        mesh_${mid}/u_mag_core "$top/mesh_$mid/inst/u_mag/u_mag/*" \
        mesh_${mid}/u_mover "$top/mesh_$mid/inst/u_mag/u_pe/u_mover/*" \
        mesh_${mid}/u_xform "$top/mesh_$mid/inst/u_mag/u_pe/u_xform/*" \
        mesh_${mid}/all "$top/mesh_$mid/*" \
        xache/home$mid "$top/xache/inst/u_kx/g_home?$mid?.*" \
        xache/m$mid "$top/xache/inst/u_kx/g_m?$mid?.*" \
        station/stn$mid "$top/station_bus/inst/u_line/g_stn?$mid?.*"
}
lappend PATS xdma "$top/xdma_0/*" jtag "$top/jtag_ctrl/*"
foreach {label pat} $PATS {
    set cells [get_cells -quiet -hier -filter "NAME =~ $pat && IS_PRIMITIVE"]
    if {![llength $cells]} { puts $f "$label\t0\t-\t-\t-\t-" ; continue }
    set x0 99999 ; set x1 -1 ; set y0 99999 ; set y1 -1 ; set ns 0
    foreach l [get_property LOC $cells] {
        if {[regexp {^SLICE_X(\d+)Y(\d+)$} $l all x y]} {
            incr ns
            if {$x < $x0} { set x0 $x } ; if {$x > $x1} { set x1 $x }
            if {$y < $y0} { set y0 $y } ; if {$y > $y1} { set y1 $y }
        }
    }
    puts $f [join [list $label $ns $x0 $x1 $y0 $y1] \t]
}
close $f
puts "@@@ bbox done"

# ---- 5. Every failing setup path: one TSV row each ------------------------
set paths [get_timing_paths -max_paths 200000 -nworst 1 -delay_type max \
    -slack_lesser_than 0 -sort_by slack]
puts "@@@ failing setup paths: [llength $paths]"
set COLS {SLACK LOGIC_LEVELS REQUIREMENT DATAPATH_DELAY DATAPATH_LOGIC_DELAY
          DATAPATH_NET_DELAY SKEW STARTPOINT_CLOCK ENDPOINT_CLOCK
          STARTPOINT_PIN ENDPOINT_PIN}
foreach c $COLS { set V($c) [get_property -quiet $c $paths] }
set f [open $out/${design_name}_failing_setup.tsv w]
puts $f [join [concat IDX $COLS XSLR_NETS] \t]
set n [llength $paths]
for {set i 0} {$i < $n} {incr i} {
    set row [list $i]
    foreach c $COLS { lappend row [lindex $V($c) $i] }
    if {$i < 2000} {
        set x 0
        foreach nn [get_nets -quiet -of_objects [lindex $paths $i]] {
            if {[get_property -quiet CROSSING_SLRS $nn] ne ""} { incr x }
        }
        lappend row $x
    } else { lappend row - }
    puts $f [join $row \t]
}
close $f
puts "@@@ tsv written"

# ---- 6. Full node-by-node text: worst 1000 + worst 50 per failing clock ---
report_timing -max_paths 1000 -nworst 1 -delay_type max -slack_lesser_than 0 \
    -input_pins -file $out/${design_name}_worst1000_full.rpt
puts "@@@ worst1000 written"
set pcf $out/${design_name}_perclock_worst50_full.rpt
file delete -force $pcf
foreach c [lsort -dictionary [get_property NAME [get_clocks]]] {
    set p [get_timing_paths -quiet -max_paths 1 -delay_type max -slack_lesser_than 0 -to [get_clocks $c]]
    if {![llength $p]} { continue }
    report_timing -to [get_clocks $c] -max_paths 50 -nworst 1 -delay_type max \
        -slack_lesser_than 0 -input_pins -append -file $pcf
}
puts "@@@ per-clock written; ALL exports done"

# Census on an implementation checkpoint, read-only, beside the run:
# the failing-path classes (87_synth_paths.tcl), each failing class's worst
# path as a placement (start and end tile, distance, SLRs), every block's
# placement per die, and who sits inside each congestion window.
#   vivado -mode batch -source scripts/tcl/audit/placed_census.tcl \
#       -tclargs dcp <checkpoint> tag placed margin 0 [cfg v9t4]
# cfg names the config folder (default v9t3) that sets the design and blocks.
set here [file dirname [file dirname [file normalize [info script]]]]
set V9_CFG v9t3
foreach {k v} $argv { if {$k eq "cfg"} { set V9_CFG $v } }
source $here/$V9_CFG/00_config.tcl
foreach {k v} $argv {
    switch -- $k {
        dcp    { set V8_DCP $v }
        tag    { set V8_TAG $v }
        margin { set V8_PATH_MARGIN $v }
    }
}
if {![info exists V8_DCP]} {
    set V8_DCP $proj_dir/${design_name}.runs/impl_1/${design_name}_wrapper_physopt.dcp
}
if {![info exists V8_TAG]} { set V8_TAG placed }
if {![info exists V8_PATH_MARGIN]} { set V8_PATH_MARGIN 0.0 }
if {![file exists $V8_DCP]} { error "no checkpoint at $V8_DCP" }
set V8_CLASS_FULL 120
set V8_NEAR_FULL 0
source $here/v8t3/87_synth_paths.tcl

# ---- 4. where each failing class's worst path sits -------------------------
# Tile COLUMN/ROW is one grid for every site type, so the distance between a
# block RAM and a slice is comparable to one between two slices.
proc cz_place {pin} {
    set c [get_cells -quiet -of_objects $pin]
    set loc [get_property -quiet LOC $c]
    if {$loc eq ""} { return {- - - - -} }
    set s [get_sites -quiet $loc]
    set t [get_tiles -quiet -of_objects $s]
    return [list $loc [get_property COLUMN $t] [get_property ROW $t] \
                [get_property CLOCK_REGION $s] [get_property NAME [get_slrs -of_objects $s]]]
}
# HOPS: tile distance summed cell to cell along the path, against DIST (start
# to end); MAXFO: the largest fanout of a net on the path. "-" when the pin
# list does not start at the startpoint cell, so the order cannot be trusted.
proc cz_walk {po} {
    set pins [get_pins -quiet -of_objects $po]
    if {![llength $pins]} { return {- -} }
    set c0 [get_cells -quiet -of_objects [lindex $pins 0]]
    if {$c0 ne [get_cells -quiet -of_objects [get_property STARTPOINT_PIN $po]]} { return {- -} }
    set hops 0 ; set fo 0 ; set prev {}
    foreach p $pins {
        set loc [get_property -quiet LOC [get_cells -quiet -of_objects $p]]
        if {$loc ne ""} {
            set t [lindex [get_tiles -quiet -of_objects [get_sites -quiet $loc]] 0]
            set xy [list [get_property COLUMN $t] [get_property ROW $t]]
            if {$prev ne ""} {
                incr hops [expr {abs([lindex $xy 0] - [lindex $prev 0]) + abs([lindex $xy 1] - [lindex $prev 1])}]
            }
            set prev $xy
        }
        if {[get_property -quiet DIRECTION $p] eq "OUT"} {
            set n [get_nets -quiet -of_objects $p]
            if {[llength $n]} {
                set f [expr {[get_property FLAT_PIN_COUNT [lindex $n 0]] - 1}]
                if {$f > $fo} { set fo $f }
            }
        }
    }
    return [list $hops $fo]
}
set fh [open ${tag}_classes_place.tsv w]
puts $fh [join {WORST N FAIL TNS LEVELS DPATH LOGIC NET SKEW DIST HOPS MAXFO S_LOC S_REGION S_SLR E_LOC E_REGION E_SLR CLASS} \t]
set nd 0
set nwalk_bad 0
foreach r $CMR {
    if {[lindex $r 0] >= 0 || $nd >= 2000} { break }
    incr nd
    set po [lindex $r 9]
    lassign [cz_place [get_property STARTPOINT_PIN $po]] sl sx sy sr ss
    lassign [cz_place [get_property ENDPOINT_PIN $po]] el ex ey er es
    set dist -
    if {$sx ne "-" && $ex ne "-"} { set dist [expr {abs($sx - $ex) + abs($sy - $ey)}] }
    lassign [cz_walk $po] hops maxfo
    if {$hops eq "-"} { incr nwalk_bad }
    puts $fh [join [list {*}[lrange $r 0 7] [get_property SKEW $po] $dist $hops $maxfo \
                         $sl $sr $ss $el $er $es [string map {"\t" " | "} [lindex $r 8]]] \t]
}
close $fh
puts "@@@ placement of $nd failing merged classes in ${tag}_classes_place.tsv, $nwalk_bad without a path walk  [v8_t]"

# ---- 5. block placement per die ---------------------------------------------
set BLOCKS {
    {cpu        mesh_N/inst/u_mag/u_pe/u_cpu/*}
    {mover      mesh_N/inst/u_mag/u_pe/u_mover/*}
    {xform      mesh_N/inst/u_mag/u_pe/u_xform/*}
    {sysnode    mesh_N/inst/u_mag/*}
    {dram_port  mesh_N/inst/u_mag/u_mag/u_dram/*}
    {staging    mesh_N/inst/u_mag/u_mag/g_l2.u_l2/*}
    {ilink      mesh_N/inst/u_mag/u_mag/g_ilink*}
    {mag_cdc    mesh_N/inst/g_magcdc.*}
    {router1    mesh_N/inst/r1_1/*}
    {router2    mesh_N/inst/r1_2/*}
    {cu0        mesh_N/inst/u_cu0/*}
    {cu1        mesh_N/inst/u_cu1/*}
    {cu2        mesh_N/inst/u_cu2/*}
    {cu3        mesh_N/inst/u_cu3/*}
    {vec0       mesh_N/inst/u_vec0/*}
    {vec1       mesh_N/inst/u_vec1/*}
    {mesh       mesh_N/inst/*}
    {pipe_out   pipe_N_to_*/inst/*}
    {pipe_in    pipe_*_to_N/inst/*}
    {kx_chain   xache/inst/u_kx/g_chain.g_p\[N\].*}
    {kx_home    xache/inst/u_kx/g_home\[N\].*}
    {kx_master  xache/inst/u_kx/g_m\[N\].*}
    {kx_hedge   xache/inst/u_kx/g_hedge\[N\].*}
    {stn_link   station_bus/inst/u_line/g_link\[N\]*}
    {station    station_bus/inst/u_line/g_stn\[N\].*}
    {ddr4       ddr4_N/inst/*}
    {dwc_ctrl   dwc_ctrl_N/*}
}
proc cz_bbox {fh top n label pat} {
    set cells [get_cells -quiet -hier -filter "IS_PRIMITIVE && NAME =~ $top/$pat"]
    set slx {} ; set sly {}
    array set cr {}
    set n_loc 0
    set n_left 0
    foreach loc [get_property -quiet LOC $cells] {
        if {$loc eq "" || ![regexp {_X(\d+)Y(\d+)$} $loc -> x y]} { continue }
        incr n_loc
        if {[string match SLICE_* $loc]} { lappend slx $x ; lappend sly $y }
        set r [get_property CLOCK_REGION [get_sites $loc]]
        if {![info exists cr($r)]} { set cr($r) 0 }
        incr cr($r)
        if {[regexp {^X([0-3])Y} $r]} { incr n_left }
    }
    if {!$n_loc} { puts $fh [format "  %-10s die %d: no placed cells" $label $n] ; return }
    set slx [lsort -integer $slx] ; set sly [lsort -integer $sly]
    set nx [llength $slx]
    set med - ; set p10 -
    if {$nx} {
        set med [format "X%dY%d" [lindex $slx [expr {$nx / 2}]] [lindex $sly [expr {$nx / 2}]]]
        set p10 [format "X%d-%d Y%d-%d" [lindex $slx [expr {$nx / 10}]] [lindex $slx [expr {$nx * 9 / 10}]] \
                     [lindex $sly [expr {$nx / 10}]] [lindex $sly [expr {$nx * 9 / 10}]]]
    }
    set rows {}
    foreach r [array names cr] { lappend rows [list $cr($r) $r] }
    set txt {}
    foreach row [lrange [lsort -integer -decreasing -index 0 $rows] 0 7] {
        lappend txt "[lindex $row 1]:[lindex $row 0]"
    }
    puts $fh [format "  %-10s die %d: %7d cells  median %-11s 10-90%% %-24s X0-3 %7d of %7d  regions %s" \
        $label $n [llength $cells] $med $p10 $n_left $n_loc [join $txt " "]]
}
set fh [open ${tag}_bbox.txt w]
puts $fh "block placement in $V8_DCP: primitive cells, median slice, 10-90 % slice band, placed cells in clock-region columns X0-3, clock regions (cells)"
foreach n {0 1 2 3} {
    foreach b $BLOCKS {
        lassign $b label pat
        regsub -all {N} $pat $n pat_n
        cz_bbox $fh $top $n $label $pat_n
    }
    puts $fh ""
    flush $fh
}
close $fh
report_clock_utilization -clock_roots_only -file ${tag}_clock_roots.rpt
puts "@@@ block placement in ${tag}_bbox.txt  [v8_t]"

# ---- 6. who sits inside each congestion window ------------------------------
proc cz_owner {name} {
    set o {}
    foreach s [lrange [split $name /] 1 end] {
        if {$s eq "inst"} { continue }
        lappend o $s
        if {[llength $o] == 3} { break }
    }
    return [join $o /]
}
set txt [report_design_analysis -congestion -return_string]
set wf [open ${tag}_cong_windows.tsv w]
puts $wf [join {LINE TILE0 TILE1 CELLS} \t]
set cf [open ${tag}_cong_cells.tsv w]
puts $cf [join {WINDOW OWNER CELLS} \t]
array set SEEN {}
foreach line [split $txt "\n"] {
    if {![regexp {\(([A-Z0-9_]+_X\d+Y\d+),([A-Z0-9_]+_X\d+Y\d+)\)} $line -> t0 t1]} { continue }
    set win "$t0..$t1"
    if {[info exists SEEN($win)]} { puts $wf [join [list [string trim $line] $t0 $t1 -] \t] ; continue }
    set SEEN($win) 1
    set a [get_tiles -quiet $t0] ; set b [get_tiles -quiet $t1]
    if {![llength $a] || ![llength $b]} { continue }
    set c0 [get_property COLUMN $a] ; set c1 [get_property COLUMN $b]
    set r0 [get_property ROW $a] ; set r1 [get_property ROW $b]
    if {$c0 > $c1} { lassign [list $c1 $c0] c0 c1 }
    if {$r0 > $r1} { lassign [list $r1 $r0] r0 r1 }
    set tiles [get_tiles -quiet -filter "COLUMN >= $c0 && COLUMN <= $c1 && ROW >= $r0 && ROW <= $r1"]
    set cells [get_cells -quiet -of_objects [get_sites -quiet -of_objects $tiles]]
    array unset C
    foreach nm [get_property -quiet NAME $cells] { incr C([cz_owner $nm]) }
    foreach k [lsort [array names C]] { puts $cf [join [list $win $k $C($k)] \t] }
    puts $wf [join [list [string trim $line] $t0 $t1 [llength $cells]] \t]
    flush $wf ; flush $cf
}
close $wf ; close $cf
puts "@@@ congestion owners in ${tag}_cong_cells.tsv  [v8_t]"
report_timing_summary -max_paths 20 -file ${tag}_timing_summary.rpt
puts "@@@ census done  [v8_t]"

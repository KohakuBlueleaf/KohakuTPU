# Block placement of routed checkpoints (-tclargs DCP ?DCP...?): per die, each
# block's slice bounding box, median, 10-90 % band and clock regions, plus the
# clock roots; read-only, one open per checkpoint (about 6 min each).
set here [file dirname [file normalize [info script]]]
set out [file normalize [file dirname $here]/../build]

# `[` is a glob class in a Vivado filter, so generate indices are escaped.
set BLOCKS {
    {cpu_core   mesh_N/inst/u_mag/u_pe/u_cpu/u_core/*}
    {cpu_rf     mesh_N/inst/u_mag/u_pe/u_cpu/u_core/u_rf/*}
    {cpu_all    mesh_N/inst/u_mag/u_pe/u_cpu/*}
    {mover      mesh_N/inst/u_mag/u_pe/u_mover/*}
    {xform      mesh_N/inst/u_mag/u_pe/u_xform/*}
    {dram_port  mesh_N/inst/u_mag/u_mag/u_dram/*}
    {staging    mesh_N/inst/u_mag/u_mag/g_l2.u_l2/*}
    {ilink      mesh_N/inst/u_mag/u_mag/g_ilink*}
    {il_port0   mesh_N/inst/u_mag/u_mag/g_ilink.u_sw/u_l0/*}
    {il_port1   mesh_N/inst/u_mag/u_mag/g_ilink.u_sw/u_l1/*}
    {pipe_out   pipe_N_to_*/inst/*}
    {pipe_in    pipe_*_to_N/inst/*}
    {stn_link   station_bus/inst/u_line/g_link\[N\]*}
    {mesh       mesh_N/inst/*}
    {kx_chain   xache/inst/u_kx/g_chain.g_p\[N\].*}
    {kx_home    xache/inst/u_kx/g_home\[N\].*}
    {kx_master  xache/inst/u_kx/g_m\[N\].*}
    {kx_hedge   xache/inst/u_kx/g_hedge\[N\].*}
    {station    station_bus/inst/u_line/g_stn\[N\].*}
    {ddr4       ddr4_N/inst/*}
}

proc bb_site {c} {
    set loc [get_property LOC $c]
    if {$loc eq ""} { return {} }
    if {![regexp {_X(\d+)Y(\d+)$} $loc -> x y]} { return {} }
    return [list $loc $x $y]
}

proc bb_report {fh top n label pat} {
    set cells [get_cells -quiet -hier -filter "IS_PRIMITIVE && NAME =~ $top/$pat"]
    set xs {} ; set ys {} ; set slx {} ; set sly {}
    array set cr {}
    set n_loc 0
    foreach c $cells {
        set s [bb_site $c]
        if {$s eq ""} { continue }
        incr n_loc
        lassign $s loc x y
        if {[string match SLICE_* $loc]} { lappend slx $x ; lappend sly $y }
        lappend xs $x ; lappend ys $y
        set r [get_property CLOCK_REGION [get_sites $loc]]
        if {![info exists cr($r)]} { set cr($r) 0 }
        incr cr($r)
    }
    if {!$n_loc} { puts $fh [format "  %-10s die %d: no placed cells" $label $n] ; return }
    set slx [lsort -integer $slx] ; set sly [lsort -integer $sly]
    set nx [llength $slx]
    set med "-"
    if {$nx} {
        set med [format "X%dY%d" [lindex $slx [expr {$nx / 2}]] [lindex $sly [expr {$nx / 2}]]]
        set p10 [format "X%d-%d Y%d-%d" [lindex $slx [expr {$nx / 10}]] [lindex $slx [expr {$nx * 9 / 10}]] \
                     [lindex $sly [expr {$nx / 10}]] [lindex $sly [expr {$nx * 9 / 10}]]]
    } else { set p10 "-" }
    set rows {}
    foreach r [array names cr] { lappend rows [list $cr($r) $r] }
    set txt {}
    foreach row [lrange [lsort -integer -decreasing -index 0 $rows] 0 5] {
        lappend txt "[lindex $row 1]:[lindex $row 0]"
    }
    puts $fh [format "  %-10s die %d: %6d cells  slices X%s-%s Y%s-%s  median %s  10-90%% %s  regions %s" \
        $label $n [llength $cells] [lindex $slx 0] [lindex $slx end] [lindex $sly 0] [lindex $sly end] \
        $med $p10 [join $txt " "]]
}

foreach dcp $argv {
    if {![file exists $dcp]} { puts "@@@ no checkpoint $dcp" ; continue }
    set name [file rootname [file tail $dcp]]
    regsub {_wrapper.*$} $name "" name
    puts "@@@ bbox: opening $dcp  [clock format [clock seconds] -format %H:%M:%S]"
    open_checkpoint $dcp
    set top ${name}_i
    set fh [open $out/${name}_routed_bbox.txt w]
    puts $fh "block placement in $dcp: primitive cells, slice bounding box, median slice, 10-90 % slice band, clock regions (cells)"
    foreach n {0 1 2 3} {
        foreach b $BLOCKS {
            lassign $b label pat
            regsub -all {N} $pat $n pat_n
            bb_report $fh $top $n $label $pat_n
        }
        puts $fh ""
    }
    close $fh
    report_clock_utilization -clock_roots_only -file $out/${name}_routed_clock_roots.rpt
    puts "@@@ bbox: $out/${name}_routed_bbox.txt and ${name}_routed_clock_roots.rpt  [clock format [clock seconds] -format %H:%M:%S]"
    close_design
}

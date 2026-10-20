# Unit shape on an implementation checkpoint, read-only: every USER_CLUSTER-
# tagged cell (value, whether a leaf, placed leaves under it) and every named
# block, as placed leaves per clock region, so a unit's spread and its side of
# the DDR4 column (X4) read off one table.
#   vivado -mode batch -source scripts/tcl/audit/user_clusters.tcl -tclargs <dcp> <out.rpt>
lassign $argv dcp out
open_checkpoint $dcp
set fh [open $out w]
puts $fh "checkpoint $dcp"

proc uc_shape {fh label leaves} {
    set n [llength $leaves]
    array set cr {}
    set lo 0 ; set mid 0 ; set hi 0
    foreach s [get_property -quiet LOC $leaves] {
        if {$s eq ""} { continue }
        set r [get_property CLOCK_REGION [get_sites $s]]
        incr cr($r)
        regexp {X(\d+)Y} $r -> x
        if {$x < 4} { incr lo } elseif {$x == 4} { incr mid } else { incr hi }
    }
    set rows {}
    foreach r [array names cr] { lappend rows [list $cr($r) $r] }
    set txt {}
    foreach row [lsort -integer -decreasing -index 0 $rows] { lappend txt "[lindex $row 1]:[lindex $row 0]" }
    puts $fh [format "%-58s %8d %4d  X0-3 %7d  X4 %7d  X5-7 %7d  %s" $label $n [llength $rows] $lo $mid $hi [join $txt " "]]
    flush $fh
}

set tagged [get_cells -quiet -hierarchical -filter {USER_CLUSTER != ""}]
puts $fh "cells carrying USER_CLUSTER: [llength $tagged] (leaves [llength [filter -quiet $tagged {IS_PRIMITIVE}]])"
puts $fh [format "%-58s %8s %4s  %s" "cell (value)" leaves CRs "side split, then leaves per clock region"]
foreach c $tagged {
    if {[get_property IS_PRIMITIVE $c]} { puts $fh "$c ([get_property USER_CLUSTER $c]) leaf" ; continue }
    set leaves [get_cells -quiet -hierarchical -filter "IS_PRIMITIVE && NAME =~ \"$c/*\""]
    uc_shape $fh "$c ([get_property USER_CLUSTER $c])" $leaves
}

# blocks that carry no tag: the DDR4 trunk, station, XDMA
set top [get_property NAME [lindex [get_cells -quiet -filter {NAME =~ "*_i"}] 0]]
puts $fh "\nuntagged blocks"
foreach {label pat} [list ddr4_0 "$top/ddr4_0/*" ddr4_1 "$top/ddr4_1/*" ddr4_2 "$top/ddr4_2/*" ddr4_3 "$top/ddr4_3/*" \
                         stn_0 "$top/station_bus/inst/u_line/g_stn?0?.*" stn_1 "$top/station_bus/inst/u_line/g_stn?1?.*" \
                         stn_2 "$top/station_bus/inst/u_line/g_stn?2?.*" stn_3 "$top/station_bus/inst/u_line/g_stn?3?.*" \
                         xdma "$top/xdma_0/*"] {
    uc_shape $fh $label [get_cells -quiet -hierarchical -filter "IS_PRIMITIVE && NAME =~ \"$pat\""]
}
close $fh
puts "@@@ unit shape: $out"

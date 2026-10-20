# One placement probe from an opt checkpoint under a constraint delta.
#   vivado -mode batch -source scripts/tcl/audit/place_probe.tcl -tclargs <opt.dcp> <out_dir> <dry|place> <op>...
# ops: reset (every pblock and USER_CLUSTER gone), xdc <file>, nouc (USER_CLUSTER
# cleared), hard <pblock glob>, soft <pblock glob>, threads <n>. dry stops before placing.
# Words, not k=v: vivado.bat splits its arguments at '='.
lassign $argv dcp out mode
set opsin [lrange $argv 3 end]
set ops $opsin
if {[lsearch {dry place} $mode] < 0} { error "mode $mode, want dry or place" }
file mkdir $out
open_checkpoint $dcp
set T [get_property NAME [lindex [get_cells -quiet -filter {NAME =~ "*_i"}] 0]]
set ROW0 {0 0 1 4 2 8 3 12}
set CMP {u_cu0 u_cu1 u_cu2 u_cu3 u_vec0 u_vec1}
set SYS {u_mag/u_mag u_mag/u_pe/u_cpu u_mag/u_pe/u_mover u_mag/u_pe/u_xform}

proc clear_uc {} {
    foreach c [get_cells -quiet -hierarchical -filter {USER_CLUSTER != ""}] { reset_property USER_CLUSTER $c }
}
while {[llength $ops]} {
    set ops [lassign $ops k]
    if {[lsearch {xdc hard soft threads} $k] >= 0} { set ops [lassign $ops v] ; set op "$k $v" } else { set op $k }
    switch $k {
        reset {
            delete_pblocks -hier [get_pblocks -quiet -filter {PARENT == ROOT}]
            clear_uc
        }
        xdc   { read_xdc $v }
        nouc  { clear_uc }
        hard  { set_property IS_SOFT false [get_pblocks $v] }
        soft  { set_property IS_SOFT true [get_pblocks $v] }
        threads { set_param general.maxThreads $v }
        default { error "op $op, want reset, xdc, nouc, hard, soft, threads" }
    }
    puts "@@@ op $op"
}
set fh [open $out/constraints.rpt w]
puts $fh "checkpoint $dcp\nops $opsin"
foreach pb [lsort [get_pblocks]] {
    puts $fh [format "pblock %-10s soft %-5s parent %-8s cells %8d  %s" $pb [get_property IS_SOFT $pb] \
        [get_property PARENT $pb] [llength [get_cells -quiet -of $pb]] [get_property GRID_RANGES $pb]]
}
set ucs [get_cells -quiet -hierarchical -filter {USER_CLUSTER != ""}]
set tags [expr {[llength $ucs] ? [lsort -unique [get_property USER_CLUSTER $ucs]] : {}}]
puts $fh "user_cluster cells [llength $ucs] tags [llength $tags]"
foreach {d y} $ROW0 {
    foreach u [concat $CMP $SYS] {
        set c [get_cells -quiet $T/mesh_$d/inst/$u]
        puts $fh [format "unit d%d %-22s pblock %-10s cluster %s" $d $u [get_pblocks -quiet -of $c] \
            [get_property -quiet USER_CLUSTER $c]]
    }
}
close $fh
puts "@@@ constraints -> $out/constraints.rpt"
if {$mode eq "dry"} { return }

set t0 [clock seconds]
place_design -directive Default
puts "@@@ placed in [expr {([clock seconds] - $t0) / 60}] min"
write_checkpoint -force $out/placed.dcp
report_timing_summary -no_detailed_paths -file $out/timing_summary.rpt
report_utilization -slr -file $out/util_slr.rpt
report_utilization -pblocks [get_pblocks] -file $out/util_pblocks.rpt

proc shape {fh label leaves} {
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
    puts $fh [format "%-34s %7d %3d  X0-3 %6d  X4 %6d  X5-7 %6d  %s" $label [llength $leaves] \
        [llength $rows] $lo $mid $hi [join $txt " "]]
    flush $fh
}
set fh [open $out/units.rpt w]
puts $fh "checkpoint $dcp  ops $opsin"
puts $fh [format "%-34s %7s %3s  %s" unit leaves CRs "side split, leaves per clock region"]
foreach {d y} $ROW0 {
    foreach u [concat $CMP $SYS] {
        shape $fh "d$d $u" [get_cells -quiet -hierarchical -filter "IS_PRIMITIVE && NAME =~ \"$T/mesh_$d/inst/$u/*\""]
    }
}
close $fh
puts "@@@ probe done: $out"

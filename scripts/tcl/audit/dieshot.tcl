# Die-shot dump of an implementation checkpoint, read-only, for
# scripts/py/docs/dieshot.py:
#   vivado -mode batch -source scripts/tcl/audit/dieshot.tcl -tclargs <dcp> <out_dir>
# bg.txt          every fabric site: name kind rpm_x rpm_y
# cr.txt          every clock region: name slr rpm_x_min rpm_x_max rpm_y_min rpm_y_max
# cells_hier.txt  leaves per (first six hierarchy levels, site)

lassign $argv dcp out
set_param tcl.collectionResultDisplayLimit 0
open_checkpoint $dcp
set t0 [clock seconds]

set ss [get_sites -filter {NAME =~ SLICE_* || NAME =~ RAMB36_* || NAME =~ DSP48E2_* || NAME =~ URAM288_*}]
set f [open $out/bg.txt w]
set xy [dict create]
foreach n [get_property NAME $ss] t [get_property SITE_TYPE $ss] x [get_property RPM_X $ss] y [get_property RPM_Y $ss] {
    puts $f "$n $t $x $y"
    dict set xy $n 1
}
close $f

set f [open $out/cr.txt w]
foreach cr [get_clock_regions] {
    set s [get_sites -quiet -of_objects $cr]
    if {[llength $s] == 0} { continue }
    set xs [lsort -integer [get_property RPM_X $s]]
    set ys [lsort -integer [get_property RPM_Y $s]]
    puts $f "[get_property NAME $cr] [get_property SLR_INDEX [get_slrs -of_objects $cr]] [lindex $xs 0] [lindex $xs end] [lindex $ys 0] [lindex $ys end]"
}
close $f

set cs [get_cells -hierarchical -filter {IS_PRIMITIVE && PRIMITIVE_LEVEL != INTERNAL}]
set cnt [dict create]
set off 0
foreach s [get_property SITE $cs] n [get_property NAME $cs] {
    if {$s eq "" || ![dict exists $xy $s]} { incr off ; continue }
    dict incr cnt "[join [lrange [split $n /] 0 5] /] $s"
}
set f [open $out/cells_hier.txt w]
dict for {k v} $cnt { puts $f "$k $v" }
close $f
puts "@@@ dieshot: [llength $ss] sites, [llength $cs] leaves ($off off the fabric) in $out ([expr {[clock seconds] - $t0}] s)"

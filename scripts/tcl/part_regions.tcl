# Site counts per clock region of a part, and per die and column, so a pblock
# is sized before a run: slices (x8 = LUT), RAMB36, URAM288, DSP48E2.
# get_clock_regions needs an open design, and synth_design on an in-memory
# project with a stub fails inside Vivado 2024.2, so the design is any
# checkpoint of the part (about 6 min to open a routed one).
#   vivado -mode batch -source scripts/tcl/part_regions.tcl -tclargs DCP OUTFILE
set dcp [lindex $argv 0]
set out [lindex $argv 1]
open_checkpoint $dcp
set part [get_property PART [current_project]]
set fh [open $out w]
puts $fh "site counts of $part per clock region: slices, RAMB36, URAM288, DSP48E2"
puts $fh [format "%-8s %7s %7s %7s %7s" region slice ramb36 uram dsp]
array set T {}
set xmax 0 ; set ymax 0
foreach r [get_clock_regions] {
    regexp {X(\d+)Y(\d+)} [get_property NAME $r] -> x y
    set s [llength [get_sites -quiet -of $r -filter {SITE_TYPE =~ SLICE*}]]
    set b [llength [get_sites -quiet -of $r -filter {SITE_TYPE =~ RAMB*36*}]]
    set u [llength [get_sites -quiet -of $r -filter {SITE_TYPE == URAM288}]]
    set d [llength [get_sites -quiet -of $r -filter {SITE_TYPE == DSP48E2}]]
    set T($x,$y) [list $s $b $u $d]
    if {$x > $xmax} { set xmax $x }
    if {$y > $ymax} { set ymax $y }
}
for {set y 0} {$y <= $ymax} {incr y} {
    for {set x 0} {$x <= $xmax} {incr x} {
        if {![info exists T($x,$y)]} { continue }
        puts $fh [format "X%dY%-5d %7d %7d %7d %7d" $x $y {*}$T($x,$y)]
    }
}
puts $fh ""
puts $fh "per column, all rows"
for {set x 0} {$x <= $xmax} {incr x} {
    set c {0 0 0 0}
    for {set y 0} {$y <= $ymax} {incr y} {
        if {![info exists T($x,$y)]} { continue }
        set c [lmap a $c b $T($x,$y) {expr {$a + $b}}]
    }
    puts $fh [format "X%-7d %7d %7d %7d %7d" $x {*}$c]
}
puts $fh ""
puts $fh "per die (4 rows) and column"
set rows_per_die 4
for {set die 0} {$die * $rows_per_die <= $ymax} {incr die} {
    set tot {0 0 0 0}
    for {set x 0} {$x <= $xmax} {incr x} {
        set c {0 0 0 0}
        for {set y [expr {$die * $rows_per_die}]} {$y < ($die + 1) * $rows_per_die} {incr y} {
            if {![info exists T($x,$y)]} { continue }
            set c [lmap a $c b $T($x,$y) {expr {$a + $b}}]
        }
        set tot [lmap a $tot b $c {expr {$a + $b}}]
        puts $fh [format "die %d X%-4d %7d %7d %7d %7d" $die $x {*}$c]
    }
    puts $fh [format "die %d all  %7d %7d %7d %7d" $die {*}$tot]
}
close $fh
puts "@@@ part regions in $out"

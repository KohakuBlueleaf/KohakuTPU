# Slice X/Y range of every clock region of the part, for placement maps.
#   vivado -mode batch -source scripts/tcl/audit/region_slices.tcl -tclargs <part> <out.tsv>
lassign $argv part out
create_project -in_memory -part $part
link_design -part $part
set fh [open $out w]
puts $fh "region\tx0\tx1\ty0\ty1"
foreach cr [get_clock_regions] {
    set xs {} ; set ys {}
    foreach s [get_sites -quiet -of_objects $cr -filter {SITE_TYPE =~ SLICE*}] {
        regexp {X(\d+)Y(\d+)$} $s -> x y
        lappend xs $x ; lappend ys $y
    }
    if {![llength $xs]} { continue }
    set xs [lsort -integer $xs] ; set ys [lsort -integer $ys]
    puts $fh "$cr\t[lindex $xs 0]\t[lindex $xs end]\t[lindex $ys 0]\t[lindex $ys end]"
}
close $fh
puts "@@@ region slices -> $out"

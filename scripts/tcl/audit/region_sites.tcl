# Site range of every clock region of the part, per site type.
#   vivado -mode batch -source scripts/tcl/audit/region_sites.tcl -tclargs <part> <out.tsv>
lassign $argv part out
create_project -in_memory -part $part
link_design -part $part
set fh [open $out w]
puts $fh "region\ttype\tcount\tx0\tx1\ty0\ty1"
foreach cr [get_clock_regions] {
    # By site name: block RAM sites carry types like RAMBFIFO36, not RAMB36.
    foreach type {SLICE DSP48E2 RAMB36 RAMB18 URAM288} {
        set xs {} ; set ys {}
        foreach s [get_sites -quiet -of_objects $cr -filter "NAME =~ ${type}_X*"] {
            if {![regexp "^${type}_X(\\d+)Y(\\d+)$" $s -> x y]} { continue }
            lappend xs $x ; lappend ys $y
        }
        if {![llength $xs]} { continue }
        set xs [lsort -integer $xs] ; set ys [lsort -integer $ys]
        puts $fh "$cr\t$type\t[llength $xs]\t[lindex $xs 0]\t[lindex $xs end]\t[lindex $ys 0]\t[lindex $ys end]"
    }
}
close $fh
puts "@@@ region sites -> $out"

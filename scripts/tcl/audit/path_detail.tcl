# Full report_timing of named start -> end cell pairs, appended to one file.
#   vivado -mode batch -source scripts/tcl/audit/path_detail.tcl -tclargs <dcp> <out.rpt> <pairs.txt>
# pairs.txt: "label<TAB>start cell<TAB>end cell" (full hierarchical names).
lassign $argv dcp out pairs
open_checkpoint $dcp
file delete -force $out
set ch [open $pairs r]
foreach line [split [read $ch] "\n"] {
    if {[string trim $line] eq "" || [string index $line 0] eq "#"} { continue }
    lassign [split $line \t] label s e
    set sc [get_cells -quiet $s] ; set ec [get_cells -quiet $e]
    if {![llength $sc] || ![llength $ec]} { puts "@@@ $label: cells not found" ; continue }
    set fh [open $out a] ; puts $fh "### $label" ; close $fh
    report_timing -from $sc -to $ec -max_paths 1 -input_pins -append -file $out
}
close $ch
puts "@@@ path_detail done -> $out"

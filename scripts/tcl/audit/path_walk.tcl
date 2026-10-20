# Worst setup path of named register classes on every die, cell by cell:
# site, tile column/row, cell type, fanout of the net it drives.
#   vivado -mode batch -source scripts/tcl/audit/path_walk.tcl -tclargs <dcp> <out.tsv> <classes.txt>
# classes.txt: one class a line, "label<TAB>start cell glob<TAB>end cell glob",
# "%d" standing for the mesh index.
lassign $argv dcp out cls
open_checkpoint $dcp
set T [get_property NAME [lindex [get_cells -filter {NAME =~ "*_i"}] 0]]
set fh [open $out w]
puts $fh [join {label die slack levels step cell ref site col row fanout} \t]
set ch [open $cls r]
foreach line [split [read $ch] "\n"] {
    if {[string trim $line] eq "" || [string index $line 0] eq "#"} { continue }
    lassign [split $line \t] label sp ep
    foreach d {0 1 2 3} {
        set s [get_cells -quiet -hier -filter "NAME =~ \"$T/[format $sp $d]\""]
        set e [get_cells -quiet -hier -filter "NAME =~ \"$T/[format $ep $d]\""]
        if {![llength $s] || ![llength $e]} { puts "@@@ $label die $d: no cells ([llength $s] / [llength $e])" ; continue }
        set p [get_timing_paths -quiet -setup -from $s -to $e -max_paths 1]
        if {![llength $p]} { puts "@@@ $label die $d: no path" ; continue }
        set slack [get_property SLACK $p]
        set lv [get_property LOGIC_LEVELS $p]
        set i 0
        foreach c [get_cells -of_objects $p] {
            set loc [get_property LOC $c]
            set col - ; set row -
            if {$loc ne ""} {
                set t [lindex [get_tiles -of_objects [get_sites $loc]] 0]
                set col [get_property COLUMN $t] ; set row [get_property ROW $t]
            }
            set fo -
            set op [get_pins -quiet -of_objects $c -filter {DIRECTION == OUT}]
            set nets [get_nets -quiet -of_objects $op]
            if {[llength $nets]} {
                set fo 0
                foreach n $nets { set f [expr {[get_property FLAT_PIN_COUNT $n] - 1}] ; if {$f > $fo} { set fo $f } }
            }
            puts $fh [join [list $label $d $slack $lv $i [get_property NAME $c] [get_property REF_NAME $c] $loc $col $row $fo] \t]
            incr i
        }
        flush $fh
    }
}
close $ch
close $fh
puts "@@@ path_walk done -> $out"

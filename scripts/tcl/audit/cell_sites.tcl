# Every placed primitive cell of a checkpoint as "site<TAB>cell name", one line each.
#   vivado -mode batch -source scripts/tcl/audit/cell_sites.tcl -tclargs <dcp> <out.tsv>
lassign $argv dcp out
open_checkpoint $dcp
set t0 [clock seconds]
set cells [get_cells -hierarchical -filter {IS_PRIMITIVE}]
set names [get_property NAME $cells]
set locs [get_property LOC $cells]
set fh [open $out w]
fconfigure $fh -buffering full -buffersize 4194304
set n 0
foreach nm $names loc $locs {
    if {$loc eq ""} { continue }
    puts $fh "$loc\t$nm"
    incr n
}
close $fh
puts "@@@ cell_sites $n placed of [llength $names] primitives in [expr {[clock seconds] - $t0}] s -> $out"

# Per-hierarchy utilisation from a PLACED checkpoint, plus each top-level
# block's SLR spread. OOC_DCP is the checkpoint, OOC_OUT the report prefix.

open_checkpoint $::env(OOC_DCP)

report_utilization -hierarchical -hierarchical_depth 3 \
                   -file $::env(OOC_OUT)_hier.rpt

# Which SLR each top-level block's leaf cells landed in. get_cells on the
# children only, then their sites -- a per-primitive sweep is far too slow.
set fh [open $::env(OOC_OUT)_slr.txt w]
foreach top [get_cells -quiet *] {
    foreach sub [get_cells -quiet $top/*] {
        set leaves [get_cells -quiet -hier -filter "IS_PRIMITIVE" $sub/*]
        if {[llength $leaves] == 0} continue
        array unset n
        foreach s [get_sites -quiet -of_objects $leaves] {
            set slr [get_property -quiet NAME [get_slrs -quiet -of_objects $s]]
            if {$slr eq ""} continue
            incr n($slr)
        }
        set out ""
        foreach k [lsort [array names n]] { append out "$k=$n($k) " }
        if {$out ne ""} { puts $fh [format "%-60s %s" $sub $out] }
    }
}
close $fh
puts "@@@ DONE $::env(OOC_OUT)"

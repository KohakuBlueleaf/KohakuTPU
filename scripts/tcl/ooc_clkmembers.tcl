# WHICH REGISTERS ARE ON WHICH CLOCK, read from the elaborated design.
# OOC_DCP is a repo-relative checkpoint; OOC_DEPTH rolls up (default 4).

set root C:/Users/apoll/Desktop/code/Project/KohakuTPU
set depth [expr {[info exists ::env(OOC_DEPTH)] ? $::env(OOC_DEPTH) : 4}]
open_checkpoint $root/$::env(OOC_DCP)

foreach c [get_clocks] {
    set nm   [get_property NAME $c]
    set regs [all_registers -clock $c]
    puts [format "@@@ ===== %s : %d registers =====" $nm [llength $regs]]

    array unset cnt
    foreach r $regs {
        set parts [split [get_property NAME $r] /]
        # drop the leaf; roll the rest up to `depth` levels
        set path [lrange $parts 0 end-1]
        if {[llength $path] > $depth} { set path [lrange $path 0 [expr {$depth-1}]] }
        set key [join $path /]
        if {$key eq ""} { set key "(top)" }
        if {[info exists cnt($key)]} { incr cnt($key) } else { set cnt($key) 1 }
    }

    foreach k [lsort [array names cnt]] {
        puts [format "@@@ %8d  %s" $cnt($k) $k]
    }
}
puts "@@@ DONE"

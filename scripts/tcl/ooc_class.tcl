# Fmax for EVERY clock, queried per clock. A single sampled sweep only reports
# the clocks that appear in the worst N, so the other domain silently vanished.

proc ooc_classify {{npaths 4000}} {
    foreach c [get_clocks] {
        set nm [get_property NAME $c]
        set ps [get_timing_paths -to $c -max_paths $npaths -nworst $npaths -setup]
        if {[llength $ps] == 0} { puts [format "@@@ %-8s no paths" $nm]; continue }

        set wsl 1e9
        set rep {}
        foreach p $ps {
            set sl [get_property SLACK $p]
            if {$sl < $wsl} {
                set wsl $sl
                set rep [list [get_property REQUIREMENT $p] \
                              [get_property LOGIC_LEVELS $p] \
                              [get_property STARTPOINT_PIN $p] \
                              [get_property ENDPOINT_PIN $p]]
            }
        }
        lassign $rep req lvl sp ep
        set d [expr {$req - $wsl}]
        set f [expr {$d > 0 ? 1000.0 / $d : 0.0}]
        puts [format "@@@ %-8s %6.1f MHz  slack %+.3f  req %.3f  lvl %-3s %s -> %s" \
                  $nm $f $wsl $req $lvl $sp $ep]
    }
}

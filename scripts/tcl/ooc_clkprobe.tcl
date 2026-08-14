# Why a derived clock's domain fails: is it logic, or is it the clock tree?
# OOC_DCP is a repo-relative checkpoint.

set root C:/Users/apoll/Desktop/code/Project/KohakuTPU
open_checkpoint $root/$::env(OOC_DCP)

foreach c [get_clocks] {
    set nm [get_property NAME $c]
    set nets [get_nets -quiet -of_objects $c]
    puts [format "@@@ CLK %-8s period %6.3f  gen %s  nets %d" \
              $nm [get_property PERIOD $c] [get_property IS_GENERATED $c] \
              [llength $nets]]
    foreach n $nets {
        set drv [get_cells -quiet -of_objects [get_pins -quiet -leaf -filter {DIRECTION == OUT} -of_objects $n]]
        puts [format "@@@     net %-28s fanout %-6s route %s  driver %s" \
                  [get_property NAME $n] [get_property FLAT_PIN_COUNT $n] \
                  [get_property ROUTE_STATUS $n] \
                  [expr {[llength $drv] ? [get_property REF_NAME [lindex $drv 0]] : "port"}]]
    }
}

# Skew is what tells logic apart from clocking: a whole domain failing setup AND
# hold at once is a clock tree, never a datapath.
set names {}
foreach c [get_clocks] { lappend names [get_property NAME $c] }
foreach f $names { foreach t $names {
    set ps [get_timing_paths -quiet -from [get_clocks $f] -to [get_clocks $t] \
                             -max_paths 1 -setup]
    if {![llength $ps]} { continue }
    set p [lindex $ps 0]
    puts [format "@@@ %s -> %s  setup slack %+.3f  req %.3f  skew %+.3f  datapath %.3f  lvl %s" \
              $f $t [get_property SLACK $p] [get_property REQUIREMENT $p] \
              [get_property SKEW $p] [get_property DATAPATH_DELAY $p] \
              [get_property LOGIC_LEVELS $p]]
    puts [format "@@@     %s -> %s" [get_property STARTPOINT_PIN $p] \
              [get_property ENDPOINT_PIN $p]]
    set ph [get_timing_paths -quiet -from [get_clocks $f] -to [get_clocks $t] \
                             -max_paths 1 -hold]
    if {[llength $ph]} {
        set h [lindex $ph 0]
        puts [format "@@@     hold slack %+.3f  skew %+.3f" \
                  [get_property SLACK $h] [get_property SKEW $h]]
    }
    # The DATA net the startpoint drives, not the clock net its /C pin sits on:
    # a reset or enable tree reports as a datapath and only fanout says so.
    set sc [get_cells -quiet -of_objects \
                [get_pins -quiet [get_property STARTPOINT_PIN $p]]]
    foreach q [get_pins -quiet -of_objects $sc -filter {DIRECTION == OUT}] {
        foreach n [get_nets -quiet -of_objects $q] {
            puts [format "@@@     data net %-46s fanout %s" \
                      [get_property NAME $n] [get_property FLAT_PIN_COUNT $n]]
        }
    }
}}

# SKEW WITHIN ONE DOMAIN. A global net should be under ~0.2 ns; anything near a
# nanosecond means the tree is spread and no datapath change will fix it.
foreach c [get_clocks] {
    set nm [get_property NAME $c]
    set worst 0.0
    foreach p [get_timing_paths -quiet -from $c -to $c -max_paths 200 -setup] {
        set s [get_property SKEW $p]
        if {abs($s) > abs($worst)} { set worst $s }
    }
    puts [format "@@@ SKEW %-12s worst over 200 intra paths %+.3f ns" $nm $worst]
}
puts "@@@ DONE2"
puts "@@@ DONE"

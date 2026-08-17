# Launch or report the v6 BD runs, SEPARATELY, so a multi-hour implementation
# never sits inside one tool call.  -tclargs launch | impl | report

set root [file normalize [file join [file dirname [info script]] .. ..]]
set proj C:/Users/apoll/Desktop/vivado/v6_bus/v6bus.xpr
set what [lindex $argv 0]
if {$what eq ""} { set what report }

open_project $proj

if {$what eq "launch"} {
    reset_run synth_1
    launch_runs synth_1 -jobs 8
    puts "@@@ launched synth_1"
    exit 0
}

if {$what eq "impl"} {
    launch_runs impl_1 -to_step write_bitstream -jobs 8
    puts "@@@ launched impl_1"
    exit 0
}

foreach r {synth_1 impl_1} {
    set run [get_runs -quiet $r]
    if {[llength $run] == 0} { continue }
    puts "@@@ $r status [get_property STATUS $run] progress [get_property PROGRESS $run]"
}

if {[get_property PROGRESS [get_runs impl_1]] eq "100%"} {
    open_run impl_1
    set clks [get_clocks -quiet]
    puts "@@@ clocks: [llength $clks]"
    if {[llength $clks] < 10} {
        error "only [llength $clks] clocks -- routed UNTIMED, any WNS is meaningless"
    }
    source [file join $root scripts tcl ooc_class.tcl]
    ooc_classify 2000
    puts "@@@ WNS [get_property SLACK [get_timing_paths -max_paths 1 -setup]]"
    ooc_util
    report_utilization -slr -file $root/build/v6bus_util.rpt
    puts "@@@ utilization $root/build/v6bus_util.rpt"
}

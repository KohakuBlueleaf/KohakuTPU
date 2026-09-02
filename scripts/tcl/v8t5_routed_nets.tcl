# v8t5 ROUTED die-spanning nets, read-only: every block-design-level signal
# net (and the debug hub's) with the dies its pins sit in; a net touching three
# or four dies, or two dies that are not neighbours, is listed by name.
#   vivado -mode batch -source scripts/tcl/v8t5_routed_nets.tcl
set here [file dirname [file normalize [info script]]]
source $here/v8t5/00_config.tcl
set dcp $proj_dir/${design_name}.runs/impl_1/${design_name}_wrapper_routed.dcp
if {![file exists $dcp]} { error "no routed checkpoint at $dcp" }
open_checkpoint $dcp
set out $root/build/${design_name}_routed_sll_nets.txt
set fh [open $out w]
puts $fh "block-design-level nets by the dies they touch (TYPE == SIGNAL; clocks excluded)"
array set cnt {}
foreach pat [list ${design_name}_i/* dbg_hub/*] {
    foreach n [get_nets -quiet $pat -filter {TYPE == SIGNAL}] {
        set ids {}
        foreach x [get_slrs -quiet -of_objects $n] {
            regexp {SLR(\d)} [get_property NAME $x] -> d
            lappend ids $d
        }
        set ids [lsort -integer -unique $ids]
        if {[llength $ids] < 2} { continue }
        set key [join $ids ,]
        incr cnt($key)
        set span [expr {[lindex $ids end] - [lindex $ids 0]}]
        if {[llength $ids] >= 3 || $span >= 2} {
            set pins [get_pins -quiet -leaf -of_objects $n]
            set drv  [lindex [get_property NAME [filter $pins {DIRECTION == OUT}]] 0]
            puts $fh [format "dies %-8s pins %5d  driver %s  net %s" $key [llength $pins] $drv [get_property NAME $n]]
        }
    }
}
foreach k [lsort -dictionary [array names cnt]] { puts $fh "count dies $k : $cnt($k)" }
close $fh
puts "@@@ die-spanning nets in $out"
# The methodology report's TIMING-9/10 (a crossing with no synchronizer, a
# synchronizer without ASYNC_REG) name no cell; report_cdc does.
report_cdc -details -file $root/build/${design_name}_routed_cdc.rpt
puts "@@@ cdc in $root/build/${design_name}_routed_cdc.rpt"

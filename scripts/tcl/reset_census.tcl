# Which registers carry a driven synchronous reset, grouped by module.
#   vivado -mode batch -source reset_census.tcl -tclargs <checkpoint.dcp> [depth]

# Reset extraction happens in synthesis, so the SYNTH checkpoint is enough and
# opens far faster than a routed one.

set dcp   [lindex $argv 0]
set depth [lindex $argv 1]
if {$depth eq ""} { set depth 4 }

open_checkpoint $dcp

# -hierarchical -filter REF_PIN_NAME returns EMPTY here; go via the cells.
set fds [get_cells -quiet -hierarchical -filter {REF_NAME =~ FD*}]
puts "@@@ flops: [llength $fds]"
set pins [get_pins -quiet -of_objects $fds -filter {REF_PIN_NAME == R}]
puts "@@@ R pins total: [llength $pins]"

array set cnt {}
set driven 0
foreach p $pins {
    set n [get_nets -quiet -of_objects $p]
    if {$n eq ""} { continue }
    # A tied-off R is free; only a real net costs a control set.
    if {[get_property -quiet TYPE $n] eq "POWER" ||
        [get_property -quiet TYPE $n] eq "GROUND"} { continue }
    incr driven
    set parts [split [get_property PARENT_CELL $p] /]
    set key [join [lrange $parts 0 [expr {$depth - 1}]] /]
    incr cnt($key)
}
puts "@@@ R pins driven by a real net: $driven"

foreach k [lsort -command {apply {{a b} {expr {$::cnt($b) - $::cnt($a)}}}} \
               [array names cnt]] {
    puts [format "@@@ %8d  %s" $cnt($k) $k]
}

# Ours only, by register. XPM FIFO pointers and reset chains legitimately hold
# reset, so counting them buries the handful of datapath regs worth fixing.
array set mine {}
foreach p $pins {
    set n [get_nets -quiet -of_objects $p]
    if {$n eq ""} { continue }
    set t [get_property -quiet TYPE $n]
    if {$t eq "POWER" || $t eq "GROUND"} { continue }
    set c [get_property PARENT_CELL $p]
    if {[string match "*xpm_*" $c] || [string match "*gnuram_async_fifo*" $c]} {
        continue
    }
    # One entry per register, not per bit: strip the index and the _reg suffix.
    regsub {\[\d+\]$} $c "" c
    regsub {_rep\w*$} $c "" c
    incr mine($c)
}
puts "@@@ --- ours (no XPM), by register ---"
foreach k [lsort -command {apply {{a b} {expr {$::mine($b) - $::mine($a)}}}} \
               [array names mine]] {
    if {$mine($k) < 8} { continue }
    puts [format "@@@ MINE %6d  %s" $mine($k) $k]
}
exit 0

# Per-module die-crossing spread on an implementation checkpoint, read-only:
# every net with CROSSING_SLRS, its owning scope, and the tile columns of its
# driver and loads on each side, binned into 16 bins across the die width (the
# router's SLL demand table uses the same 16 bins), plus the rows each end sits
# at as a distance from the boundary it crosses.
#   vivado -mode batch -source scripts/tcl/audit/sll_spread.tcl ?-tclargs dcp <checkpoint> tag placed cfg v9t3?
set here [file dirname [file dirname [file normalize [info script]]]]
set V9_CFG v9t3
foreach {k v} $argv { if {$k eq "cfg"} { set V9_CFG $v } }
source $here/$V9_CFG/00_config.tcl
foreach {k v} $argv {
    switch -- $k { dcp { set V8_DCP $v } tag { set V8_TAG $v } }
}
if {![info exists V8_DCP]} {
    set V8_DCP $proj_dir/${design_name}.runs/impl_1/${design_name}_wrapper_physopt.dcp
}
if {![info exists V8_TAG]} { set V8_TAG placed }
open_checkpoint $V8_DCP
set top ${design_name}_i
set tag $root/build/${design_name}_${V8_TAG}_sll
proc sp_t {} { return [clock format [clock seconds] -format %H:%M:%S] }

proc sp_owner {name} {
    set o {}
    foreach s [lrange [split $name /] 1 end-1] {
        if {$s eq "inst"} { continue }
        lappend o $s
        if {[llength $o] == 3} { break }
    }
    return [join $o /]
}

# The die's tile-column span, from every site's tile.
set cols [get_property COLUMN [get_tiles -quiet -filter {TYPE =~ INT*}]]
set cmin [tcl::mathfunc::min {*}$cols] ; set cmax [tcl::mathfunc::max {*}$cols]
set span [expr {$cmax - $cmin + 1}]
puts "@@@ INT tile columns $cmin..$cmax  [sp_t]"

set nets [get_nets -quiet -hier -top_net_of_hierarchical_group -filter {CROSSING_SLRS != ""}]
puts "@@@ crossing nets: [llength $nets]  [sp_t]"
set raw [open ${tag}_nets.tsv w]
puts $raw [join {NET OWNER SLRS DRV_SLR DRV_COL DRV_ROW LOAD_SLR LOAD_COL LOAD_ROW} \t]
array set H {}
array set N {}
foreach n $nets {
    set lp [get_pins -quiet -leaf -of_objects $n]
    set drv [get_pins -quiet $lp -filter {DIRECTION == OUT}]
    if {[llength $drv] != 1} { continue }
    set dc [get_cells -quiet -of_objects $drv]
    set dl [get_property -quiet LOC $dc]
    if {$dl eq ""} { continue }
    set ds [get_sites -quiet $dl]
    set dt [get_tiles -quiet -of_objects $ds]
    set dslr [get_property NAME [get_slrs -of_objects $ds]]
    set dcol [get_property COLUMN $dt] ; set drow [get_property ROW $dt]
    set own [sp_owner [get_property NAME $drv]]
    set slrs [get_property CROSSING_SLRS $n]
    foreach l [get_pins -quiet $lp -filter {DIRECTION == IN}] {
        set lc [get_cells -quiet -of_objects $l]
        set ll [get_property -quiet LOC $lc]
        if {$ll eq ""} { continue }
        set ls [get_sites -quiet $ll]
        set lslr [get_property NAME [get_slrs -of_objects $ls]]
        if {$lslr eq $dslr} { continue }
        set lt [get_tiles -quiet -of_objects $ls]
        set lcol [get_property COLUMN $lt] ; set lrow [get_property ROW $lt]
        puts $raw [join [list [get_property NAME $n] $own $slrs $dslr $dcol $drow $lslr $lcol $lrow] \t]
        set b0 [expr {min(15, ($dcol - $cmin) * 16 / $span)}]
        set b1 [expr {min(15, ($lcol - $cmin) * 16 / $span)}]
        set key "$own\t$dslr>$lslr"
        incr N($key)
        incr H($key\t$b0\t$b1)
        break
    }
}
close $raw
set fh [open ${tag}_spread.txt w]
puts $fh "die-crossing nets by driver scope and direction: count, then the driver's and the first cross-die load's"
puts $fh "position as a 16-bin histogram over the die width (bin 0 = leftmost tile column $cmin, 15 = $cmax)"
foreach key [lsort [array names N]] {
    set hd [lrepeat 16 0] ; set hl [lrepeat 16 0]
    foreach hk [array names H "$key\t*"] {
        lassign [lrange [split $hk \t] end-1 end] b0 b1
        lset hd $b0 [expr {[lindex $hd $b0] + $H($hk)}]
        lset hl $b1 [expr {[lindex $hl $b1] + $H($hk)}]
    }
    puts $fh [format "%-60s %6d\n    drv  %s\n    load %s" [string map {"\t" "  "} $key] $N($key) $hd $hl]
}
close $fh
puts "@@@ spread in ${tag}_spread.txt  [sp_t]"

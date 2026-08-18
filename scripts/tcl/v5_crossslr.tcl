# What actually crosses an SLR boundary in v5, and where each DDR controller
# sits relative to the mesh it serves.

open_project C:/Users/apoll/Desktop/vivado/JTAG-DMA-test/JTAG-DMA-test.xpr
open_run impl_1 -name impl_1
puts "@@@ opened [get_property PROGRESS [get_runs impl_1]]"

proc try {label script} {
    if {[catch {uplevel 1 $script} m]} { puts "@@@ $label FAILED $m" }
}

proc slr_of {cell} {
    set st [get_sites -quiet -of_objects $cell]
    if {$st eq ""} { return "?" }
    return [get_slrs -quiet -of_objects [get_clock_regions -quiet -of_objects [lindex $st 0]]]
}

# --- where the big blocks landed ---------------------------------------------
try blocks {
    foreach n {mesh_0 mesh_1 mesh_2 mesh_3 ddr4_0 ddr4_1 ddr4_2 ddr4_3
               root_smc xdma_0 leaf_smc_0 leaf_smc_2 leaf_smc_3} {
        set c [get_cells -quiet "multimesh_v5_i/$n"]
        if {$c eq ""} { puts "@@@ block $n MISSING" ; continue }
        set leaves [get_cells -quiet -hier -filter "NAME =~ multimesh_v5_i/$n/*" ]
        array unset s ; array set s {}
        foreach l [lrange $leaves 0 3000] { incr s([slr_of $l]) }
        set out ""
        foreach {k v} [array get s] { append out " $k=$v" }
        puts "@@@ block $n leaves [llength $leaves] slr_sample$out"
    }
}

# --- everything sitting on a Laguna site IS an SLL crossing ------------------
try laguna {
    set lag [get_sites -quiet -filter {SITE_TYPE =~ LAGUNA*}]
    puts "@@@ laguna sites [llength $lag]"
    set cells [get_cells -quiet -of_objects $lag]
    puts "@@@ laguna cells [llength $cells]"
    array set tally {}
    foreach c $cells {
        set p [split [get_property NAME $c] /]
        incr tally([join [lrange $p 0 2] /])
    }
    set rows {}
    foreach {k v} [array get tally] { lappend rows [list $v $k] }
    foreach r [lsort -integer -index 0 -decreasing $rows] {
        puts "@@@ crossing [lindex $r 0] [lindex $r 1]"
    }
}

# --- pblock occupancy, mesh_0 (SLR0) against mesh_2 (SLR3) -------------------
try pblocks {
    foreach pb [get_pblocks -quiet] {
        set cs [get_cells -quiet -of_objects $pb]
        puts "@@@ pblock $pb cells [llength $cs] range [get_property GRID_RANGES $pb]"
    }
}

puts "@@@ v5_crossslr done"

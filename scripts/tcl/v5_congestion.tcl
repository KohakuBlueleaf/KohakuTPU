# Attribute v5's congestion-7 window (INT_X64..127 Y733..830), and find what
# drives noc_out_data_reg/R when the RTL never resets it.

open_project C:/Users/apoll/Desktop/vivado/JTAG-DMA-test/JTAG-DMA-test.xpr
open_run impl_1 -name impl_1
puts "@@@ opened [get_property PROGRESS [get_runs impl_1]]"

proc try {label script} {
    if {[catch {uplevel 1 $script} m]} { puts "@@@ $label FAILED $m" }
}

# --- which SLR the congested tiles are in -------------------------------------
try slrmap {
    foreach t {INT_X64Y733 INT_X95Y780 INT_X71Y750 INT_X64Y830 INT_X20Y400} {
        set tl [get_tiles -quiet $t]
        if {$tl eq ""} { puts "@@@ tile $t MISSING" ; continue }
        set cr [get_clock_regions -quiet -of_objects $tl]
        puts "@@@ tile $t cr $cr slr [get_slrs -quiet -of_objects $cr]"
    }
}

# --- cells in the worst window, attributed to hierarchy -----------------------
try window {
    set names {}
    for {set x 64} {$x <= 127} {incr x} {
        for {set y 733} {$y <= 830} {incr y} { lappend names INT_X${x}Y${y} }
    }
    set sites [get_sites -quiet -of_objects [get_tiles -quiet $names]]
    set cells [get_cells -quiet -of_objects $sites]
    puts "@@@ window sites [llength $sites] cells [llength $cells]"
    array set tally {}
    foreach c $cells {
        set p [split [get_property NAME $c] /]
        incr tally([join [lrange $p 0 3] /])
    }
    set rows {}
    foreach {k v} [array get tally] { lappend rows [list $v $k] }
    foreach r [lrange [lsort -integer -index 0 -decreasing $rows] 0 24] {
        puts "@@@ owner [lindex $r 0] [lindex $r 1]"
    }
}

# --- does noc_out_data really carry a reset ----------------------------------
try nocreset {
    set base multimesh_v5_i/mesh_0/inst/u_cu0/u_cu/u_base
    set fl [get_cells -quiet "$base/noc_out_data_reg\[*\]"]
    puts "@@@ noc_out_data flops [llength $fl]"
    foreach c [lrange $fl 0 1] {
        puts "@@@ cell [get_property NAME $c] ref [get_property REF_NAME $c]"
        foreach pin {R S CE} {
            set p [get_pins -quiet "$c/$pin"]
            if {$p eq ""} { continue }
            set n [get_nets -quiet -of_objects $p]
            if {$n eq ""} { puts "@@@   $pin unconnected" ; continue }
            puts "@@@   $pin net [get_property NAME $n] type [get_property TYPE $n]"
        }
    }
}

# --- URAM placement ----------------------------------------------------------
try uram {
    set ur [get_cells -quiet -hier -filter {PRIMITIVE_TYPE =~ BLOCKRAM.URAM.*}]
    puts "@@@ uram total [llength $ur]"
    array set uc {}
    foreach c $ur {
        set st [get_sites -quiet -of_objects $c]
        if {$st eq ""} { continue }
        incr uc([get_slrs -quiet -of_objects [get_clock_regions -quiet -of_objects $st]])
    }
    foreach {k v} [array get uc] { puts "@@@ uram_slr $k $v" }
}

puts "@@@ v5_congestion done"

# Physical facts on an implementation checkpoint, read-only: where the
# device pins every fixed resource the design uses (DDR4 IO banks, Laguna
# columns, the PCIe block), and, for every bus between two blocks, where its
# drivers and loads actually landed -- the stretch each interface pays.
#   vivado -mode batch -source scripts/tcl/audit/physical.tcl ?-tclargs dcp <checkpoint> tag placed cfg v9t3?
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
set tag $root/build/${design_name}_${V8_TAG}_phys
proc ph_t {} { return [clock format [clock seconds] -format %H:%M:%S] }

# ---- 1. fixed resources ------------------------------------------------------
set fh [open ${tag}_fixed.txt w]
puts $fh "== IO banks of each DDR4 controller (clock region: IOB count)"
foreach s {0 1 2 3} {
    array unset R
    foreach p [get_ports -quiet -of_objects [get_nets -quiet -of_objects \
                  [get_pins -quiet -hier -filter "NAME =~ $top/ddr4_$s/*"]]] {
        set st [get_sites -quiet -of_objects $p]
        if {![llength $st]} { continue }
        incr R([get_property CLOCK_REGION $st])
    }
    set t {} ; foreach k [lsort [array names R]] { lappend t "$k:$R($k)" }
    puts $fh "  ddr4_$s  $t"
}
puts $fh "\n== Laguna sites per clock region (the only places an SLL lands)"
array unset L
foreach st [get_sites -quiet -filter {SITE_TYPE =~ LAGUNA*}] { incr L([get_property CLOCK_REGION $st]) }
foreach k [lsort -dictionary [array names L]] { puts $fh "  $k $L($k)" }
puts $fh "\n== hard blocks the design uses (site type: clock regions)"
foreach {label filt} {
    PCIE  {REF_NAME =~ PCIE4*}
    GT    {REF_NAME =~ GTYE4_CHANNEL*}
    MMCM  {REF_NAME =~ MMCME4*}
    BUFG  {REF_NAME =~ BUFGCE* || REF_NAME =~ BUFG_GT*}
} {
    array unset R
    foreach c [get_cells -quiet -hier -filter "IS_PRIMITIVE && ($filt)"] {
        set loc [get_property LOC $c]
        if {$loc eq ""} { continue }
        incr R([get_property CLOCK_REGION [get_sites $loc]])
    }
    set t {} ; foreach k [lsort -dictionary [array names R]] { lappend t "$k:$R($k)" }
    puts $fh "  $label  $t"
}
puts $fh "\n== URAM / RAMB36 / DSP used per clock region, by owner (mesh unit, Xache part, other)"
proc ph_owner {name} {
    set o {}
    foreach s [lrange [split $name /] 1 end] {
        if {$s eq "inst"} { continue }
        lappend o $s
        if {[llength $o] == 3} { break }
    }
    return [join $o /]
}
foreach {kind filt} {URAM {REF_NAME =~ URAM288*} RAMB {REF_NAME =~ RAMB*} DSP {REF_NAME =~ DSP*}} {
    array unset R
    foreach c [get_cells -quiet -hier -filter "IS_PRIMITIVE && ($filt)"] {
        set loc [get_property LOC $c]
        if {$loc eq ""} { continue }
        incr R([get_property CLOCK_REGION [get_sites $loc]]\t[ph_owner [get_property NAME $c]])
    }
    foreach k [lsort -dictionary [array names R]] { puts $fh "  $kind\t$k\t$R($k)" }
}
close $fh
puts "@@@ fixed resources in ${tag}_fixed.txt  [ph_t]"

# ---- 2. every inter-block bus: where its two ends sit ------------------------
# A bus is the hierarchical pins of one BD cell sharing a port prefix; for each
# bit the leaf driver and every leaf load are placed, so the ends' tile
# columns/rows and clock regions are the bus's physical stretch.
proc ph_place {pin} {
    set c [get_cells -quiet -of_objects $pin]
    set loc [get_property -quiet LOC $c]
    if {$loc eq ""} { return {} }
    set s [get_sites -quiet $loc]
    set t [get_tiles -quiet -of_objects $s]
    return [list [get_property COLUMN $t] [get_property ROW $t] [get_property CLOCK_REGION $s]]
}
set bf [open ${tag}_buses.tsv w]
puts $bf [join {CELL BUS BITS DRV_REGIONS LOAD_REGIONS DRV_COL DRV_ROW LOAD_COL LOAD_ROW MEAN_DIST MAX_DIST} \t]
set cells {}
foreach s {0 1 2 3} { lappend cells $top/mesh_$s $top/ddr4_$s $top/dwc_ctrl$s }
foreach c [get_cells -quiet $top/pipe_*] { lappend cells [get_property NAME $c] }
foreach cell $cells {
    set pins [get_pins -quiet -of_objects [get_cells -quiet $cell]]
    array unset B
    foreach p $pins {
        set pn [lindex [split [get_property NAME $p] /] end]
        regsub {\[\d+\]$} $pn "" pn
        regsub {_(\w+)$} $pn "" bus
        if {[regexp {^(c0_ddr4|C0_DDR4_S_AXI|S_AXI|M_AXI|LINK\d|S_AXI_CTRL|S_AXI_MEM|M_AXI_DRAM)} $pn m]} { set bus $m }
        lappend B($bus) $p
    }
    foreach bus [lsort [array names B]] {
        set n 0 ; set dsum 0 ; set dmax 0
        set dc {} ; set dr {} ; set lc {} ; set lr {}
        array unset DR ; array unset LR
        foreach p $B($bus) {
            set net [get_nets -quiet -of_objects $p]
            if {[llength $net] != 1} { continue }
            set lp [get_pins -quiet -leaf -of_objects $net]
            set drv [get_pins -quiet $lp -filter {DIRECTION == OUT}]
            if {[llength $drv] != 1} { continue }
            set d [ph_place $drv]
            if {![llength $d]} { continue }
            lassign $d x0 y0 r0
            incr n
            lappend dc $x0 ; lappend dr $y0 ; incr DR($r0)
            foreach l [get_pins -quiet $lp -filter {DIRECTION == IN}] {
                set q [ph_place $l]
                if {![llength $q]} { continue }
                lassign $q x1 y1 r1
                lappend lc $x1 ; lappend lr $y1 ; incr LR($r1)
                set dd [expr {abs($x1 - $x0) + abs($y1 - $y0)}]
                incr dsum $dd
                if {$dd > $dmax} { set dmax $dd }
            }
        }
        if {!$n || ![llength $lc]} { continue }
        set med {apply {{l} { set l [lsort -integer $l] ; return [lindex $l [expr {[llength $l] / 2}]] }}}
        set rt {} ; foreach k [lsort -dictionary [array names DR]] { lappend rt "$k:$DR($k)" }
        set lt {} ; foreach k [lsort -dictionary [array names LR]] { lappend lt "$k:$LR($k)" }
        puts $bf [join [list $cell $bus $n [join $rt " "] [join $lt " "] \
            [{*}$med $dc] [{*}$med $dr] [{*}$med $lc] [{*}$med $lr] \
            [format %.1f [expr {double($dsum) / [llength $lc]}]] $dmax] \t]
        flush $bf
    }
    puts "@@@ buses of $cell  [ph_t]"
}
close $bf
puts "@@@ physical facts done  [ph_t]"

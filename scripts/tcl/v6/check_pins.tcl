# Static check of sb_v6_bus.v against what the BD connects to it. No Vivado.
#   tclsh scripts/tcl/v6/check_pins.tcl
# Discovering these one BD build at a time cost four rebuilds.

set here [file dirname [file normalize [info script]]]
source $here/00_config.tcl

set fh [open $root/src/synth_top/sb_v6_bus.v r]
set src [read $fh]
close $fh

set declared {}
foreach line [split $src "\n"] {
    if {[regexp {^\s+(?:input|output)\s+wire\s*(?:\[[^\]]*\]\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*,?\s*$} \
                $line -> nm]} {
        lappend declared $nm
    }
}
proc has {sig} { global declared ; expr {[lsearch -exact $declared $sig] >= 0} }

# interface -> {protocol  what it connects to}. AXI4LITE has no id/len/size/
# burst/last; connecting either shape to the other is BD 41-1285.
set want [dict create \
    S00_AXI {AXI4     jtag_ctrl/M_AXI (jtag_axi PROTOCOL 0)} \
    S01_AXI {AXI4     xdma_0/M_AXI} \
    S02_AXI {AXI4LITE xdma_0/M_AXI_LITE}]
foreach {mid mod} $MESHES {
    set b [expr {$mid * $NQ}]
    dict set want [format M%02d_AXI [expr {$b + 0}]] {AXI4     mesh/S_AXI_MEM}
    dict set want [format M%02d_AXI [expr {$b + 1}]] {AXI4     dwc_ctrl/S_AXI}
    dict set want [format M%02d_AXI [expr {$b + 2}]] {AXI4LITE ddr4/C0_DDR4_S_AXI_CTRL}
    dict set want [format M%02d_AXI [expr {$b + 3}]] {AXI4LITE clk_wiz_mesh/s_axi_lite}
}

set bad 0
puts [format "%-10s %-9s %-9s %s" interface want got connects-to]
foreach ifc [lsort [dict keys $want]] {
    lassign [dict get $want $ifc] proto peer
    if {![has ${ifc}_awaddr]} {
        puts "  $ifc MISSING entirely" ; incr bad ; continue
    }
    set got [expr {[has ${ifc}_awlen] ? "AXI4" : "AXI4LITE"}]
    set ok [expr {$got eq $proto}]
    puts [format "%-10s %-9s %-9s %s%s" $ifc $proto $got $peer \
          [expr {$ok ? "" : "   <-- MISMATCH"}]]
    if {!$ok} { incr bad }
    # A Lite port must not carry ANY of the AXI4-only signals.
    if {$proto eq "AXI4LITE"} {
        foreach s {awid awlen awsize awburst wlast bid arid arlen arsize arburst rid rlast} {
            if {[has ${ifc}_$s]} { puts "  $ifc still has $s" ; incr bad }
        }
    }
}

# Clocks the BD drives, per station.
foreach {mid mod} $MESHES {
    foreach sig [list bus_clk$mid bus_rst$mid clk_s$mid aresetn_s$mid \
                      clk_ddr$mid aresetn_ddr$mid] {
        if {![has $sig]} { puts "  missing clock pin $sig" ; incr bad }
    }
}
foreach sig {clk_ctrl aresetn_ctrl clk_xdma aresetn_xdma} {
    if {![has $sig]} { puts "  missing clock pin $sig" ; incr bad }
}
# The single clk_ddr is gone; a stale name would connect to nothing.
foreach dead {clk_ddr aresetn_ddr} {
    if {[has $dead]} { puts "  stale $dead still declared" ; incr bad }
}

puts "\n[llength $declared] ports declared, [dict size $want] interfaces checked"
if {$bad} { error "$bad protocol/pin problem(s) -- fix before building the BD" }
puts "@@@ pins ok: protocols match every peer, per-station clk_ddr0..3"

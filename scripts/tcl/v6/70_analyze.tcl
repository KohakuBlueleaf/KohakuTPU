# Full observation of the SYNTHESIZED design, checked against the netlist. A
# pblock path that resolves empty is 12-1433 at impl and an unconstrained die.

open_run synth_1 -name v6_synth
set out $root/build
file mkdir $out
set top ${design_name}_i
# Read back the XDC THAT IMPL WILL READ, not a list held in memory: this runs in
# a later session when the project is reopened just to synthesise.
proc v6_slurp {path} {
    if {![file exists $path]} { error "v6 analyze: $path missing" }
    set fh [open $path r] ; set t [read $fh] ; close $fh
    return $t
}
set ::V6_PB {}
foreach {all pb cell} [regexp -all -inline \
        {add_cells_to_pblock \[get_pblocks (\S+)\] \[get_cells -quiet \{([^\}]+)\}\]} \
        [v6_slurp $out/${design_name}_pblocks.xdc]] {
    lappend ::V6_PB [list $pb $cell]
}
set ::V6_CLKPINS {}
foreach {all pins} [regexp -all -inline {get_pins \{([^\}]+)\}} \
                    [v6_slurp $out/${design_name}_clocks.xdc]] {
    lappend ::V6_CLKPINS {*}$pins
}
set v6_fail 0
proc v6_bad {msg} { global v6_fail ; incr v6_fail ; puts "@@@ FAIL $msg" }

# ---- 1. clocks -----------------------------------------------------------
puts "\n=== clocks ==="
set clks [get_clocks -quiet]
puts [format "%-52s %9s %9s" clock period MHz]
foreach c [lsort -dictionary [get_property NAME $clks]] {
    set p [get_property PERIOD [get_clocks $c]]
    puts [format "%-52s %9.3f %9.1f" $c $p [expr {$p > 0 ? 1000.0 / $p : 0}]]
}
puts "@@@ clocks [llength $clks]"
if {[llength $clks] < 12} { v6_bad "only [llength $clks] clocks" }

# THE PRIMARY. Every MMCM output derives from it; if create_clock matched no
# port the whole design carries no clock and routes untimed at any WNS.
if {![llength [get_clocks -quiet sys_clk]]} {
    v6_bad "sys_clk does not exist -- create_clock matched no port, so nothing\
            downstream is timed. system_clk_p is a \[0:0\] vector."
}
# Unconstrained I/O fails at placement, which is the GUI step, not this one.
puts "\n=== top-level pins ==="
set v6_pins [list system_clk_p system_clk_n pcie_reset user_lnk_up]
foreach i {0 1 2 3} {
    if {[v6_has_ddr $i]} { lappend v6_pins c${i}_sys_clk_p }
}
foreach p $v6_pins {
    set o [get_ports -quiet $p]
    if {![llength $o]} { set o [get_ports -quiet "$p\[0\]"] }
    if {![llength $o]} { v6_bad "port $p matches nothing" ; continue }
    set loc [get_property -quiet PACKAGE_PIN $o]
    puts [format "  %-16s %-18s %s" $p [get_property NAME $o] \
          [expr {$loc eq "" ? "NO PACKAGE_PIN" : $loc}]]
    if {$loc eq ""} { v6_bad "port $p has no PACKAGE_PIN" }
}

# Every mesh clock must be 300 and every 2x 600, or a rate was invented.
# The wizards exist in probe mode too, so the rate check covers all four.
# `co`, NOT `out`: this file's report directory is $out, and the loop var
# shadowing it sent every section-5 report into a directory named clk_out4.
foreach {mid mod} $MESHES {
    foreach {co want} [list clk_out1 $MESH_MHZ clk_out2 $MAT2X_MHZ \
                            clk_out3 $VEC_MHZ clk_out4 $MESH_MHZ] {
        set c [get_clocks -quiet -of_objects \
                   [get_pins -quiet $top/clk_wiz_mesh$mid/$co]]
        if {![llength $c]} { v6_bad "mesh$mid/$co has no clock" ; continue }
        set got [expr {1000.0 / [get_property PERIOD $c]}]
        if {abs($got - $want) > 1.0} {
            v6_bad [format "mesh%s/%s is %.1f MHz, want %.1f" $mid $co $got $want]
        }
    }
}

# ---- 1b. DDR4 <-> SLR, from the part rather than from DDR_OF_SLR ----------
# ddr4_0's pins are in SLR3. A controller pblocked into the wrong die is never
# reported downstream; it just routes badly forever.
puts "\n=== DDR4 pins per die ==="
foreach {mid mod} $MESHES {
    if {![v6_has_mesh $mid]} { continue }
    set ddr [dict get $DDR_OF_SLR $mid]
    set fh [open $BOARD_DIR/ddr4_c${ddr}.xdc r]
    set txt [read $fh] ; close $fh
    set slrs {}
    foreach {all pin} [regexp -all -inline {PACKAGE_PIN\s+(\w+)} $txt] {
        set st [get_sites -quiet -of_objects [get_package_pins -quiet $pin]]
        if {![llength $st]} { continue }
        lappend slrs [get_property NAME [get_slrs -of_objects $st]]
    }
    set slrs [lsort -unique $slrs]
    puts [format "  mesh_%s <- ddr4_%s  pins in %s" $mid $ddr $slrs]
    if {[llength $slrs] != 1} {
        v6_bad "ddr4_$ddr spans $slrs -- one controller cannot straddle dice"
    } elseif {[lindex $slrs 0] ne "SLR$mid"} {
        v6_bad "mesh_$mid is pblocked to SLR$mid but takes ddr4_$ddr, whose pins\
                are in [lindex $slrs 0]"
    }
}

# ---- 2. pblock targets ---------------------------------------------------
# THE EMITTED LIST, not a copy: an unresolved path is 12-1433 at impl and a
# whole die left unconstrained, which no later report calls out.
puts "\n=== pblock cell paths ==="
foreach e $::V6_PB {
    lassign $e pb cell
    set n [llength [get_cells -quiet {*}[list $cell]]]
    if {$n == 0} { set n [llength [get_cells -quiet -hier -filter "NAME =~ $cell"]] }
    puts [format "  %-8s %-52s %6d" $pb $cell $n]
    if {$n == 0} { v6_bad "pblock $pb: $cell resolves to NO cells" }
}
# g_link MUST NOT be pinned -- its registers are the die crossing.
foreach {mid mod} $MESHES {
    set pat $top/station_bus/inst/u_line/g_stn\[$mid\].*/*g_link*
    if {[llength [get_cells -quiet -hier -filter "NAME =~ $pat"]] > 0} {
        v6_bad "g_link matched inside g_stn\[$mid\] -- it would be pinned"
    }
}

# Every pin the clock XDC named. One that resolves empty makes its -group a
# silent no-op, and the crossings it should have cut are timed instead.
puts "\n=== clock-group pins ==="
foreach p $::V6_CLKPINS {
    set c [get_clocks -quiet -of_objects [get_pins -quiet $p]]
    set nm [expr {[llength $c] ? [join [get_property NAME $c] " "] : "NONE"}]
    puts [format "  %-56s %s" $p $nm]
    if {![llength $c]} { v6_bad "clock pin $p resolves to no clock" }
}

# ---- 3. module setup -----------------------------------------------------
puts "\n=== meshes ==="
foreach {mid mod} $MESHES {
    if {![v6_has_mesh $mid]} { continue }
    set cell [get_cells -quiet $top/mesh_$mid]
    if {![llength $cell]} { v6_bad "mesh_$mid absent" ; continue }
    # A module-reference cell synthesizes as <design>_<inst>_0 wrapping an
    # `inst` whose REF_NAME is the real module -- which per-IP OOC synthesis
    # uniquifies to <design>_<inst>_0_<module>, so match the suffix too.
    set ref [get_property REF_NAME $cell]
    set inner [get_cells -quiet $top/mesh_$mid/inst]
    set iref [expr {[llength $inner] ? [get_property REF_NAME $inner] : ""}]
    if {$ref ne $mod && $iref ne $mod && ![string match "*_$mod" $iref]} {
        v6_bad "mesh_$mid is $ref (inner: $iref), want $mod"
    }
    set bits {}
    foreach p {MESH_ID L2_MAG_BANKS L2_MAG_ENTRIES L2_CU_DEPTH TILE_PRIM \
               MAG_CDC UNIT_CDC} {
        set v [get_property -quiet $p $cell]
        lappend bits "$p=$v"
    }
    puts "  mesh_$mid  $ref  [join $bits {  }]"
}

# The clock each mesh port actually sees, traced through the netlist rather
# than read back off the script that wrote it.
puts "\n=== per-mesh clock sources ==="
foreach {mid mod} $MESHES {
    if {![v6_has_mesh $mid]} { continue }
    foreach pin {axi_aclk noc_clk vec_clk mat_clk mat_clk2x dram_aclk} {
        set p [get_pins -quiet $top/mesh_$mid/$pin]
        if {![llength $p]} { v6_bad "mesh_$mid/$pin missing" ; continue }
        set c [get_clocks -quiet -of_objects $p]
        set nm [expr {[llength $c] ? [get_property NAME $c] : "NONE"}]
        set mhz [expr {[llength $c] ? 1000.0 / [get_property PERIOD $c] : 0}]
        puts [format "  mesh_%s %-10s %-44s %7.1f MHz" $mid $pin $nm $mhz]
        if {![llength $c]} { v6_bad "mesh_$mid/$pin has no clock" }
    }
}

# ---- 4. station bus ------------------------------------------------------
puts "\n=== station bus ==="
# CONFIG.* lives on the BD cell, not the netlist cell: a REOPENED session
# (impl relaunch) reads blanks off the netlist and this guard false-fails.
open_bd_design [get_files ${design_name}.bd]
set sbbd [get_bd_cells -quiet /station_bus]
foreach p {FW OST LINK_CDC LINK_FULL CRED PIPE SEG_OVERRIDE} {
    puts "  $p = [get_property -quiet CONFIG.$p $sbbd]"
}
if {[get_property -quiet CONFIG.SEG_OVERRIDE $sbbd] ne "1"} {
    v6_bad "station_bus SEG_OVERRIDE is not 1 -- the uniform 64 KB test map is live"
}
current_design v6_synth
foreach {mid mod} $MESHES {
    foreach sig {clk_ddr aresetn_ddr} {
        if {![llength [get_pins -quiet $top/station_bus/${sig}$mid]]} {
            v6_bad "station_bus/${sig}$mid missing"
        }
    }
}

# ---- 5. timing and area --------------------------------------------------
puts "\n=== timing (synthesis) ==="
report_timing_summary -file $out/${design_name}_synth_timing.rpt
foreach c [lsort -dictionary [get_property NAME $clks]] {
    set p [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type max \
                            -sort_by slack -to [get_clocks $c]]
    if {![llength $p]} { continue }
    puts [format "  %-52s WNS %+8.3f  levels %s" $c \
          [get_property SLACK $p] [get_property LOGIC_LEVELS $p]]
}
set wp [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type max -sort_by slack]
if {[llength $wp]} {
    puts "@@@ v6 synth WNS [format %.3f [get_property SLACK $wp]]\
 levels [get_property LOGIC_LEVELS $wp]"
    puts "@@@   from [get_property STARTPOINT_PIN $wp]"
    puts "@@@   to   [get_property ENDPOINT_PIN $wp]"
}

report_utilization -file $out/${design_name}_synth_util.rpt
report_utilization -hierarchical -hierarchical_depth 3 \
    -file $out/${design_name}_synth_util_hier.rpt
report_control_sets -verbose -file $out/${design_name}_synth_ctrlsets.rpt
puts "\n@@@ reports in $out"
if {$v6_fail} { error "v6 analysis: $v6_fail check(s) FAILED" }
puts "@@@ v6 analysis: all checks passed"

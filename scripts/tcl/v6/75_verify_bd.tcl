# Verify the BUILT block design without synthesising it: 70_analyze.tcl needs an
# open_run, so nothing until now checked the BD itself.

#   vivado -mode batch -source scripts/tcl/v6/75_verify_bd.tcl

set here [file dirname [file normalize [info script]]]
source $here/00_config.tcl

# A probe build lives in its own project, never the main one.
set v6_xpr [expr {$PROBE_SLR >= 0 ? "$proj_dir/${design_name}.xpr" : $MAIN_XPR}]
if {![file exists $v6_xpr]} { error "no project at $v6_xpr" }
open_project $v6_xpr

set fail 0
proc bad {msg} { global fail ; incr fail ; puts "@@@ FAIL $msg" }
proc ok  {msg} { puts "@@@ ok   $msg" }

# ---- 1. the design is there and it is the top ----------------------------
set bdf [get_files -quiet ${design_name}.bd]
if {![llength $bdf]} { error "no ${design_name}.bd in $MAIN_XPR -- build it first" }
open_bd_design $bdf
ok "opened [file tail $bdf]"

set top [get_property top [current_fileset]]
if {$top ne "${design_name}_wrapper"} {
    bad "top is $top, not ${design_name}_wrapper"
} else { ok "top $top" }
if {[get_property top_auto_set [current_fileset]] ne "0"} {
    bad "top_auto_set is on -- auto-top picks a bare mesh and place_design dies\
         on 3,610 I/O one synthesis later"
}

# validate_bd_design ERRORS on a real problem, so catching it is the check.
if {[catch {validate_bd_design -quiet} vmsg]} {
    bad "validate_bd_design: $vmsg"
} else { ok "validate_bd_design clean" }

# ---- 2. every mesh is the module the config names ------------------------
puts "\n=== meshes ==="
foreach {mid mod} $MESHES {
    if {![v6_has_mesh $mid]} { continue }
    set c [get_bd_cells -quiet mesh_$mid]
    if {![llength $c]} { bad "mesh_$mid absent" ; continue }
    set ref [get_property CONFIG.Component_Name $c]
    set id  [get_property CONFIG.MESH_ID $c]
    puts [format "  mesh_%s  MESH_ID=%s  banks=%s entries=%s" $mid $id \
          [get_property -quiet CONFIG.L2_MAG_BANKS $c] \
          [get_property -quiet CONFIG.L2_MAG_ENTRIES $c]]
    if {$id ne "$mid"} { bad "mesh_$mid has MESH_ID $id" }
}

# ---- 3. which net actually drives each mesh clock ------------------------

# The BD is where a clock gets mis-wired; after synthesis it is a net name that
# no longer says which generator it came from.
puts "\n=== per-mesh clock sources ==="
proc srcpin {pin} {
    set n [get_bd_nets -quiet -of_objects [get_bd_pins -quiet $pin]]
    if {![llength $n]} { return "UNCONNECTED" }
    set src [get_bd_pins -quiet -of_objects $n -filter {DIR == O}]
    if {![llength $src]} { return "no-driver" }
    # get_bd_pins returns an ABSOLUTE path; the wants below are cell-relative.
    return [string trimleft [lindex $src 0] /]
}
foreach {mid mod} $MESHES {
    if {![v6_has_mesh $mid]} { continue }
    foreach {pin want} [list axi_aclk clk_wiz_mesh$mid/clk_out4 \
                             noc_clk  clk_wiz_mesh$mid/clk_out1 \
                             vec_clk  clk_wiz_mesh$mid/clk_out3 \
                             mat_clk2x clk_wiz_mesh$mid/clk_out2 \
                             mat_clk  div2_mesh$mid/clk1x] {
        set got [srcpin mesh_$mid/$pin]
        puts [format "  mesh_%s %-10s <- %s" $mid $pin $got]
        if {$got ne $want} { bad "mesh_$mid/$pin driven by $got, want $want" }
    }
}

# ---- 4. each mesh's DRAM is the controller in its own die ----------------
puts "\n=== mesh <-> ddr4 pairing ==="
foreach {mid mod} $MESHES {
    if {![v6_has_mesh $mid]} { continue }
    set ddr [dict get $DDR_OF_SLR $mid]
    set got [srcpin mesh_$mid/dram_aclk]
    puts [format "  mesh_%s dram_aclk <- %s  (want ddr4_%s)" $mid $got $ddr]
    if {$got ne "ddr4_$ddr/c0_ddr4_ui_clk"} {
        bad "mesh_$mid takes its DRAM clock from $got, not ddr4_$ddr"
    }
}

# ---- 5. the station bus carries the configured shape --------------------
puts "\n=== station bus ==="
set sb [get_bd_cells -quiet station_bus]
if {![llength $sb]} { bad "station_bus absent" } else {
    foreach {p want} [list FW $FW OST $OST LINK_CDC $LINK_CDC \
                           LINK_FULL $LINK_FULL CRED $CRED PIPE $PIPE \
                           SEG_OVERRIDE 1] {
        set got [get_property -quiet CONFIG.$p $sb]
        puts [format "  %-13s %s" $p $got]
        if {$got ne "$want"} { bad "station_bus $p is $got, want $want" }
    }
}

# ---- 5b. bus clock rate ---------------------------------------------------
# By PIN FREQ_HZ: under OVERRIDE_MMCM the requested frequency can lie.
puts "\n=== bus clocks ==="
set want_hz [expr {int($BUS_MHZ * 1e6)}]
foreach {mid mod} $MESHES {
    set hz [get_property -quiet CONFIG.FREQ_HZ \
                [get_bd_pins -quiet clk_wiz_bus$mid/clk_out1]]
    puts [format "  clk_wiz_bus%s/clk_out1  %s Hz" $mid $hz]
    if {$hz ne "$want_hz"} { bad "clk_wiz_bus$mid runs at $hz, want $want_hz" }
}

# ---- 6. the address map ---------------------------------------------------

# An unassigned segment is BD 41-1356 at generate, and an excluded one is a
# window the master cannot reach at all.
puts "\n=== address segments ==="
set nass [llength [get_bd_addr_segs -quiet -excluded]]
puts "  excluded segments: $nass"
foreach {mid mod} $MESHES {
    if {![v6_has_mesh $mid]} { continue }
    set ddr [dict get $DDR_OF_SLR $mid]
    # The segments MAPPED INTO the space, not the addressables it could reach.
    set segs [get_bd_addr_segs -quiet \
                  -of_objects [get_bd_addr_spaces -quiet mesh_$mid/M_AXI_DRAM]]
    set hit 0
    foreach s $segs { if {[string match "*ddr4_$ddr*" $s]} { set hit 1 } }
    puts [format "  mesh_%s/M_AXI_DRAM -> ddr4_%s : %s" $mid $ddr \
          [expr {$hit ? "assigned" : "MISSING"}]]
    if {!$hit} { bad "mesh_$mid/M_AXI_DRAM is not mapped onto ddr4_$ddr" }
}

# ---- 7. constraints: ours enabled, the stale ones off --------------------
puts "\n=== constraints ==="
foreach f [get_files -quiet -of_objects [get_filesets constrs_1]] {
    puts [format "  %-44s enabled=%s order=%s" [file tail $f] \
          [get_property is_enabled $f] [get_property -quiet PROCESSING_ORDER $f]]
}
set v6_need [list ${design_name}_pblocks.xdc ${design_name}_clocks.xdc pcie.xdc]
foreach i {0 1 2 3} { if {[v6_has_ddr $i]} { lappend v6_need ddr4_c${i}.xdc } }
foreach need $v6_need {
    set f [get_files -quiet -of_objects [get_filesets constrs_1] *$need]
    if {![llength $f]} { bad "constraint $need is not in constrs_1" } \
    elseif {![get_property is_enabled [lindex $f 0]]} { bad "$need is disabled" }
}
foreach stale [list multimesh_v2_clocks.xdc multimesh_v3_clocks.xdc \
                    multimesh_v5_clocks.xdc sysytem.xdc] {
    foreach f [get_files -quiet -of_objects [get_filesets constrs_1] *$stale] {
        if {[get_property is_enabled $f]} {
            bad "[file tail $f] is still enabled -- XDC files ADD, so its clock\
                 groups conflict with ours"
        }
    }
}

puts ""
if {$fail} { error "v6 BD verify: $fail check(s) FAILED" }
puts "@@@ v6 BD verify: all checks passed"

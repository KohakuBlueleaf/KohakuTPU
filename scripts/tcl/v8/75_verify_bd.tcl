# Verify the BUILT block design without synthesising it: every clock net,
# reset net, module and address segment read back from the BD against the
# plan. 70_analyze repeats the checks on the netlist after synthesis.
#   vivado -mode batch -source scripts/tcl/v8/75_verify_bd.tcl

set here [file dirname [file normalize [info script]]]
source $here/00_config.tcl
source $here/50_addr_lit.tcl

set v8_xpr $proj_dir/${design_name}.xpr
if {![file exists $v8_xpr]} { error "no project at $v8_xpr" }
open_project $v8_xpr

set fail 0
proc bad {msg} { global fail ; incr fail ; puts "@@@ FAIL $msg" }
proc ok  {msg} { puts "@@@ ok   $msg" }

set bdf [get_files -quiet ${design_name}.bd]
if {![llength $bdf]} { error "no ${design_name}.bd -- build it first" }
open_bd_design $bdf
ok "opened [file tail $bdf]"
set top [get_property top [current_fileset]]
if {$top ne "${design_name}_wrapper"} { bad "top is $top, not ${design_name}_wrapper" } else { ok "top $top" }
if {[get_property top_auto_set [current_fileset]] ne "0"} { bad "top_auto_set is on" }
if {[catch {validate_bd_design -quiet} vmsg]} { bad "validate_bd_design: $vmsg" } else { ok "validate_bd_design clean" }

proc srcpin {pin} {
    set n [get_bd_nets -quiet -of_objects [get_bd_pins -quiet $pin]]
    if {![llength $n]} { return "UNCONNECTED" }
    set src [get_bd_pins -quiet -of_objects $n -filter {DIR == O}]
    if {![llength $src]} { return "no-driver" }
    return [string trimleft [lindex $src 0] /]
}
proc want {pin w} {
    set got [srcpin $pin]
    puts [format "  %-34s <- %s" $pin $got]
    if {$got ne $w} { bad "$pin driven by $got, want $w" }
}

# ---- meshes: module, parameters, clocks, resets --------------------------
puts "\n=== meshes ==="
foreach {mid mod} $MESHES {
    set c [get_bd_cells -quiet mesh_$mid]
    if {![llength $c]} { bad "mesh_$mid absent" ; continue }
    set ref [get_property VLNV $c]
    puts [format "  mesh_%s  %s  MESH_ID=%s banks=%s entries=%s L2_CU_DEPTH=%s" $mid $ref \
          [get_property CONFIG.MESH_ID $c] [get_property -quiet CONFIG.L2_MAG_BANKS $c] \
          [get_property -quiet CONFIG.L2_MAG_ENTRIES $c] [get_property -quiet CONFIG.L2_CU_DEPTH $c]]
    if {[get_property CONFIG.MESH_ID $c] ne "$mid"} { bad "mesh_$mid has MESH_ID [get_property CONFIG.MESH_ID $c]" }
    if {![string match "*module_ref:$mod:*" $ref]} { bad "mesh_$mid is $ref, want $mod" }
    if {[get_property -quiet CONFIG.L2_CU_DEPTH $c] ne ""} { bad "mesh_$mid still declares an L2 adapter depth" }
    want mesh_$mid/axi_aclk    $SYS_CLK
    want mesh_$mid/axi_aresetn [v8_rstn $mid]
    want mesh_$mid/noc_clk     clk_wiz_mesh$mid/clk_out1
    want mesh_$mid/vec_clk     clk_wiz_mesh$mid/clk_out3
    want mesh_$mid/mat_clk2x   clk_wiz_mesh$mid/clk_out2
    want mesh_$mid/mat_clk     div2_mesh$mid/clk1x
    if {[get_property -quiet CONFIG.DRAM_CDC $c] ne "0"} { bad "mesh_$mid DRAM_CDC is not 0" }
    want mesh_$mid/dram_aclk    $SYS_CLK
    want mesh_$mid/dram_aresetn [v8_rstn $mid]
}
foreach hop {{0 1} {1 2} {2 3}} {
    lassign $hop lo hi
    want pipe_${lo}_to_${hi}/aclk    $SYS_CLK
    want pipe_${lo}_to_${hi}/aresetn [v8_rstn $lo]
    want pipe_${hi}_to_${lo}/aresetn [v8_rstn $hi]
}

# ---- the Xache -----------------------------------------------------------
puts "\n=== Kohaku Xache ==="
set kx [get_bd_cells -quiet xache]
if {![llength $kx]} { bad "xache absent" } else {
    foreach {p w} [list SETS $KX_SETS SET_W $KX_SET_W RD_OUTQ $KX_RD_OUTQ SLRX $KX_SLRX \
                        NSWAP $KX_NSWAP RD_PIPE 1 RSAMD 1 WSAMD 1] {
        set got [get_property -quiet CONFIG.$p $kx]
        puts [format "  %-10s %s" $p $got]
        if {$got ne "$w"} { bad "xache $p is $got, want $w" }
    }
    want xache/aclk    $SYS_CLK
    foreach {mid mod} $MESHES {
        set ddr [dict get $DDR_OF_SLR $mid]
        want xache/d_rstn$mid [v8_rstn $mid]
        want xache/h_clk$mid  ddr4_$ddr/c0_ddr4_ui_clk
        want xache/h_rstn$mid rst_ddr4_$ddr/peripheral_aresetn
        # the master and channel interfaces, by net
        set sn [get_bd_intf_nets -quiet -of_objects [get_bd_intf_pins xache/S0${mid}_AXI]]
        set sp [lsort [get_bd_intf_pins -quiet -of_objects $sn]]
        if {[lsearch -glob $sp "*mesh_$mid/M_AXI_DRAM"] < 0} { bad "xache/S0${mid}_AXI is not mesh_$mid's DRAM master" }
        set mn [get_bd_intf_nets -quiet -of_objects [get_bd_intf_pins xache/M0${mid}_AXI]]
        set mp [lsort [get_bd_intf_pins -quiet -of_objects $mn]]
        if {[lsearch -glob $mp "*ddr4_$ddr/C0_DDR4_S_AXI"] < 0} { bad "xache/M0${mid}_AXI is not ddr4_$ddr" }
        puts "  S0${mid}_AXI <- mesh_$mid/M_AXI_DRAM ; M0${mid}_AXI -> ddr4_$ddr"
    }
}

# ---- one clock, one reset: every PSR's clock and lock ---------------------
puts "\n=== resets ==="
foreach {name clkw lockw} [list rst_ctrl clk_wiz_ctrl/clk_out1 clk_wiz_ctrl/locked \
                                rst_sys $SYS_CLK $SYS_LOCK] {
    want $name/slowest_sync_clk $clkw
    want $name/dcm_locked $lockw
}
want rst_tree/clk     $SYS_CLK
want rst_tree/rstn_in rst_sys/peripheral_aresetn
foreach {mid mod} $MESHES {
    if {[llength [get_bd_cells -quiet rst_noc$mid]]} { bad "rst_noc$mid exists: the NoC reset is the top's own kh_rst_sync, this block has no load" }
    want rst_bus$mid/slowest_sync_clk clk_wiz_bus$mid/clk_out1
    set ddr [dict get $DDR_OF_SLR $mid]
    want rst_ddr4_$ddr/slowest_sync_clk ddr4_$ddr/c0_ddr4_ui_clk
    want rst_ddr4_$ddr/ext_reset_in     ddr4_$ddr/c0_ddr4_ui_clk_sync_rst
}
# the redundant MAG-rate output must be gone on the other dies
foreach {mid mod} $MESHES {
    set has [llength [get_bd_pins -quiet clk_wiz_mesh$mid/clk_out4]]
    if {$mid == $SYS_WIZ && !$has} { bad "clk_wiz_mesh$mid lacks clk_out4, the sysnode clock" }
    if {$mid != $SYS_WIZ && $has}  { bad "clk_wiz_mesh$mid still has a clk_out4" }
}

# ---- station bus, per die -------------------------------------------------
puts "\n=== station bus ==="
set sb [get_bd_cells -quiet station_bus]
foreach {p w} [list FW $FW OST $OST LINK_CDC $LINK_CDC LINK_FULL $LINK_FULL CRED $CRED PIPE $PIPE SEG_OVERRIDE 1] {
    set got [get_property -quiet CONFIG.$p $sb]
    if {$got ne "$w"} { bad "station_bus $p is $got, want $w" }
}
foreach {nm w} [list SEG_BASE_P [v8_cat $seg_base $AW] SEG_MASK_P [v8_cat $seg_mask $AW] \
                     SEG_XLT_P [v8_cat $seg_xlt $AW] SEG_DST_P [v8_cat $seg_dst 2] SEG_DPORT_P [v8_cat $seg_dprt 2]] {
    if {[get_property -quiet CONFIG.$nm $sb] ne $w} { bad "station_bus $nm differs from the intended table" }
}
ok "station segment literals match the intended table"
foreach {mid mod} $MESHES {
    want station_bus/clk_s$mid     $SYS_CLK
    want station_bus/aresetn_s$mid [v8_rstn $mid]
    want dwc_ctrl$mid/s_axi_aclk    $SYS_CLK
    want dwc_ctrl$mid/s_axi_aresetn [v8_rstn $mid]
    set ddr [dict get $DDR_OF_SLR $mid]
    want station_bus/clk_ddr$mid   ddr4_$ddr/c0_ddr4_ui_clk
    want station_bus/bus_clk$mid   clk_wiz_bus$mid/clk_out1
    foreach {port peer} [list [expr {$mid*$NQ+0}] mesh_$mid/S_AXI_MEM \
                              [expr {$mid*$NQ+1}] dwc_ctrl$mid/S_AXI \
                              [expr {$mid*$NQ+2}] ddr4_$ddr/C0_DDR4_S_AXI_CTRL \
                              [expr {$mid*$NQ+3}] clk_wiz_mesh$mid/s_axi_lite] {
        set ip [get_bd_intf_pins station_bus/[format M%02d_AXI $port]]
        set n [get_bd_intf_nets -quiet -of_objects $ip]
        set pins [get_bd_intf_pins -quiet -of_objects $n]
        if {[lsearch -glob $pins "*/$peer"] < 0} { bad "station port $port is not $peer" }
    }
}
ok "every station port on its intended endpoint"

# ---- the address map ------------------------------------------------------
puts "\n=== address map ==="
puts "  excluded segments: [llength [get_bd_addr_segs -quiet -excluded]]"
foreach {mid mod} $MESHES {
    set ddr [dict get $DDR_OF_SLR $mid]
    # the master-side segment is SEG_xache_reg0: the port index is not in its
    # name, and the interface-net check above already pinned S0m to mesh m
    set segs [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet mesh_$mid/M_AXI_DRAM]]
    set hit "" ; foreach s $segs { if {[string match "*xache*" $s]} { set hit $s } }
    if {$hit eq ""} { bad "mesh_$mid/M_AXI_DRAM not mapped onto the Xache ($segs)" ; continue }
    if {[llength $segs] != 1} { bad "mesh_$mid/M_AXI_DRAM has [llength $segs] segments, want the Xache alone: $segs" }
    set off [get_property OFFSET $hit] ; set rng [get_property RANGE $hit]
    puts [format "  mesh_%s/M_AXI_DRAM -> xache S0%s  offset %s range %s" $mid $mid $off $rng]
    if {[expr {$off}] != 0 || [expr {$rng}] < ($DRAM_GB << 30)} { bad "mesh_$mid's Xache aperture is not the flat space at 0" }
    set dsegs [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet xache/M0${mid}_AXI]]
    set dhit "" ; foreach s $dsegs { if {[string match "*ddr4_$ddr*" $s]} { set dhit $s } }
    if {$dhit eq ""} { bad "xache/M0${mid}_AXI not mapped onto ddr4_$ddr" ; continue }
    set doff [get_property OFFSET $dhit] ; set drng [get_property RANGE $dhit]
    puts [format "  xache/M0%s_AXI -> ddr4_%s  offset %s range %s" $mid $ddr $doff $drng]
    if {[expr {$doff}] != 0 || [expr {$drng}] != (4 << 30)} { bad "ddr4_$ddr behind home $mid is not 4 GB at 0" }
}

# ---- constraints ----------------------------------------------------------
puts "\n=== constraints ==="
foreach f [get_files -quiet -of_objects [get_filesets constrs_1]] {
    puts [format "  %-44s enabled=%s order=%s" [file tail $f] \
          [get_property is_enabled $f] [get_property -quiet PROCESSING_ORDER $f]]
}
foreach need [list ${design_name}_pblocks.xdc ${design_name}_clocks.xdc pcie.xdc \
                   ddr4_c0.xdc ddr4_c1.xdc ddr4_c2.xdc ddr4_c3.xdc] {
    set f [get_files -quiet -of_objects [get_filesets constrs_1] *$need]
    if {![llength $f]} { bad "constraint $need is not in constrs_1" } \
    elseif {![get_property is_enabled [lindex $f 0]]} { bad "$need is disabled" }
}
puts ""
if {$fail} { error "v8 BD verify: $fail check(s) FAILED" }
puts "@@@ v8 BD verify: all checks passed"

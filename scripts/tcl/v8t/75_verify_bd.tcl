# Verify the BUILT block design without synthesising it: every clock net,
# reset net, module and address segment read back from the BD against the
# plan. 70_analyze repeats the checks on the netlist after synthesis.
#   vivado -mode batch -source scripts/tcl/v8t/75_verify_bd.tcl

set here [file dirname [file normalize [info script]]]
# a sibling flow (v8t2) loads its own config first and then this file
if {![info exists design_name]} { source $here/00_config.tcl }
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

# ---- nodes: module, parameters, clocks, resets ---------------------------
puts "\n=== nodes ==="
foreach {mid mod} $MESHES {
    set c [get_bd_cells -quiet node_$mid]
    if {![llength $c]} { bad "node_$mid absent" ; continue }
    set ref [get_property VLNV $c]
    puts [format "  node_%s  %s  MESH_ID=%s PORTS=%s banks=%s entries=%s DRAM_CDC=%s" $mid $ref \
          [get_property CONFIG.MESH_ID $c] [get_property -quiet CONFIG.PORTS $c] \
          [get_property -quiet CONFIG.L2_MAG_BANKS $c] \
          [get_property -quiet CONFIG.L2_MAG_ENTRIES $c] [get_property -quiet CONFIG.DRAM_CDC $c]]
    if {[get_property CONFIG.MESH_ID $c] ne "$mid"} { bad "node_$mid has MESH_ID [get_property CONFIG.MESH_ID $c]" }
    if {![string match "*module_ref:$mod:*" $ref]} { bad "node_$mid is $ref, want $mod" }
    if {[get_property -quiet CONFIG.DRAM_CDC $c] ne "0"} { bad "node_$mid DRAM_CDC is not 0" }
    if {[get_property -quiet CONFIG.L2_MAG_BANKS $c] ne "$L2_MAG_BANKS"} { bad "node_$mid staging banks" }
    if {[get_property -quiet CONFIG.L2_MAG_ENTRIES $c] ne "$L2_MAG_ENTRIES"} { bad "node_$mid staging entries" }
    want node_$mid/axi_aclk     $SYS_CLK
    want node_$mid/axi_aresetn  [v8_rstn $mid]
    want node_$mid/dram_aclk    $SYS_CLK
    want node_$mid/dram_aresetn [v8_rstn $mid]
}
if {[llength [get_bd_cells -quiet mesh_*]]} { bad "a mesh_* cell exists: v8t has no mesh" }
if {[llength [get_bd_cells -quiet pipe_*]]}  { bad "an interlink pipe exists: v8t has no interlink" }

# ---- the Xache -----------------------------------------------------------
puts "\n=== Kohaku Xache (kx_pxache) ==="
set kx [get_bd_cells -quiet xache]
if {![llength $kx]} { bad "xache absent" } else {
    if {![string match "*module_ref:kx_pbd_4x4:*" [get_property VLNV $kx]]} { bad "xache is not kx_pbd_4x4" }
    foreach {p w} [list SETS $KX_SETS SET_W $KX_SET_W RD_OUTQ $KX_RD_OUTQ WR_OUTQ $KX_WR_OUTQ \
                        HOP_DEPTH $KX_HOP_DEPTH HOP_RXREG $KX_HOP_RXREG NSWAP $KX_NSWAP \
                        CDC_DEPTH $KX_CDC_DEPTH K 1 BANKS $KX_BANKS] {
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
        set sn [get_bd_intf_nets -quiet -of_objects [get_bd_intf_pins xache/S0${mid}_AXI]]
        set sp [lsort [get_bd_intf_pins -quiet -of_objects $sn]]
        if {[lsearch -glob $sp "*node_$mid/M_AXI_DRAM"] < 0} { bad "xache/S0${mid}_AXI is not node_$mid's DRAM master" }
        set mn [get_bd_intf_nets -quiet -of_objects [get_bd_intf_pins xache/M0${mid}_AXI]]
        set mp [lsort [get_bd_intf_pins -quiet -of_objects $mn]]
        if {[lsearch -glob $mp "*ddr4_$ddr/C0_DDR4_S_AXI"] < 0} { bad "xache/M0${mid}_AXI is not ddr4_$ddr" }
        puts "  S0${mid}_AXI <- node_$mid/M_AXI_DRAM ; M0${mid}_AXI -> ddr4_$ddr"
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
    want rst_bus$mid/slowest_sync_clk clk_wiz_bus$mid/clk_out1
    want rst_bus$mid/dcm_locked       clk_wiz_bus$mid/locked
    set ddr [dict get $DDR_OF_SLR $mid]
    want rst_ddr4_$ddr/slowest_sync_clk ddr4_$ddr/c0_ddr4_ui_clk
    want rst_ddr4_$ddr/ext_reset_in     ddr4_$ddr/c0_ddr4_ui_clk_sync_rst
}
set npsr [llength [get_bd_cells -quiet rst_*]]
# rst_tree is a module, not a proc_sys_reset
if {$npsr != 11} { bad "$npsr rst_* cells, want 11: ctrl, sys, tree, 4 bus, 4 MIG" }
# only SYS_WIZ carries the sysnode clock
foreach {mid mod} $MESHES {
    set has [llength [get_bd_pins -quiet clk_wiz_mesh$mid/clk_out4]]
    if {$mid == $SYS_WIZ && !$has} { bad "clk_wiz_mesh$mid lacks clk_out4, the sysnode clock" }
    if {$mid != $SYS_WIZ && $has}  { bad "clk_wiz_mesh$mid has a clk_out4" }
    if {$mid != $SYS_WIZ} {
        set n [get_bd_nets -quiet -of_objects [get_bd_pins -quiet clk_wiz_mesh$mid/clk_out1]]
        if {[llength $n]} { bad "clk_wiz_mesh$mid/clk_out1 drives something: no die but SYS_WIZ clocks anything" }
    }
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
    want station_bus/bus_clk$mid   clk_wiz_bus$mid/clk_out1
    want station_bus/bus_rst$mid   bus_rst_inv$mid/Res
    want dwc_ctrl$mid/s_axi_aclk    $SYS_CLK
    want dwc_ctrl$mid/s_axi_aresetn [v8_rstn $mid]
    set ddr [dict get $DDR_OF_SLR $mid]
    want station_bus/clk_ddr$mid   ddr4_$ddr/c0_ddr4_ui_clk
    foreach {port peer} [list [expr {$mid*$NQ+0}] node_$mid/S_AXI_MEM \
                              [expr {$mid*$NQ+1}] dwc_ctrl$mid/S_AXI \
                              [expr {$mid*$NQ+2}] ddr4_$ddr/C0_DDR4_S_AXI_CTRL \
                              [expr {$mid*$NQ+3}] clk_wiz_mesh$mid/s_axi_lite] {
        set ip [get_bd_intf_pins station_bus/[format M%02d_AXI $port]]
        set n [get_bd_intf_nets -quiet -of_objects $ip]
        set pins [get_bd_intf_pins -quiet -of_objects $n]
        if {[lsearch -glob $pins "*/$peer"] < 0} { bad "station port $port is not $peer" }
    }
}
want station_bus/clk_ctrl clk_wiz_ctrl/clk_out1
want station_bus/clk_xdma xdma_0/axi_aclk
ok "every station port on its intended endpoint"

# ---- the address map ------------------------------------------------------
puts "\n=== address map ==="
puts "  excluded segments: [llength [get_bd_addr_segs -quiet -excluded]]"
foreach {mid mod} $MESHES {
    set ddr [dict get $DDR_OF_SLR $mid]
    set segs [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet node_$mid/M_AXI_DRAM]]
    set hit "" ; foreach s $segs { if {[string match "*xache*" $s]} { set hit $s } }
    if {$hit eq ""} { bad "node_$mid/M_AXI_DRAM not mapped onto the Xache ($segs)" ; continue }
    if {[llength $segs] != 1} { bad "node_$mid/M_AXI_DRAM has [llength $segs] segments, want the Xache alone: $segs" }
    set off [get_property OFFSET $hit] ; set rng [get_property RANGE $hit]
    puts [format "  node_%s/M_AXI_DRAM -> xache S0%s  offset %s range %s" $mid $mid $off $rng]
    if {[expr {$off}] != 0 || [expr {$rng}] < ($DRAM_GB << 30)} { bad "node_$mid's Xache aperture is not the flat space at 0" }
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
if {$fail} { error "v8t BD verify: $fail check(s) FAILED" }
puts "@@@ v8t BD verify: all checks passed"

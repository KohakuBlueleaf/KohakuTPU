# Verify the BUILT block design without synthesising it: every clock net,
# reset net, module, interlink hop and address segment read back from the BD
# against the plan. 70_analyze repeats the checks on the netlist after synth.
#   vivado -mode batch -source scripts/tcl/v8t3/75_verify_bd.tcl

set here [file dirname [file normalize [info script]]]
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
# IPI lowercases a module reference's pin names when it repackages it, so the
# comparison is on case-folded names -- get_bd_pins matched them either way.
proc want {pin w} {
    set got [srcpin $pin]
    puts [format "  %-40s <- %s" $pin $got]
    if {[string tolower $got] ne [string tolower $w]} { bad "$pin driven by $got, want $w" }
}
# Pins and ports are different object types and one Vivado list cannot hold
# both (Common 17-224), so each is walked as names.
proc peer_of {intf_pin} {
    set n [get_bd_intf_nets -quiet -of_objects [get_bd_intf_pins -quiet $intf_pin]]
    if {![llength $n]} { return "UNCONNECTED" }
    set me [string trimleft $intf_pin /]
    foreach p [get_bd_intf_pins -quiet -of_objects $n] {
        set s [string trimleft "$p" /]
        if {$s ne $me} { return $s }
    }
    foreach p [get_bd_intf_ports -quiet -of_objects $n] {
        set s [string trimleft "$p" /]
        if {$s ne $me} { return $s }
    }
    return "self"
}

# ---- meshes: module, parameters, clocks, resets --------------------------
puts "\n=== meshes ==="
foreach {mid mod} $MESHES {
    set c [get_bd_cells -quiet mesh_$mid]
    if {![llength $c]} { bad "mesh_$mid absent" ; continue }
    set ref [get_property VLNV $c]
    puts [format "  mesh_%s  %s  MESH_ID=%s ILINK=%s PORTS=%s banks=%s entries=%s DRAM_CDC=%s DRAM_AR_MAX=%s" $mid $ref \
          [get_property CONFIG.MESH_ID $c] [get_property -quiet CONFIG.ILINK $c] \
          [get_property -quiet CONFIG.PORTS $c] [get_property -quiet CONFIG.L2_MAG_BANKS $c] \
          [get_property -quiet CONFIG.L2_MAG_ENTRIES $c] [get_property -quiet CONFIG.DRAM_CDC $c] \
          [get_property -quiet CONFIG.DRAM_AR_MAX $c]]
    if {[get_property CONFIG.MESH_ID $c] ne "$mid"} { bad "mesh_$mid has MESH_ID [get_property CONFIG.MESH_ID $c]" }
    if {![string match "*module_ref:$mod:*" $ref]} { bad "mesh_$mid is $ref, want $mod" }
    if {[get_property -quiet CONFIG.DRAM_CDC $c] ne "0"} { bad "mesh_$mid DRAM_CDC is not 0" }
    # The node's AR split and the Xache's read slot are one value: a burst the
    # slot cannot hold is a protocol error at the Xache.
    if {[get_property -quiet CONFIG.DRAM_AR_MAX $c] ne "$KX_RB_BEATS"} { bad "mesh_$mid DRAM_AR_MAX is not KX_RB_BEATS ($KX_RB_BEATS)" }
    if {[get_property -quiet CONFIG.L2_MAG_BANKS $c] ne "$L2_MAG_BANKS"} { bad "mesh_$mid staging banks" }
    if {[get_property -quiet CONFIG.L2_MAG_ENTRIES $c] ne "$L2_MAG_ENTRIES"} { bad "mesh_$mid staging entries" }
    want mesh_$mid/axi_aclk     [v8_sys_clk $mid]
    want mesh_$mid/axi_aresetn  [v8_rstn $mid]
    want mesh_$mid/dram_aclk    [v8_sys_clk $mid]
    want mesh_$mid/dram_aresetn [v8_rstn $mid]
    if {[get_property -quiet CONFIG.ILINK $c] ne "$NODE_ILINK"} { bad "mesh_$mid ILINK is not $NODE_ILINK" }
}
# v8t3 has no mesh at all: no mesh clock domain and none of its cells.
foreach n {div2_mesh0 dclr_mesh0 div2_mesh1 dclr_mesh1} {
    if {[llength [get_bd_cells -quiet $n]]} { bad "$n exists: v8t3 carries no matmul pump" }
}

# ---- the interlink chain: mesh i's LINK1 faces mesh i+1's LINK0 ------------
puts "\n=== interlink ==="
foreach hop {{0 1} {1 2} {2 3}} {
    lassign $hop lo hi
    foreach {name sp dp tx rx} [list pipe_${lo}_to_${hi} mesh_$lo/LINK1_OUT mesh_$hi/LINK0_IN $lo $hi \
                                     pipe_${hi}_to_${lo} mesh_$hi/LINK0_OUT mesh_$lo/LINK1_IN $hi $lo] {
        set c [get_bd_cells -quiet $name]
        if {![llength $c]} { bad "$name absent" ; continue }
        if {![string match "*module_ref:kts_pipe_bd:*" [get_property VLNV $c]]} { bad "$name is not kts_pipe_bd" }
        foreach {p w} [list W $IL_W VCW $IL_VCW CN_W $IL_CN_W ASYNC $IL_ASYNC CRED $IL_CRED STAGES $IL_STAGES] {
            if {[get_property -quiet CONFIG.$p $c] ne "$w"} { bad "$name $p is [get_property -quiet CONFIG.$p $c], want $w" }
        }
        want $name/clk     [v8_sys_clk $tx]
        want $name/clk_rx  [v8_sys_clk $rx]
        want $name/rstn_tx [v8_rstn $tx]
        want $name/rstn_rx [v8_rstn $rx]
        foreach f {valid vc last flit} {
            want $name/i_$f ${sp}_$f
            want ${dp}_$f   $name/o_$f
        }
        foreach f {crd_valid crd_vc crd_n} {
            want $name/i_$f ${dp}_$f
            want ${sp}_$f   $name/o_$f
        }
    }
}
foreach {m side} {0 LINK0 3 LINK1} {
    foreach f {valid vc last} { want mesh_$m/${side}_IN_$f zero_1/dout }
    want mesh_$m/${side}_IN_flit zero_flit/dout
    foreach f {crd_valid crd_vc} { want mesh_$m/${side}_OUT_$f zero_1/dout }
    want mesh_$m/${side}_OUT_crd_n zero_4/dout
}
if {[llength [get_bd_cells -quiet pipe_*]] != 6} { bad "[llength [get_bd_cells -quiet pipe_*]] pipes, want 6" }

# ---- the MIGs: ddr4_s IS the controller whose pins are in SLR s ------------
puts "\n=== MIGs by SLR ==="
foreach {mid mod} $MESHES {
    set port [dict get $DDR_PORT_OF_SLR $mid]
    set c [get_bd_cells -quiet ddr4_$mid]
    if {![llength $c]} { bad "ddr4_$mid absent" ; continue }
    set got [peer_of ddr4_$mid/C0_DDR4]
    puts "  ddr4_$mid/C0_DDR4 <-> $got  (SLR$mid holds board c$port)"
    if {$got ne "c${port}_ddr4"} { bad "ddr4_$mid is wired to $got, want board port c${port}_ddr4" }
    if {[peer_of ddr4_$mid/C0_SYS_CLK] ne "c${port}_sys"} { bad "ddr4_$mid's reference is not c${port}_sys" }
    if {[get_property -quiet CONFIG.C0.DDR4_AxiIDWidth $c] ne "$KX_DRAM_IDW"} { bad "ddr4_$mid AXI ID width" }
    want rst_ddr4_$mid/slowest_sync_clk ddr4_$mid/c0_ddr4_ui_clk
    want rst_ddr4_$mid/ext_reset_in     ddr4_$mid/c0_ddr4_ui_clk_sync_rst
    want ddr4_$mid/c0_ddr4_aresetn      rst_ddr4_$mid/peripheral_aresetn
    want ddr4_$mid/sys_rst              ddr_sys_rst/Res
}

# ---- the Xache -----------------------------------------------------------
puts "\n=== Kohaku Xache (kx_pxache) ==="
set kx [get_bd_cells -quiet xache]
if {![llength $kx]} { bad "xache absent" } else {
    if {![string match "*module_ref:kx_pbd_4x4:*" [get_property VLNV $kx]]} { bad "xache is not kx_pbd_4x4" }
    foreach {p w} [list SETS $KX_SETS SET_W $KX_SET_W K $KX_K BANKS $KX_BANKS RD_OUTQ $KX_RD_OUTQ \
                        WR_OUTQ $KX_WR_OUTQ RB_BEATS $KX_RB_BEATS \
                        HOP_DEPTH $KX_HOP_DEPTH HOP_RXREG $KX_HOP_RXREG \
                        NSWAP $KX_NSWAP CDC_DEPTH $KX_CDC_DEPTH PCLK $KX_PCLK \
                        MEM_TRUNK $KX_MEM_TRUNK MEM_RB $KX_MEM_RB \
                        MEM_HRD $KX_MEM_HRD MEM_HWR $KX_MEM_HWR] {
        set got [get_property -quiet CONFIG.$p $kx]
        puts [format "  %-10s %s" $p $got]
        if {$got ne "$w"} { bad "xache $p is $got, want $w" }
    }
    if {[llength [get_bd_pins -quiet xache/aclk]]} { bad "xache still has an aclk pin" }
    foreach {mid mod} $MESHES {
        want xache/p_clk$mid  [v8_sys_clk $mid]
        want xache/d_rstn$mid [v8_rstn $mid]
        want xache/h_clk$mid  ddr4_$mid/c0_ddr4_ui_clk
        want xache/h_rstn$mid rst_ddr4_$mid/peripheral_aresetn
        if {[peer_of xache/S0${mid}_AXI] ne "mesh_$mid/M_AXI_DRAM"} { bad "xache/S0${mid}_AXI is not mesh_$mid's DRAM master" }
        if {[peer_of xache/M0${mid}_AXI] ne "ddr4_$mid/C0_DDR4_S_AXI"} { bad "xache/M0${mid}_AXI is not ddr4_$mid" }
        puts "  S0${mid}_AXI <- mesh_$mid/M_AXI_DRAM ; M0${mid}_AXI -> ddr4_$mid"
    }
}

# ---- one clock, one reset: every PSR's clock and lock, both trees ----------
puts "\n=== resets ==="
set ext [expr {$PER_DIE_CLK ? "lock_all/Res" : "clk_wiz_ctrl/locked"}]
set psrw [list rst_ctrl $CTRL_CLK clk_wiz_ctrl/locked]
if {$PER_DIE_CLK} {
    foreach {mid mod} $MESHES {
        lappend psrw rst_bus$mid [v8_bus_clk $mid] clk_wiz_ctrl/locked
        lappend psrw rst_sys$mid [v8_sys_clk $mid] [v8_sys_lock $mid]
    }
} else {
    lappend psrw rst_bus [v8_bus_clk 0] clk_wiz_ctrl/locked
    lappend psrw rst_sys [v8_sys_clk 0] [v8_sys_lock 0]
}
foreach {name clkw lockw} $psrw {
    want $name/slowest_sync_clk $clkw
    want $name/dcm_locked $lockw
    want $name/ext_reset_in $ext
}
if {$PER_DIE_CLK} {
    # No die leaves reset until every wizard has locked.
    want lock_cat/In0 clk_wiz_ctrl/locked
    foreach {mid mod} $MESHES {
        want lock_cat/In[expr {$mid + 1}] clk_wiz_mesh$mid/locked
    }
    want lock_all/Op1 lock_cat/dout
    foreach t {rst_tree rst_tree_bus} {
        if {[llength [get_bd_cells -quiet $t]]} { bad "$t exists: no reset spans a die at PER_DIE_CLK 1" }
    }
} else {
    want rst_tree/clk         [v8_sys_clk 0]
    want rst_tree/rstn_in     rst_sys/peripheral_aresetn
    want rst_tree_bus/clk     [v8_bus_clk 0]
    want rst_tree_bus/rstn_in rst_bus/peripheral_aresetn
}
foreach {mid mod} $MESHES {
    want bus_rst_inv$mid/Op1 [v8_bus_rstn $mid]
}
set npsr [llength [get_bd_cells -quiet rst_*]]
# ctrl + 4 MIG + bus + sys, the last two per die at PER_DIE_CLK 1; at 0 the two
# trees are modules, not proc_sys_reset, and count here too.
set nwant [expr {$PER_DIE_CLK ? 13 : 9}]
if {$npsr != $nwant} { bad "$npsr rst_* cells, want $nwant" }
foreach {mid mod} $MESHES {
    set has [llength [get_bd_pins -quiet clk_wiz_mesh$mid/clk_out4]]
    if {($PER_DIE_CLK || $mid == $SYS_WIZ) && !$has} { bad "clk_wiz_mesh$mid lacks clk_out4, the sysnode clock" }
    if {!$PER_DIE_CLK && $mid != $SYS_WIZ && $has}  { bad "clk_wiz_mesh$mid has a clk_out4" }
    if {![llength [get_bd_pins -quiet [v8_bus_clk $mid]]]} { bad "[v8_bus_clk $mid] does not exist" }
}
if {[llength [get_bd_cells -quiet clk_wiz_bus*]]} { bad "a per-die bus wizard exists" }

# ---- station bus, per die -------------------------------------------------
puts "\n=== station bus ==="
set sb [get_bd_cells -quiet station_bus]
if {![string match "*module_ref:$SB_WRAP:*" [get_property VLNV $sb]]} { bad "station_bus is not $SB_WRAP" }
foreach {p w} [list FW $FW OST $OST LINK_CDC $LINK_CDC LINK_FULL $LINK_FULL LINK_KTS $LINK_KTS \
                    MGR0_DOM $MGR0_DOM CRED $CRED PIPE $PIPE SEG_OVERRIDE 1 \
                    LUT_PER_BRAM $LUT_PER_BRAM LPB1 $SB_LPB1 \
                    MREQ0 $SB_MREQ0 MREQ1 $SB_MREQ1 MREQ2 $SB_MREQ2 \
                    MRSP0 $SB_MRSP0 MRSP1 $SB_MRSP1 MRSP2 $SB_MRSP2 \
                    MMAXB0 $SB_MMAXB0 MMAXB1 $SB_MMAXB1 MMAXB2 $SB_MMAXB2] {
    set got [get_property -quiet CONFIG.$p $sb]
    if {$got ne "$w"} { bad "station_bus $p is $got, want $w" }
}
foreach {nm w} [list SEG_BASE_P [v8_cat $seg_base $AW] SEG_MASK_P [v8_cat $seg_mask $AW] \
                     SEG_XLT_P [v8_cat $seg_xlt $AW] SEG_DST_P [v8_cat $seg_dst 2] SEG_DPORT_P [v8_cat $seg_dprt 2]] {
    if {[get_property -quiet CONFIG.$nm $sb] ne $w} { bad "station_bus $nm differs from the intended table" }
}
ok "station segment literals match the intended table"
foreach {mid mod} $MESHES {
    want station_bus/clk_s$mid     [v8_sys_clk $mid]
    want station_bus/aresetn_s$mid [v8_rstn $mid]
    want station_bus/bus_clk$mid   [v8_bus_clk $mid]
    want station_bus/bus_rst$mid   bus_rst_inv$mid/Res
    want station_bus/clk_ddr$mid   ddr4_$mid/c0_ddr4_ui_clk
    want station_bus/aresetn_ddr$mid rst_ddr4_$mid/peripheral_aresetn
    want dwc_ctrl$mid/s_axi_aclk    [v8_sys_clk $mid]
    want dwc_ctrl$mid/s_axi_aresetn [v8_rstn $mid]
    want clk_wiz_mesh$mid/s_axi_aclk    $CTRL_CLK
    want clk_wiz_mesh$mid/s_axi_aresetn rst_ctrl/peripheral_aresetn
    foreach {port peer} [list [expr {$mid*$NQ+0}] mesh_$mid/S_AXI_MEM \
                              [expr {$mid*$NQ+1}] dwc_ctrl$mid/S_AXI \
                              [expr {$mid*$NQ+2}] ddr4_$mid/C0_DDR4_S_AXI_CTRL \
                              [expr {$mid*$NQ+3}] clk_wiz_mesh$mid/s_axi_lite] {
        set got [peer_of station_bus/[format M%02d_AXI $port]]
        if {$got ne $peer} { bad "station port $port is $got, not $peer" }
    }
    if {[peer_of dwc_ctrl$mid/M_AXI] ne "mesh_$mid/S_AXI_CTRL"} { bad "dwc_ctrl$mid does not reach mesh_$mid/S_AXI_CTRL" }
}
want station_bus/clk_ctrl     $CTRL_CLK
want station_bus/aresetn_ctrl rst_ctrl/peripheral_aresetn
want station_bus/clk_xdma     xdma_0/axi_aclk
want jtag_ctrl/aclk           [v8_bus_clk 1]
want jtag_ctrl/aresetn        [v8_bus_rstn 1]
if {[peer_of jtag_ctrl/M_AXI] ne "station_bus/S00_AXI"} { bad "jtag_ctrl is not S00" }
if {[peer_of xdma_0/M_AXI] ne "station_bus/S01_AXI"} { bad "xdma M_AXI is not S01" }
if {[peer_of xdma_0/M_AXI_LITE] ne "station_bus/S02_AXI"} { bad "xdma M_AXI_LITE is not S02" }
ok "every station port on its intended endpoint"

# ---- the address map ------------------------------------------------------
puts "\n=== address map ==="
puts "  excluded segments: [llength [get_bd_addr_segs -quiet -excluded]]"
foreach {mid mod} $MESHES {
    set segs [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet mesh_$mid/M_AXI_DRAM]]
    set hit "" ; foreach s $segs { if {[string match "*xache*" $s]} { set hit $s } }
    if {$hit eq ""} { bad "mesh_$mid/M_AXI_DRAM not mapped onto the Xache ($segs)" ; continue }
    if {[llength $segs] != 1} { bad "mesh_$mid/M_AXI_DRAM has [llength $segs] segments, want the Xache alone: $segs" }
    set off [get_property OFFSET $hit] ; set rng [get_property RANGE $hit]
    puts [format "  mesh_%s/M_AXI_DRAM -> xache S0%s  offset %s range %s" $mid $mid $off $rng]
    if {[expr {$off}] != 0 || [expr {$rng}] < ($DRAM_GB << 30)} { bad "mesh_$mid's Xache aperture is not the flat space at 0" }
    set dsegs [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet xache/M0${mid}_AXI]]
    set dhit "" ; foreach s $dsegs { if {[string match "*ddr4_$mid*" $s]} { set dhit $s } }
    if {$dhit eq ""} { bad "xache/M0${mid}_AXI not mapped onto ddr4_$mid" ; continue }
    set doff [get_property OFFSET $dhit] ; set drng [get_property RANGE $dhit]
    puts [format "  xache/M0%s_AXI -> ddr4_%s  offset %s range %s" $mid $mid $doff $drng]
    if {[expr {$doff}] != 0 || [expr {$drng}] != (4 << 30)} { bad "ddr4_$mid behind home $mid is not 4 GB at 0" }
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
if {$fail} { error "$design_name BD verify:$fail check(s) FAILED" }
puts "@@@ v8t3 BD verify: all checks passed"

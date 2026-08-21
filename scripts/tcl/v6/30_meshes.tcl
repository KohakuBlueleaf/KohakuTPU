# Meshes, their DDR4 controllers, and the mag_link chain between them.

foreach i {0 1 2 3} {
    if {![v6_has_ddr $i]} { continue }
    create_bd_intf_port -mode Master -vlnv xilinx.com:interface:ddr4_rtl:1.0 c${i}_ddr4
    set p [create_bd_intf_port -mode Slave \
               -vlnv xilinx.com:interface:diff_clock_rtl:1.0 c${i}_sys]
    set_property CONFIG.FREQ_HZ {400160000} $p
}

# The MIGs come out of reset when the control MMCM locks. Left unconnected they
# are tied to 0 (BD 41-759) and never reset at all.
set sysrst [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic ddr_sys_rst]
set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] $sysrst
connect_bd_net [get_bd_pins clk_wiz_ctrl/locked] [get_bd_pins ddr_sys_rst/Op1]

foreach i {0 1 2 3} {
    if {![v6_has_ddr $i]} { continue }
    set d [create_bd_cell -type ip -vlnv xilinx.com:ip:ddr4 ddr4_$i]
    set_property -dict [list \
      CONFIG.C0.DDR4_DataWidth {72} CONFIG.C0.DDR4_InputClockPeriod {2499} \
      CONFIG.C0.DDR4_MemoryPart {MT40A512M16HA-083E} \
      CONFIG.C0.DDR4_TimePeriod {833} CONFIG.C0.DDR4_isCustom {true}] $d
    create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_ddr4_$i
    connect_bd_intf_net [get_bd_intf_ports c${i}_ddr4] [get_bd_intf_pins ddr4_$i/C0_DDR4]
    connect_bd_intf_net [get_bd_intf_ports c${i}_sys]  [get_bd_intf_pins ddr4_$i/C0_SYS_CLK]
    connect_bd_net [get_bd_pins ddr_sys_rst/Res] [get_bd_pins ddr4_$i/sys_rst]
}

# A knob applies only where the module declares it, so this survives an RTL
# revision that has not landed the parameter yet.
proc v6_set_if {cell name value} {
    if {[llength [list_property [get_bd_cells $cell] CONFIG.$name]]} {
        set_property CONFIG.$name $value [get_bd_cells $cell]
        return 1
    }
    puts "@@@ $cell: no CONFIG.$name declared"
    return 0
}

foreach {mid mod} $MESHES {
    if {![v6_has_mesh $mid]} { continue }
    set ddr [dict get $DDR_OF_SLR $mid]
    create_bd_cell -type module -reference $mod mesh_$mid
    set_property -dict [list CONFIG.MESH_ID $mid CONFIG.GA {512} CONFIG.GB {512} \
                             CONFIG.TILES {4096} CONFIG.TILE_PRIM {ultra} \
                             CONFIG.VEC_PRIM {block} CONFIG.MAG_CDC {1} \
                             CONFIG.UNIT_CDC {1}] [get_bd_cells mesh_$mid]
    v6_set_if mesh_$mid L2_MAG_BANKS   $L2_MAG_BANKS
    v6_set_if mesh_$mid L2_MAG_ENTRIES $L2_MAG_ENTRIES
    v6_set_if mesh_$mid L2_CU_DEPTH    $L2_CU_DEPTH
    v6_set_if mesh_$mid L2_VEC_DEPTH   $L2_VEC_DEPTH

    connect_bd_intf_net [get_bd_intf_pins mesh_$mid/M_AXI_DRAM] \
                        [get_bd_intf_pins ddr4_$ddr/C0_DDR4_S_AXI]

    # axi_aclk IS the AXI ports' clock and they live in MAG, so it takes MAG's
    # clk_out4; clk_out1 is the NoC fabric and clk_out3 the vector core.
    connect_bd_net [get_bd_pins clk_wiz_mesh$mid/clk_out4] \
                   [get_bd_pins mesh_$mid/axi_aclk]
    connect_bd_net [get_bd_pins clk_wiz_mesh$mid/clk_out1] \
                   [get_bd_pins mesh_$mid/noc_clk]
    connect_bd_net [get_bd_pins clk_wiz_mesh$mid/clk_out3] \
                   [get_bd_pins mesh_$mid/vec_clk]
    # axi_aresetn is NOT connected here -- it waits on the link CDCs below.

    # The 2x drives the matmul AND a BUFGCE_DIV that makes its 1x partner. Both
    # halves must come off the same divider or the pump pair is not aligned.
    create_bd_cell -type module -reference ktpu_div2 div2_mesh$mid
    connect_bd_net [get_bd_pins clk_wiz_mesh$mid/clk_out2] \
                   [get_bd_pins div2_mesh$mid/clk2x] \
                   [get_bd_pins mesh_$mid/mat_clk2x]
    connect_bd_net [get_bd_pins div2_mesh$mid/clk1x] \
                   [get_bd_pins mesh_$mid/mat_clk]

    # CLR off `locked`, never off a mesh reset: that reset is clocked BY this
    # divider. The mesh has no CLR -- every pumped CU takes both halves here.
    set dc [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic dclr_mesh$mid]
    set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] $dc
    connect_bd_net [get_bd_pins clk_wiz_mesh$mid/locked] [get_bd_pins dclr_mesh$mid/Op1]
    connect_bd_net [get_bd_pins dclr_mesh$mid/Res] \
                   [get_bd_pins div2_mesh$mid/clr]

    connect_bd_net [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk] \
                   [get_bd_pins mesh_$mid/dram_aclk] \
                   [get_bd_pins rst_ddr4_$ddr/slowest_sync_clk]
    connect_bd_net [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk_sync_rst] \
                   [get_bd_pins rst_ddr4_$ddr/ext_reset_in]
    connect_bd_net [get_bd_pins rst_ddr4_$ddr/peripheral_aresetn] \
                   [get_bd_pins ddr4_$ddr/c0_ddr4_aresetn] \
                   [get_bd_pins mesh_$mid/dram_aresetn]
}

# Per-mesh clocks make every hop ASYNCHRONOUS, and mag_link ties tready high,
# so a direct net would sample a foreign domain in silence.
set ::V6_CDC_READY {}
set ::V6_CDC_FAULT {}
proc v6_link_cdc {name src dst src_wiz dst_wiz} {
    create_bd_cell -type module -reference mag_link_cdc $name
    connect_bd_intf_net [get_bd_intf_pins $src] [get_bd_intf_pins $name/S_AXIS]
    connect_bd_intf_net [get_bd_intf_pins $name/M_AXIS] [get_bd_intf_pins $dst]
    # clk_out4: the AXIS link ports come out of mag_ilink, inside mag, so they
    # are in the MAG domain and not the fabric's.
    connect_bd_net [get_bd_pins $src_wiz/clk_out4] [get_bd_pins $name/s_axis_aclk]
    connect_bd_net [get_bd_pins $dst_wiz/clk_out4] [get_bd_pins $name/m_axis_aclk]
    lappend ::V6_CDC_READY [get_bd_pins $name/ready]
    lappend ::V6_CDC_FAULT [get_bd_pins $name/fault]
}

# The link CDCs take their OWN ungated reset (locked only, on the link/MAG
# clock): their ready flags clear only after their reset releases, so wiring
# them from rst_mesh* while rst_mesh* waits on cdc_ready_all would deadlock
# the release. This is the feedback the Change-1 gating must stay out of.
foreach {mid mod} $MESHES {
    if {$PROBE_SLR >= 0 || [info exists ::V7MINI]} { break }
    v6_psr rst_cdc$mid clk_wiz_mesh$mid/clk_out4 clk_wiz_mesh$mid/locked
}

foreach hop {{0 1} {1 2} {2 3}} {
    lassign $hop lo hi
    if {![v6_has_mesh $lo] || ![v6_has_mesh $hi]} { continue }
    v6_link_cdc cdc_${lo}_to_${hi} mesh_$lo/M_AXIS_LINK1 mesh_$hi/S_AXIS_LINK0 \
                clk_wiz_mesh$lo clk_wiz_mesh$hi
    v6_link_cdc cdc_${hi}_to_${lo} mesh_$hi/M_AXIS_LINK0 mesh_$lo/S_AXIS_LINK1 \
                clk_wiz_mesh$hi clk_wiz_mesh$lo
    connect_bd_net [get_bd_pins rst_cdc$lo/peripheral_aresetn] \
                   [get_bd_pins cdc_${lo}_to_${hi}/s_axis_aresetn] \
                   [get_bd_pins cdc_${hi}_to_${lo}/m_axis_aresetn]
    connect_bd_net [get_bd_pins rst_cdc$hi/peripheral_aresetn] \
                   [get_bd_pins cdc_${lo}_to_${hi}/m_axis_aresetn] \
                   [get_bd_pins cdc_${hi}_to_${lo}/s_axis_aresetn]
}

# Reset rule (owner, 2026-08-20): NO cdc_ready signaling, ever. A mesh's
# reset follows its OWN wizard's lock (dcm_locked, wired in v6_psr) plus the
# boot-time ctrl lock on ext_reset_in, so a relock on one mesh can never
# reset another. The link CDCs run on their own ungated rst_cdcN. v6.5
# shipped a cdc_ready_all AND-gate here; measured on hardware it made every
# wizard relock ripple all four meshes, and it is deleted, not disabled.
foreach {mid mod} $MESHES {
    if {![v6_has_mesh $mid]} { continue }
    connect_bd_net [get_bd_pins rst_mesh$mid/peripheral_aresetn] \
                   [get_bd_pins mesh_$mid/axi_aresetn]
}

puts "@@@ v6 meshes: [llength [get_bd_cells -quiet mesh_*]], [llength $::V6_CDC_READY] link CDCs (ready flags unconsumed by design)"

# The four MIGs, the four meshes with their nodes, and the interlink chain.
# M_AXI_DRAM is NOT connected here: 35_xache.tcl puts the Xache between every
# node and every channel.

foreach i {0 1 2 3} {
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
    set d [create_bd_cell -type ip -vlnv xilinx.com:ip:ddr4 ddr4_$i]
    # The AXI ID carries the Xache master index on top of the node's 4 bits.
    set_property -dict [list \
      CONFIG.C0.DDR4_DataWidth {72} CONFIG.C0.DDR4_InputClockPeriod {2499} \
      CONFIG.C0.DDR4_MemoryPart {MT40A512M16HA-083E} \
      CONFIG.C0.DDR4_TimePeriod {833} CONFIG.C0.DDR4_isCustom {true} \
      CONFIG.C0.DDR4_AxiIDWidth $KX_DRAM_IDW] $d
    # One reset per MIG clock: rst_ddr4_i on ui_clk, released by the MIG.
    create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_ddr4_$i
    connect_bd_intf_net [get_bd_intf_ports c${i}_ddr4] [get_bd_intf_pins ddr4_$i/C0_DDR4]
    connect_bd_intf_net [get_bd_intf_ports c${i}_sys]  [get_bd_intf_pins ddr4_$i/C0_SYS_CLK]
    connect_bd_net [get_bd_pins ddr_sys_rst/Res] [get_bd_pins ddr4_$i/sys_rst]
    connect_bd_net [get_bd_pins ddr4_$i/c0_ddr4_ui_clk] \
                   [get_bd_pins rst_ddr4_$i/slowest_sync_clk]
    connect_bd_net [get_bd_pins ddr4_$i/c0_ddr4_ui_clk_sync_rst] \
                   [get_bd_pins rst_ddr4_$i/ext_reset_in]
    connect_bd_net [get_bd_pins rst_ddr4_$i/peripheral_aresetn] \
                   [get_bd_pins ddr4_$i/c0_ddr4_aresetn]
}

# A knob applies only where the module declares it, so this survives an RTL
# revision that has not landed the parameter yet.
proc v8_set_if {cell name value} {
    if {[llength [list_property [get_bd_cells $cell] CONFIG.$name]]} {
        set_property CONFIG.$name $value [get_bd_cells $cell]
        return 1
    }
    puts "@@@ $cell: no CONFIG.$name declared"
    return 0
}

foreach {mid mod} $MESHES {
    set ddr [dict get $DDR_OF_SLR $mid]
    create_bd_cell -type module -reference $mod mesh_$mid
    set_property -dict [list CONFIG.MESH_ID $mid CONFIG.GA {512} CONFIG.GB {512} \
                             CONFIG.TILES {4096} CONFIG.TILE_PRIM {ultra} \
                             CONFIG.VEC_PRIM {block} CONFIG.MAG_CDC {1} \
                             CONFIG.UNIT_CDC {1}] [get_bd_cells mesh_$mid]
    v8_set_if mesh_$mid L2_MAG_BANKS   $L2_MAG_BANKS
    v8_set_if mesh_$mid L2_MAG_ENTRIES $L2_MAG_ENTRIES
    if {![v8_set_if mesh_$mid DRAM_CDC $DRAM_CDC]} {
        error "mesh_$mid ($mod) has no DRAM_CDC: regenerate the tops"
    }
    # A top that still declared an L2 adapter depth would take one here; the
    # nol2 tops declare none, and 70_analyze checks that.

    # axi_aclk IS the AXI ports' clock and they live in the node: the ONE
    # sysnode clock, with the ONE sysnode reset.
    connect_bd_net [get_bd_pins $SYS_CLK] [get_bd_pins mesh_$mid/axi_aclk]
    connect_bd_net [get_bd_pins [v8_rstn $mid]] [get_bd_pins mesh_$mid/axi_aresetn]
    connect_bd_net [get_bd_pins clk_wiz_mesh$mid/clk_out1] \
                   [get_bd_pins mesh_$mid/noc_clk]
    connect_bd_net [get_bd_pins clk_wiz_mesh$mid/clk_out3] \
                   [get_bd_pins mesh_$mid/vec_clk]

    # The 2x drives the matmul AND a BUFGCE_DIV that makes its 1x partner. Both
    # halves must come off the same divider or the pump pair is not aligned.
    create_bd_cell -type module -reference ktpu_div2 div2_mesh$mid
    connect_bd_net [get_bd_pins clk_wiz_mesh$mid/clk_out2] \
                   [get_bd_pins div2_mesh$mid/clk2x] \
                   [get_bd_pins mesh_$mid/mat_clk2x]
    connect_bd_net [get_bd_pins div2_mesh$mid/clk1x] \
                   [get_bd_pins mesh_$mid/mat_clk]
    # CLR off `locked`, never off a reset clocked BY this divider.
    set dc [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic dclr_mesh$mid]
    set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] $dc
    connect_bd_net [get_bd_pins clk_wiz_mesh$mid/locked] [get_bd_pins dclr_mesh$mid/Op1]
    connect_bd_net [get_bd_pins dclr_mesh$mid/Res] [get_bd_pins div2_mesh$mid/clr]

    # The node's DRAM master is on the sysnode clock too (DRAM_CDC 0): it
    # meets the Xache with no crossing. The MIG clock reaches the node nowhere.
    if {$DRAM_CDC} { error "v8 is the one-clock memory path; DRAM_CDC must be 0" }
    connect_bd_net [get_bd_pins $SYS_CLK] [get_bd_pins mesh_$mid/dram_aclk]
    connect_bd_net [get_bd_pins [v8_rstn $mid]] [get_bd_pins mesh_$mid/dram_aresetn]
}

# The interlink chain 0-1-3-2 on the ONE sysnode clock: a registered pipe per
# hop and direction (mag_link_pipe_bd), no CDC FIFO. tready is tied high on
# the way in and ignored on the way out, as mag_link requires.
proc v8_link_pipe {name src dst die} {
    global SYS_CLK IL_PIPE
    create_bd_cell -type module -reference mag_link_pipe_bd $name
    set_property CONFIG.DEPTH $IL_PIPE [get_bd_cells $name]
    connect_bd_intf_net [get_bd_intf_pins $src] [get_bd_intf_pins $name/S_AXIS]
    connect_bd_intf_net [get_bd_intf_pins $name/M_AXIS] [get_bd_intf_pins $dst]
    connect_bd_net [get_bd_pins $SYS_CLK] [get_bd_pins $name/aclk]
    # the reset of the die the pipe leaves from
    connect_bd_net [get_bd_pins [v8_rstn $die]] [get_bd_pins $name/aresetn]
}
foreach hop {{0 1} {1 2} {2 3}} {
    lassign $hop lo hi
    v8_link_pipe pipe_${lo}_to_${hi} mesh_$lo/M_AXIS_LINK1 mesh_$hi/S_AXIS_LINK0 $lo
    v8_link_pipe pipe_${hi}_to_${lo} mesh_$hi/M_AXIS_LINK0 mesh_$lo/S_AXIS_LINK1 $hi
}
puts "@@@ v8 meshes: [llength [get_bd_cells -quiet mesh_*]], 4 MIGs, 6 link pipes on the sysnode clock"

# The four MIGs and the four nodes. M_AXI_DRAM is NOT connected here:
# 35_xache.tcl puts the Xache between every node and every channel. No mesh,
# no interlink: nothing but the Xache and the station line crosses a die.

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

if {$DRAM_CDC} { error "v8t is the one-clock memory path; DRAM_CDC must be 0" }
foreach {mid mod} $MESHES {
    create_bd_cell -type module -reference $mod node_$mid
    set_property -dict [list CONFIG.MESH_ID $mid CONFIG.PORTS $NODE_PORTS \
                             CONFIG.L2_MAG_BANKS $L2_MAG_BANKS \
                             CONFIG.L2_MAG_ENTRIES $L2_MAG_ENTRIES \
                             CONFIG.DRAM_CDC $DRAM_CDC] [get_bd_cells node_$mid]
    # axi_aclk IS the AXI ports' clock: the ONE sysnode clock, with die mid's
    # copy of the ONE sysnode reset. The DRAM master is on it too (DRAM_CDC 0):
    # it meets the Xache with no crossing. The MIG clock reaches the node nowhere.
    connect_bd_net [get_bd_pins $SYS_CLK] [get_bd_pins node_$mid/axi_aclk]
    connect_bd_net [get_bd_pins [v8_rstn $mid]] [get_bd_pins node_$mid/axi_aresetn]
    connect_bd_net [get_bd_pins $SYS_CLK] [get_bd_pins node_$mid/dram_aclk]
    connect_bd_net [get_bd_pins [v8_rstn $mid]] [get_bd_pins node_$mid/dram_aresetn]
}
puts "@@@ v8t nodes: [llength [get_bd_cells -quiet node_*]], 4 MIGs, no mesh, no interlink"

# The station-bus line, its managers, and the per-station endpoints. Ports are
# fixed by sb_line4 port_dom(): 0,1 the sysnode clock; 2 that die's MIG ui_clk;
# 3 the control clock. All four stations and the JTAG manager run on the ONE
# system clock (clk_wiz_ctrl/clk_out2); each station takes its die's copy of
# the bus reset, and the links between stations are register pipes.

set bus [create_bd_cell -type module -reference $SB_WRAP station_bus]
set_property -dict [list CONFIG.FW $FW CONFIG.OST $OST \
    CONFIG.STORE_FWD $STORE_FWD CONFIG.LUT_PER_BRAM $LUT_PER_BRAM \
    CONFIG.TIMEOUT $TIMEOUT CONFIG.LINK_CDC $LINK_CDC \
    CONFIG.LINK_FULL $LINK_FULL CONFIG.LINK_KTS $LINK_KTS \
    CONFIG.MGR0_DOM $MGR0_DOM CONFIG.CRED $CRED CONFIG.PIPE $PIPE] $bus

foreach {mid mod} $MESHES {
    connect_bd_net [get_bd_pins $BUS_CLK] [get_bd_pins station_bus/bus_clk$mid]
    connect_bd_net [get_bd_pins bus_rst_inv$mid/Res] \
                   [get_bd_pins station_bus/bus_rst$mid]
    # S_AXI_MEM and S_AXI_CTRL are inside the node, on the sysnode clock.
    connect_bd_net [get_bd_pins $SYS_CLK] [get_bd_pins station_bus/clk_s$mid]
    connect_bd_net [get_bd_pins [v8_rstn $mid]] [get_bd_pins station_bus/aresetn_s$mid]
    # Port 2 of station s runs on ddr4_s's ui_clk, so the station meets the
    # controller in its own domain and no crossing IP sits between.
    connect_bd_net [get_bd_pins ddr4_$mid/c0_ddr4_ui_clk] \
                   [get_bd_pins station_bus/clk_ddr$mid]
    connect_bd_net [get_bd_pins rst_ddr4_$mid/peripheral_aresetn] \
                   [get_bd_pins station_bus/aresetn_ddr$mid]
}
connect_bd_net [get_bd_pins $CTRL_CLK] [get_bd_pins station_bus/clk_ctrl]
connect_bd_net [get_bd_pins rst_ctrl/peripheral_aresetn] \
               [get_bd_pins station_bus/aresetn_ctrl]

# ---- managers ------------------------------------------------------------
# PROTOCOL 0 is AXI4; 64-bit addressing (the station field is at AW-4 = 39)
# and 64-bit data, stated, not defaulted (the IP defaults to 32). On the
# system clock, with station 1's copy of the bus reset: same domain as the NMU.
proc v8_jtag {name clkpin rstpin} {
    set j [create_bd_cell -type ip -vlnv xilinx.com:ip:jtag_axi $name]
    set_property -dict [list CONFIG.PROTOCOL {0} CONFIG.M_AXI_ID_WIDTH {4} \
                             CONFIG.M_AXI_ADDR_WIDTH {64} \
                             CONFIG.M_AXI_DATA_WIDTH {64}] $j
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/aclk]
    connect_bd_net [get_bd_pins $rstpin] [get_bd_pins $name/aresetn]
}
v8_jtag jtag_ctrl $BUS_CLK [v8_bus_rstn 1]
connect_bd_intf_net [get_bd_intf_pins jtag_ctrl/M_AXI] \
                    [get_bd_intf_pins station_bus/S00_AXI]

set xdma [create_bd_cell -type ip -vlnv xilinx.com:ip:xdma xdma_0]
set_property -dict [list \
  CONFIG.axi_data_width {512_bit} CONFIG.axi_id_width {4} \
  CONFIG.axilite_master_en {true} CONFIG.axilite_master_scale {Megabytes} \
  CONFIG.axilite_master_size {16} CONFIG.axisten_freq {250} \
  CONFIG.functional_mode {DMA} CONFIG.mode_selection {Basic} \
  CONFIG.pcie_blk_locn {X0Y1} CONFIG.pf0_device_id {903F} \
  CONFIG.pf0_subsystem_id {0007} CONFIG.pf0_subsystem_vendor_id {10EE} \
  CONFIG.pl_link_cap_max_link_speed {8.0_GT/s} \
  CONFIG.pl_link_cap_max_link_width {X16} CONFIG.ref_clk_freq {100_MHz} \
  CONFIG.xdma_num_usr_irq {1} CONFIG.xdma_rnum_chnl $XDMA_RCHNL \
  CONFIG.xdma_wnum_chnl $XDMA_WCHNL] $xdma
# XDMA's OWN clock and reset: its masters are synchronous to the axi_aclk the
# PCIe core derives from the link -- an MMCM output here is a false domain.
connect_bd_net [get_bd_pins xdma_0/axi_aclk]    [get_bd_pins station_bus/clk_xdma]
connect_bd_net [get_bd_pins xdma_0/axi_aresetn] [get_bd_pins station_bus/aresetn_xdma]
connect_bd_intf_net [get_bd_intf_pins xdma_0/M_AXI] \
                    [get_bd_intf_pins station_bus/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins xdma_0/M_AXI_LITE] \
                    [get_bd_intf_pins station_bus/S02_AXI]

create_bd_intf_port -mode Slave  -vlnv xilinx.com:interface:diff_clock_rtl:1.0 pcie_clk
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:pcie_7x_mgt_rtl:1.0 pcie_lane
create_bd_port -dir O user_lnk_up
set pr [create_bd_port -dir I -type rst pcie_reset]
set_property CONFIG.POLARITY {ACTIVE_LOW} $pr
set udb [create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_pcie]
set_property CONFIG.C_BUF_TYPE {IBUFDSGTE} $udb
connect_bd_intf_net [get_bd_intf_ports pcie_clk] [get_bd_intf_pins util_ds_buf_pcie/CLK_IN_D]
connect_bd_intf_net [get_bd_intf_ports pcie_lane] [get_bd_intf_pins xdma_0/pcie_mgt]
connect_bd_net [get_bd_ports pcie_reset] [get_bd_pins xdma_0/sys_rst_n]
connect_bd_net [get_bd_pins util_ds_buf_pcie/IBUF_DS_ODIV2] [get_bd_pins xdma_0/sys_clk]
connect_bd_net [get_bd_pins util_ds_buf_pcie/IBUF_OUT]      [get_bd_pins xdma_0/sys_clk_gt]
connect_bd_net [get_bd_pins xdma_0/user_lnk_up] [get_bd_ports user_lnk_up]
connect_bd_net [get_bd_pins zero_1/dout] [get_bd_pins xdma_0/usr_irq_req]

# ---- endpoints: every die is real -----------------------------------------
foreach {mid mod} $MESHES {
    set p0 [format M%02d_AXI [expr {$mid * $NQ + 0}]]
    set p1 [format M%02d_AXI [expr {$mid * $NQ + 1}]]
    set p2 [format M%02d_AXI [expr {$mid * $NQ + 2}]]
    set p3 [format M%02d_AXI [expr {$mid * $NQ + 3}]]

    connect_bd_intf_net [get_bd_intf_pins station_bus/$p0] \
                        [get_bd_intf_pins mesh_$mid/S_AXI_MEM]

    # S_AXI_CTRL is 64-bit and gen_station_wrap emits only 32 or FW, so the
    # station cannot meet it natively. Converter on the sysnode clock/reset.
    set dc [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dwidth_converter dwc_ctrl$mid]
    set_property -dict [list CONFIG.SI_DATA_WIDTH {32} CONFIG.MI_DATA_WIDTH {64}] $dc
    connect_bd_net [get_bd_pins $SYS_CLK] [get_bd_pins dwc_ctrl$mid/s_axi_aclk]
    connect_bd_net [get_bd_pins [v8_rstn $mid]] [get_bd_pins dwc_ctrl$mid/s_axi_aresetn]
    connect_bd_intf_net [get_bd_intf_pins station_bus/$p1] \
                        [get_bd_intf_pins dwc_ctrl$mid/S_AXI]
    connect_bd_intf_net [get_bd_intf_pins dwc_ctrl$mid/M_AXI] \
                        [get_bd_intf_pins mesh_$mid/S_AXI_CTRL]

    # Direct: the station's port-2 domain IS this controller's ui_clk.
    connect_bd_intf_net [get_bd_intf_pins station_bus/$p2] \
                        [get_bd_intf_pins ddr4_$mid/C0_DDR4_S_AXI_CTRL]

    # The die's clock controller, reached on the FIXED control clock so a
    # retune never stands on the clock it is changing.
    connect_bd_intf_net [get_bd_intf_pins station_bus/$p3] \
                        [get_bd_intf_pins clk_wiz_mesh$mid/s_axi_lite]
    connect_bd_net [get_bd_pins $CTRL_CLK] \
                   [get_bd_pins clk_wiz_mesh$mid/s_axi_aclk]
    connect_bd_net [get_bd_pins rst_ctrl/peripheral_aresetn] \
                   [get_bd_pins clk_wiz_mesh$mid/s_axi_aresetn]
}
puts "@@@ v8t2 bus: $SB_WRAP, 3 managers, [expr {4 * $NQ}] endpoints, one system clock"

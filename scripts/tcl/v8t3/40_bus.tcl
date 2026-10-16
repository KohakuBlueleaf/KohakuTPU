# The station-bus line, its managers, and the per-station endpoints. Ports are
# fixed by sb_line4 port_dom(): 0,1 the sysnode clock; 2 that die's MIG ui_clk;
# 3 the control clock. All four stations and the JTAG manager run on the ONE
# system clock (clk_wiz_ctrl/clk_out2); each station takes its die's copy of
# the bus reset, and the links between stations are register pipes.

set bus [create_bd_cell -type module -reference $SB_WRAP station_bus]
set_property -dict [list CONFIG.FW $FW CONFIG.OST $OST \
    CONFIG.STORE_FWD $STORE_FWD CONFIG.LUT_PER_BRAM $LUT_PER_BRAM \
    CONFIG.LPB1 $SB_LPB1 \
    CONFIG.MREQ0 $SB_MREQ0 CONFIG.MREQ1 $SB_MREQ1 CONFIG.MREQ2 $SB_MREQ2 \
    CONFIG.MRSP0 $SB_MRSP0 CONFIG.MRSP1 $SB_MRSP1 CONFIG.MRSP2 $SB_MRSP2 \
    CONFIG.MMAXB0 $SB_MMAXB0 CONFIG.MMAXB1 $SB_MMAXB1 CONFIG.MMAXB2 $SB_MMAXB2 \
    CONFIG.TIMEOUT $TIMEOUT CONFIG.LINK_CDC $LINK_CDC \
    CONFIG.LINK_FULL $LINK_FULL CONFIG.LINK_KTS $LINK_KTS \
    CONFIG.MGR0_DOM $MGR0_DOM CONFIG.CRED $CRED CONFIG.PIPE $PIPE] $bus

foreach {mid mod} $MESHES {
    connect_bd_net [get_bd_pins [v8_bus_clk $mid]] [get_bd_pins station_bus/bus_clk$mid]
    connect_bd_net [get_bd_pins bus_rst_inv$mid/Res] \
                   [get_bd_pins station_bus/bus_rst$mid]
    # S_AXI_MEM and S_AXI_CTRL are inside the node, on the sysnode clock.
    connect_bd_net [get_bd_pins [v8_sys_clk $mid]] [get_bd_pins station_bus/clk_s$mid]
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
v8_jtag jtag_ctrl [v8_bus_clk 1] [v8_bus_rstn 1]
# A manager's AXI into its station port, through an axi_register_slice on the
# manager's own clock and reset at SB_MGR_RS 1: every channel fully registered
# (REG_* 1), widths propagated from both ends.
proc v8_mgr_rs {name src dst clkpin rstpin} {
    global SB_MGR_RS
    if {!$SB_MGR_RS} {
        connect_bd_intf_net [get_bd_intf_pins $src] [get_bd_intf_pins $dst]
        return
    }
    set rs [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_register_slice $name]
    set_property -dict [list CONFIG.REG_AW {1} CONFIG.REG_AR {1} CONFIG.REG_W {1} \
                             CONFIG.REG_R {1} CONFIG.REG_B {1}] $rs
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/aclk]
    connect_bd_net [get_bd_pins $rstpin] [get_bd_pins $name/aresetn]
    connect_bd_intf_net [get_bd_intf_pins $src] [get_bd_intf_pins $name/S_AXI]
    connect_bd_intf_net [get_bd_intf_pins $name/M_AXI] [get_bd_intf_pins $dst]
}
v8_mgr_rs rs_jtag jtag_ctrl/M_AXI station_bus/S00_AXI [v8_bus_clk 1] [v8_bus_rstn 1]

set xdma [create_bd_cell -type ip -vlnv xilinx.com:ip:xdma xdma_0]
set n_irq [expr {$XDMA_IRQ ? [llength $MESHES] / 2 : 1}]
set xcfg [list \
  CONFIG.axi_data_width ${XDMA_AXI_W}_bit CONFIG.axi_id_width {4} \
  CONFIG.axilite_master_en {true} CONFIG.axilite_master_scale {Megabytes} \
  CONFIG.axilite_master_size {16} CONFIG.axisten_freq {250} \
  CONFIG.functional_mode {DMA} CONFIG.mode_selection {Basic} \
  CONFIG.pcie_blk_locn {X0Y1} CONFIG.pf0_device_id {903F} \
  CONFIG.pf0_subsystem_id {0007} CONFIG.pf0_subsystem_vendor_id {10EE} \
  CONFIG.pl_link_cap_max_link_speed {8.0_GT/s} \
  CONFIG.pl_link_cap_max_link_width $XDMA_LANES CONFIG.ref_clk_freq {100_MHz} \
  CONFIG.xdma_num_usr_irq $n_irq CONFIG.xdma_rnum_chnl $XDMA_RCHNL \
  CONFIG.xdma_wnum_chnl $XDMA_WCHNL]
if {[llength $XDMA_CLASS]} {
    lassign $XDMA_CLASS cb cs ci
    lappend xcfg CONFIG.pf0_class_code_base $cb CONFIG.pf0_class_code_sub $cs \
                 CONFIG.pf0_class_code_interface $ci
}
set_property -dict $xcfg $xdma
# XDMA's OWN clock and reset: its masters are synchronous to the axi_aclk the
# PCIe core derives from the link -- an MMCM output here is a false domain.
connect_bd_net [get_bd_pins xdma_0/axi_aclk]    [get_bd_pins station_bus/clk_xdma]
connect_bd_net [get_bd_pins xdma_0/axi_aresetn] [get_bd_pins station_bus/aresetn_xdma]
if {$XDMA_AXI_W == 512} {
    v8_mgr_rs rs_xdma xdma_0/M_AXI station_bus/S01_AXI \
              xdma_0/axi_aclk xdma_0/axi_aresetn
} else {
    # Station 1's XDMA manager is 512-bit (sb_line4): upsize on axi_aclk, no FIFO.
    set dx [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dwidth_converter dwc_xdma]
    set_property -dict [list CONFIG.SI_DATA_WIDTH $XDMA_AXI_W CONFIG.MI_DATA_WIDTH {512} \
                             CONFIG.ACLK_ASYNC {0} CONFIG.FIFO_MODE {0}] $dx
    connect_bd_net [get_bd_pins xdma_0/axi_aclk]    [get_bd_pins dwc_xdma/s_axi_aclk]
    connect_bd_net [get_bd_pins xdma_0/axi_aresetn] [get_bd_pins dwc_xdma/s_axi_aresetn]
    connect_bd_intf_net [get_bd_intf_pins xdma_0/M_AXI] [get_bd_intf_pins dwc_xdma/S_AXI]
    v8_mgr_rs rs_xdma dwc_xdma/M_AXI station_bus/S01_AXI \
              xdma_0/axi_aclk xdma_0/axi_aresetn
}
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
if {$XDMA_IRQ} {
    # Die i's host_irq toggle -> usr_irq_req[i], each through kohaku_irq_req on
    # axi_aclk (its two flops are the crossing; the toggle holds between flips).
    set ic [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat irq_cat]
    set_property CONFIG.NUM_PORTS $n_irq $ic
    foreach {mid mod} $MESHES {
        create_bd_cell -type module -reference kohaku_irq_req irq_req$mid
        set sl [create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice irq_ack$mid]
        set_property -dict [list CONFIG.DIN_WIDTH $n_irq CONFIG.DIN_FROM $mid \
                                 CONFIG.DIN_TO $mid] $sl
        connect_bd_net [get_bd_pins xdma_0/axi_aclk]    [get_bd_pins irq_req$mid/clk]
        connect_bd_net [get_bd_pins xdma_0/axi_aresetn] [get_bd_pins irq_req$mid/resetn]
        connect_bd_net [get_bd_pins mesh_$mid/host_irq] [get_bd_pins irq_req$mid/evt]
        connect_bd_net [get_bd_pins xdma_0/usr_irq_ack] [get_bd_pins irq_ack$mid/Din]
        connect_bd_net [get_bd_pins irq_ack$mid/Dout]   [get_bd_pins irq_req$mid/ack]
        connect_bd_net [get_bd_pins irq_req$mid/req]    [get_bd_pins irq_cat/In$mid]
    }
    connect_bd_net [get_bd_pins irq_cat/dout] [get_bd_pins xdma_0/usr_irq_req]
} else {
    connect_bd_net [get_bd_pins zero_1/dout] [get_bd_pins xdma_0/usr_irq_req]
}

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
    connect_bd_net [get_bd_pins [v8_sys_clk $mid]] [get_bd_pins dwc_ctrl$mid/s_axi_aclk]
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
puts "@@@ bus: $SB_WRAP, 3 managers, [expr {4 * $NQ}] endpoints,\
 [expr {$PER_DIE_CLK ? {a bus clock per station} : {one system clock}}], LINK_CDC $LINK_CDC"

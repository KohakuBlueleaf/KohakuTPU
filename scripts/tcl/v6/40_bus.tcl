# The station-bus line, its managers, and the per-station endpoints. Ports are
# fixed by sb_line4 port_dom(): 0,1 mesh clock; 2 clk_ddr; 3 clk_ctrl.

set bus [create_bd_cell -type module -reference sb_bd_line4 station_bus]
set_property -dict [list CONFIG.FW $FW CONFIG.OST $OST \
    CONFIG.STORE_FWD $STORE_FWD CONFIG.LUT_PER_BRAM $LUT_PER_BRAM \
    CONFIG.TIMEOUT $TIMEOUT CONFIG.LINK_CDC $LINK_CDC \
    CONFIG.LINK_FULL $LINK_FULL CONFIG.CRED $CRED CONFIG.PIPE $PIPE] $bus

foreach {mid mod} $MESHES {
    connect_bd_net [get_bd_pins clk_wiz_bus$mid/clk_out1] \
                   [get_bd_pins station_bus/bus_clk$mid]
    connect_bd_net [get_bd_pins bus_rst_inv$mid/Res] \
                   [get_bd_pins station_bus/bus_rst$mid]
    # clk_out4, the MAG clock: S_AXI_MEM and S_AXI_CTRL are inside mag_1m, which
    # runs on mag_clk_i, NOT on the fabric clock the port used to take.
    connect_bd_net [get_bd_pins clk_wiz_mesh$mid/clk_out4] \
                   [get_bd_pins station_bus/clk_s$mid]
    connect_bd_net [get_bd_pins rst_mesh$mid/peripheral_aresetn] \
                   [get_bd_pins station_bus/aresetn_s$mid]
}
connect_bd_net [get_bd_pins clk_wiz_ctrl/clk_out1] [get_bd_pins station_bus/clk_ctrl]
connect_bd_net [get_bd_pins rst_ctrl/peripheral_aresetn] \
               [get_bd_pins station_bus/aresetn_ctrl]
# Port 2 of each station runs on THAT SLR's MIG ui_clk, so the station meets
# the controller in its own domain and no crossing IP sits between them.
foreach {mid mod} $MESHES {
    if {[v6_has_mesh $mid]} {
        set ddr [dict get $DDR_OF_SLR $mid]
        connect_bd_net [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk] \
                       [get_bd_pins station_bus/clk_ddr$mid]
        connect_bd_net [get_bd_pins rst_ddr4_$ddr/peripheral_aresetn] \
                       [get_bd_pins station_bus/aresetn_ddr$mid]
    } else {
        # No MIG on a probe-absent die; the MAG-rate clock stands in.
        connect_bd_net [get_bd_pins clk_wiz_mesh$mid/clk_out4] \
                       [get_bd_pins station_bus/clk_ddr$mid]
        connect_bd_net [get_bd_pins rst_mesh$mid/peripheral_aresetn] \
                       [get_bd_pins station_bus/aresetn_ddr$mid]
    }
}

# ---- managers ------------------------------------------------------------
# PROTOCOL 0 is AXI4; the default 2 is Lite and will not connect. 64-bit
# addressing: the station field is at AW-4 = 39, so 32 bits reach station 0 only.
proc v6_jtag {name clkpin rstpin} {
    set j [create_bd_cell -type ip -vlnv xilinx.com:ip:jtag_axi $name]
    # DATA_WIDTH stated, not defaulted: the IP defaults to 32 and the driver
    # speaks 64-bit words (v6.5 shipped 32-bit by omission).
    set_property -dict [list CONFIG.PROTOCOL {0} CONFIG.M_AXI_ID_WIDTH {4} \
                             CONFIG.M_AXI_ADDR_WIDTH {64} \
                             CONFIG.M_AXI_DATA_WIDTH {64}] $j
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/aclk]
    connect_bd_net [get_bd_pins $rstpin] [get_bd_pins $name/aresetn]
}
v6_jtag jtag_ctrl clk_wiz_ctrl/clk_out1 rst_ctrl/peripheral_aresetn
# Straight in at its own width: the station converts width itself.
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
# XDMA's OWN clock and reset. Both masters are synchronous to axi_aclk, which
# the PCIe core derives from the link -- an MMCM output here is a false domain.
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
set xlc [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant xlconstant_irq]
set_property -dict [list CONFIG.CONST_VAL {0} CONFIG.CONST_WIDTH {1}] $xlc
connect_bd_net [get_bd_pins xlconstant_irq/dout] [get_bd_pins xdma_0/usr_irq_req]

# ---- endpoints -----------------------------------------------------------
# Probe-absent die: the port still terminates on real logic in that SLR, or
# the bus has nothing to place there.
proc v6_bram_ep {name dw clkpin rstpin} {
    set c [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_bram_ctrl $name]
    set_property -dict [list CONFIG.DATA_WIDTH $dw CONFIG.SINGLE_PORT_BRAM {1} \
                             CONFIG.ECC_TYPE {0}] $c
    set m [create_bd_cell -type ip -vlnv xilinx.com:ip:blk_mem_gen ${name}_mem]
    set_property -dict [list CONFIG.Memory_Type {Single_Port_RAM} \
                             CONFIG.use_bram_block {BRAM_Controller}] $m
    connect_bd_intf_net [get_bd_intf_pins $name/BRAM_PORTA] \
                        [get_bd_intf_pins ${name}_mem/BRAM_PORTA]
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/s_axi_aclk]
    connect_bd_net [get_bd_pins $rstpin] [get_bd_pins $name/s_axi_aresetn]
}
foreach {mid mod} $MESHES {
    set p0 [format M%02d_AXI [expr {$mid * $NQ + 0}]]
    set p1 [format M%02d_AXI [expr {$mid * $NQ + 1}]]
    set p2 [format M%02d_AXI [expr {$mid * $NQ + 2}]]
    set p3 [format M%02d_AXI [expr {$mid * $NQ + 3}]]

    if {![v6_has_mesh $mid]} {
        foreach {port dw} [list $p0 $FW $p1 32 $p2 32] {
            v6_bram_ep ep_s${mid}_[string range $port 0 2] $dw \
                clk_wiz_mesh$mid/clk_out4 rst_mesh$mid/peripheral_aresetn
            connect_bd_intf_net [get_bd_intf_pins station_bus/$port] \
                [get_bd_intf_pins ep_s${mid}_[string range $port 0 2]/S_AXI]
        }
        connect_bd_intf_net [get_bd_intf_pins station_bus/$p3] \
                            [get_bd_intf_pins clk_wiz_mesh$mid/s_axi_lite]
        connect_bd_net [get_bd_pins clk_wiz_ctrl/clk_out1] \
                       [get_bd_pins clk_wiz_mesh$mid/s_axi_aclk]
        connect_bd_net [get_bd_pins rst_ctrl/peripheral_aresetn] \
                       [get_bd_pins clk_wiz_mesh$mid/s_axi_aresetn]
        continue
    }
    set ddr [dict get $DDR_OF_SLR $mid]

    connect_bd_intf_net [get_bd_intf_pins station_bus/$p0] \
                        [get_bd_intf_pins mesh_$mid/S_AXI_MEM]

    # S_AXI_CTRL is 64-bit and gen_station_wrap emits only 32 or FW, so the
    # station cannot meet it natively.
    set dc [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dwidth_converter dwc_ctrl$mid]
    set_property -dict [list CONFIG.SI_DATA_WIDTH {32} CONFIG.MI_DATA_WIDTH {64}] $dc
    connect_bd_net [get_bd_pins clk_wiz_mesh$mid/clk_out4] \
                   [get_bd_pins dwc_ctrl$mid/s_axi_aclk]
    connect_bd_net [get_bd_pins rst_mesh$mid/peripheral_aresetn] \
                   [get_bd_pins dwc_ctrl$mid/s_axi_aresetn]
    connect_bd_intf_net [get_bd_intf_pins station_bus/$p1] \
                        [get_bd_intf_pins dwc_ctrl$mid/S_AXI]
    connect_bd_intf_net [get_bd_intf_pins dwc_ctrl$mid/M_AXI] \
                        [get_bd_intf_pins mesh_$mid/S_AXI_CTRL]

    # Direct: the station's port-2 domain IS this controller's ui_clk.
    connect_bd_intf_net [get_bd_intf_pins station_bus/$p2] \
                        [get_bd_intf_pins ddr4_$ddr/C0_DDR4_S_AXI_CTRL]

    # The mesh's own clock controller, reached on the FIXED ctrl clock so a
    # retune never stands on the clock it is changing.
    connect_bd_intf_net [get_bd_intf_pins station_bus/$p3] \
                        [get_bd_intf_pins clk_wiz_mesh$mid/s_axi_lite]
    connect_bd_net [get_bd_pins clk_wiz_ctrl/clk_out1] \
                   [get_bd_pins clk_wiz_mesh$mid/s_axi_aclk]
    connect_bd_net [get_bd_pins rst_ctrl/peripheral_aresetn] \
                   [get_bd_pins clk_wiz_mesh$mid/s_axi_aresetn]
}
puts "@@@ v6 bus: 3 managers, [expr {4 * $NQ}] endpoints"

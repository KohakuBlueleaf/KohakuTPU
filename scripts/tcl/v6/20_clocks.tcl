# Clock generators. One reconfigurable MMCM per mesh and one per station bus,
# so every compute domain and every SLR fabric retunes independently at runtime.

# ONE differential reference into an explicit BUFG. Vivado's auto-inserted
# buffer has no name until opt_design, so no XDC can reach it.
set sysp [create_bd_intf_port -mode Slave \
              -vlnv xilinx.com:interface:diff_clock_rtl:1.0 system]
set_property CONFIG.FREQ_HZ {100000000} $sysp
set udbs [create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_sys]
set_property CONFIG.C_BUF_TYPE {IBUFDS} $udbs
set udbg [create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_bufg]
set_property CONFIG.C_BUF_TYPE {BUFG} $udbg
connect_bd_intf_net $sysp [get_bd_intf_pins util_ds_buf_sys/CLK_IN_D]
connect_bd_net [get_bd_pins util_ds_buf_sys/IBUF_OUT] \
               [get_bd_pins util_ds_buf_bufg/BUFG_I]

# `outs` is {clkout_index requested_mhz mmcm_divide}. The IP's DIVIDE property
# names run one behind its port names: CLKOUT0_DIVIDE_F feeds clk_out1.
proc v6_wiz {name outs drp} {
    set w [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz $name]
    set cfg [list CONFIG.PRIM_SOURCE {No_buffer} CONFIG.PRIM_IN_FREQ {100.000} \
                  CONFIG.PRIMITIVE {MMCM} CONFIG.USE_RESET {false} \
                  CONFIG.FEEDBACK_SOURCE {FDBK_AUTO}]
    foreach o $outs {
        lassign $o idx mhz div
        lappend cfg CONFIG.CLKOUT${idx}_REQUESTED_OUT_FREQ [format %.3f $mhz] \
                    CONFIG.CLKOUT${idx}_DRIVES {Buffer}
        if {$idx > 1} { lappend cfg CONFIG.CLKOUT${idx}_USED {true} }
    }
    if {$drp} {
        lappend cfg CONFIG.USE_DYN_RECONFIG {true} \
                    CONFIG.INTERFACE_SELECTION {Enable_AXI}
    }
    set_property -dict $cfg $w

    # OVERRIDE FIRST: otherwise the IP re-solves from the requested frequency,
    # DIVCLK_DIVIDE returns 1, and the DRP step is 25 MHz instead of 6.25.
    global VCO_D VCO_M
    set_property CONFIG.OVERRIDE_MMCM {true} $w
    set div [list CONFIG.MMCM_DIVCLK_DIVIDE $VCO_D \
                  CONFIG.MMCM_CLKFBOUT_MULT_F [format %.3f $VCO_M]]
    foreach o $outs {
        lassign $o idx mhz d
        if {$idx == 1} {
            lappend div CONFIG.MMCM_CLKOUT0_DIVIDE_F [format %.3f $d]
        } else {
            lappend div CONFIG.MMCM_CLKOUT[expr {$idx - 1}]_DIVIDE $d
        }
    }
    set_property -dict $div $w
    connect_bd_net [get_bd_pins $name/clk_in1] \
                   [get_bd_pins util_ds_buf_bufg/BUFG_O]
    puts "@@@ $name D=[get_property CONFIG.MMCM_DIVCLK_DIVIDE $w]\
 M=[get_property CONFIG.MMCM_CLKFBOUT_MULT_F $w] drp=$drp"
    return $w
}

# ext_reset_in is ACTIVE LOW, so `locked` drives it directly -- station_bus_bd
# wired it this way and ran on hardware.
proc v6_psr {name clkpin lockpin} {
    create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset $name
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/slowest_sync_clk]
    connect_bd_net [get_bd_pins $name/ext_reset_in] \
                   [get_bd_pins clk_wiz_ctrl/locked]
    # Its OWN generator's lock, or the domain releases before its clock exists.
    connect_bd_net [get_bd_pins $lockpin] [get_bd_pins $name/dcm_locked]
}

# NO DRP: reaching a clock controller must not depend on the clock it changes.
# 1200/250 = 4.8 and only CLKOUT0 divides fractionally, so no XDMA output here.
v6_wiz clk_wiz_ctrl [list [list 1 $CTRL_MHZ 12]] 0

# Per mesh: NoC/fabric, matmul 2x, vector, MAG -- four independent outputs off
# one VCO, so a retune moves them together in ratio and each is set separately.
foreach {mid mod} $MESHES {
    v6_wiz clk_wiz_mesh$mid [list \
        [list 1 $MESH_MHZ  $DIV_MESH] \
        [list 2 $MAT2X_MHZ $DIV_MAT2X] \
        [list 3 $VEC_MHZ   $DIV_VEC] \
        [list 4 $MESH_MHZ  $DIV_MESH]] 1
    v6_psr rst_mesh$mid clk_wiz_mesh$mid/clk_out1 clk_wiz_mesh$mid/locked
}

# One fabric clock per station; sb_link_cdc crosses between them. NOT DRP: all
# NQ=4 ports are taken and a 5th measured +1,610 LUT, bus_clk 346 -> 328 MHz.
foreach {mid mod} $MESHES {
    v6_wiz clk_wiz_bus$mid [list [list 1 $BUS_MHZ $DIV_BUS]] 0
    v6_psr rst_bus$mid clk_wiz_bus$mid/clk_out1 clk_wiz_bus$mid/locked
}

v6_psr rst_ctrl clk_wiz_ctrl/clk_out1 clk_wiz_ctrl/locked

# sb_line4 takes an ACTIVE HIGH bus reset; proc_sys_reset gives active low.
foreach {mid mod} $MESHES {
    set inv [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic \
                 bus_rst_inv$mid]
    set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] $inv
    connect_bd_net [get_bd_pins rst_bus$mid/peripheral_aresetn] \
                   [get_bd_pins bus_rst_inv$mid/Op1]
}

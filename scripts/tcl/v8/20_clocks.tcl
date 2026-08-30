# Clock generators and the one reset each of them owns.
#
# ONE CLOCK = ONE RESET. Every proc_sys_reset here is clocked by the clock it
# resets and released on that generator's own lock; a reset never reaches a
# register on another clock (70_analyze audits the netlist for that).

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
proc v8_wiz {name outs drp} {
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
 M=[get_property CONFIG.MMCM_CLKFBOUT_MULT_F $w] drp=$drp outs=[llength $outs]"
    return $w
}

# ext_reset_in is ACTIVE LOW, so the control lock drives it directly; dcm_locked
# is the reset's OWN generator, or the domain releases before its clock exists.
proc v8_psr {name clkpin lockpin} {
    create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset $name
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/slowest_sync_clk]
    connect_bd_net [get_bd_pins $name/ext_reset_in] \
                   [get_bd_pins clk_wiz_ctrl/locked]
    connect_bd_net [get_bd_pins $lockpin] [get_bd_pins $name/dcm_locked]
}

# Control: fixed, no DRP. 1200/250 = 4.8 and only CLKOUT0 divides
# fractionally, so no XDMA output here either.
v8_wiz clk_wiz_ctrl [list [list 1 $CTRL_MHZ 12]] 0
v8_psr rst_ctrl clk_wiz_ctrl/clk_out1 clk_wiz_ctrl/locked

# Per die: NoC, matmul 2x, vector -- and on SYS_WIZ only, the sysnode clock
# as a fourth output. The other dies carry no MAG-rate output at all. The
# NoC, mat and vec domains take their resets INSIDE the top (kh_rst_sync
# from axi_aresetn); a proc_sys_reset here would have no load (measured: 0).
foreach {mid mod} $MESHES {
    set outs [list [list 1 $MESH_MHZ  $DIV_MESH] \
                   [list 2 $MAT2X_MHZ $DIV_MAT2X] \
                   [list 3 $VEC_MHZ   $DIV_VEC]]
    if {$mid == $SYS_WIZ} { lappend outs [list 4 $SYS_MHZ $DIV_SYS] }
    v8_wiz clk_wiz_mesh$mid $outs 1
}
# THE sysnode clock ($SYS_CLK, 00_config) and its reset: every node's
# axi_aclk/axi_aresetn and dram_aclk, the Xache, every station's port-0/1
# domain, the interlink pipes, the converters.
v8_psr rst_sys $SYS_CLK $SYS_LOCK
# ...delivered to each die as its own registered copy (00_config v8_rstn).
create_bd_cell -type module -reference xcvu13p_rst_tree rst_tree
connect_bd_net [get_bd_pins $SYS_CLK] [get_bd_pins rst_tree/clk]
connect_bd_net [get_bd_pins rst_sys/peripheral_aresetn] [get_bd_pins rst_tree/rstn_in]

# One fabric clock per station; sb_link_cdc crosses between them. NOT DRP.
foreach {mid mod} $MESHES {
    v8_wiz clk_wiz_bus$mid [list [list 1 $BUS_MHZ $DIV_BUS]] 0
    v8_psr rst_bus$mid clk_wiz_bus$mid/clk_out1 clk_wiz_bus$mid/locked
}

# sb_line4 takes an ACTIVE HIGH bus reset; proc_sys_reset gives active low.
foreach {mid mod} $MESHES {
    set inv [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic \
                 bus_rst_inv$mid]
    set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] $inv
    connect_bd_net [get_bd_pins rst_bus$mid/peripheral_aresetn] \
                   [get_bd_pins bus_rst_inv$mid/Op1]
}
puts "@@@ v8 clocks: ctrl, sys (mesh$SYS_WIZ/clk_out4), 4 mesh wizards, 4 bus wizards; resets ctrl, sys, 4 bus (MIG resets in 30_meshes)"

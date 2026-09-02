# Clock generators and the one reset each of them owns.
#
# ONE CLOCK = ONE RESET. Every proc_sys_reset here is clocked by the clock it
# resets and released on that generator's own lock; a reset never reaches a
# register on another clock (70_analyze audits the netlist for that). The two
# clocks that span four dies -- the sysnode clock and the system clock -- each
# deliver their reset through an xcvu13p_rst_tree, one registered copy per die.

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

# ext_reset_in is ACTIVE LOW, so a lock drives it directly; dcm_locked is the
# reset's OWN generator, or the domain releases before its clock exists.
set ::V8_EXT_RST clk_wiz_ctrl/locked
proc v8_psr {name clkpin lockpin} {
    create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset $name
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/slowest_sync_clk]
    connect_bd_net [get_bd_pins $name/ext_reset_in] [get_bd_pins $::V8_EXT_RST]
    connect_bd_net [get_bd_pins $lockpin] [get_bd_pins $name/dcm_locked]
}

# One reset delivered to four dies: q in the source die, land + fan in die i.
proc v8_tree {name clkpin rstnpin} {
    create_bd_cell -type module -reference xcvu13p_rst_tree $name
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/clk]
    connect_bd_net [get_bd_pins $rstnpin] [get_bd_pins $name/rstn_in]
}

# Control: fixed, no DRP. out1 100 MHz control plane, then the 200 MHz system
# clock -- out2 alone, or one output per die at PER_DIE_CLK 1 so each station
# takes a BUFG that can root in its own die.
set couts [list [list 1 $CTRL_MHZ $DIV_CTRL]]
if {$PER_DIE_CLK} {
    foreach {mid mod} $MESHES { lappend couts [list [expr {$mid + 2}] $BUS_MHZ $DIV_BUS] }
} else {
    lappend couts [list 2 $BUS_MHZ $DIV_BUS]
}
v8_wiz clk_wiz_ctrl $couts 0

# Per die: NoC, matmul 2x, vector -- and the sysnode clock as a fourth output,
# on SYS_WIZ alone or on every wizard at PER_DIE_CLK 1. The NoC, mat and vec
# domains take their resets INSIDE the top (kh_rst_sync from axi_aresetn); a
# proc_sys_reset here would have no load (v8: measured 0).
foreach {mid mod} $MESHES {
    set outs [list [list 1 $MESH_MHZ  $DIV_MESH] \
                   [list 2 $MAT2X_MHZ $DIV_MAT2X] \
                   [list 3 $VEC_MHZ   $DIV_VEC]]
    if {$PER_DIE_CLK || $mid == $SYS_WIZ} { lappend outs [list 4 $SYS_MHZ $DIV_SYS] }
    v8_wiz clk_wiz_mesh$mid $outs 1
}

# Independent clocks release independently, and the interlink surface has no
# ready: a die that starts sending into a die still in reset loses those flits.
# So no die leaves reset until EVERY wizard has locked.
if {$PER_DIE_CLK} {
    set cat [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat lock_cat]
    set_property CONFIG.NUM_PORTS [expr {1 + [llength $MESHES] / 2}] $cat
    set red [create_bd_cell -type ip -vlnv xilinx.com:ip:util_reduced_logic lock_all]
    set_property -dict [list CONFIG.C_OPERATION {and} \
        CONFIG.C_SIZE [expr {1 + [llength $MESHES] / 2}]] $red
    connect_bd_net [get_bd_pins clk_wiz_ctrl/locked] [get_bd_pins lock_cat/In0]
    foreach {mid mod} $MESHES {
        connect_bd_net [get_bd_pins clk_wiz_mesh$mid/locked] \
                       [get_bd_pins lock_cat/In[expr {$mid + 1}]]
    }
    connect_bd_net [get_bd_pins lock_cat/dout] [get_bd_pins lock_all/Op1]
    set ::V8_EXT_RST lock_all/Res
}

v8_psr rst_ctrl $CTRL_CLK clk_wiz_ctrl/locked

# THE sysnode clock and its reset: every mesh's axi_aclk/axi_aresetn and
# dram_aclk, the Xache, every station's port-0/1 domain, the interlink pipes,
# the converters. One clock and one tree, or one of each per die.
if {$PER_DIE_CLK} {
    foreach {mid mod} $MESHES {
        v8_psr rst_bus$mid [v8_bus_clk $mid] clk_wiz_ctrl/locked
        v8_psr rst_sys$mid [v8_sys_clk $mid] [v8_sys_lock $mid]
    }
} else {
    v8_psr rst_bus  [v8_bus_clk 0] clk_wiz_ctrl/locked
    v8_tree rst_tree_bus [v8_bus_clk 0] rst_bus/peripheral_aresetn
    v8_psr rst_sys [v8_sys_clk 0] [v8_sys_lock 0]
    v8_tree rst_tree [v8_sys_clk 0] rst_sys/peripheral_aresetn
}

# sb_line4 takes an ACTIVE HIGH bus reset per station; die i's copy, inverted.
foreach {mid mod} $MESHES {
    set inv [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic \
                 bus_rst_inv$mid]
    set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] $inv
    connect_bd_net [get_bd_pins [v8_bus_rstn $mid]] [get_bd_pins bus_rst_inv$mid/Op1]
}
if {$PER_DIE_CLK} {
    puts "@@@ clocks: PER-DIE -- ctrl 100 + four 200 MHz bus outputs, a sysnode\
 clk_out4 on every mesh wizard; resets ctrl + rst_bus0-3 + rst_sys0-3, all held\
 until every wizard locks"
} else {
    puts "@@@ clocks: ctrl (100 + 200), sys (mesh$SYS_WIZ/clk_out4), 4 mesh wizards;\
 resets ctrl, bus (+tree), sys (+tree)"
}
puts "@@@ MIG resets are in 30_meshes"

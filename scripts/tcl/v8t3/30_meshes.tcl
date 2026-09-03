# The four MIGs (named by SLR), the four meshes with their nodes, and the
# interlink chain 0-1-2-3 on the sysnode clock. M_AXI_DRAM is NOT connected
# here: 35_xache.tcl puts the Xache between every node and every channel.

# ---- MIGs: ddr4_$s is the controller whose pins are in SLR s ---------------
# The board ports keep the board's names (the pin XDCs are written against
# them); DDR_PORT_OF_SLR is the one line that joins the two numberings.
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

foreach {s mod} $MESHES {
    set port [dict get $DDR_PORT_OF_SLR $s]
    set d [create_bd_cell -type ip -vlnv xilinx.com:ip:ddr4 ddr4_$s]
    # The AXI ID carries the Xache master index on top of the node's 4 bits.
    set_property -dict [list \
      CONFIG.C0.DDR4_DataWidth {72} CONFIG.C0.DDR4_InputClockPeriod {2499} \
      CONFIG.C0.DDR4_MemoryPart {MT40A512M16HA-083E} \
      CONFIG.C0.DDR4_TimePeriod {833} CONFIG.C0.DDR4_isCustom {true} \
      CONFIG.C0.DDR4_AxiIDWidth $KX_DRAM_IDW] $d
    # One reset per MIG clock: rst_ddr4_s on ui_clk, released by the MIG.
    create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_ddr4_$s
    connect_bd_intf_net [get_bd_intf_ports c${port}_ddr4] [get_bd_intf_pins ddr4_$s/C0_DDR4]
    connect_bd_intf_net [get_bd_intf_ports c${port}_sys]  [get_bd_intf_pins ddr4_$s/C0_SYS_CLK]
    connect_bd_net [get_bd_pins ddr_sys_rst/Res] [get_bd_pins ddr4_$s/sys_rst]
    connect_bd_net [get_bd_pins ddr4_$s/c0_ddr4_ui_clk] \
                   [get_bd_pins rst_ddr4_$s/slowest_sync_clk]
    connect_bd_net [get_bd_pins ddr4_$s/c0_ddr4_ui_clk_sync_rst] \
                   [get_bd_pins rst_ddr4_$s/ext_reset_in]
    connect_bd_net [get_bd_pins rst_ddr4_$s/peripheral_aresetn] \
                   [get_bd_pins ddr4_$s/c0_ddr4_aresetn]
}

# ---- constants: the host stub window and the chain's two open ends --------
proc v8_zero {name width} {
    set c [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant $name]
    set_property -dict [list CONFIG.CONST_VAL {0} CONFIG.CONST_WIDTH $width] $c
    return $name/dout
}
set z1   [v8_zero zero_1 1]
set z4   [v8_zero zero_4 $IL_CN_W]
set z8   [v8_zero zero_8 8]
set z32  [v8_zero zero_32 32]
set z64  [v8_zero zero_64 64]
set zfl  [v8_zero zero_flit $IL_W]

# ---- meshes ----------------------------------------------------------------
if {$DRAM_CDC} { error "the node's DRAM master shares its die's clock with the Xache partition; DRAM_CDC must be 0" }
foreach {mid mod} $MESHES {
    create_bd_cell -type module -reference $mod mesh_$mid
    # The cell keeps the name every later stage addresses; only the module
    # behind it changed, from a 2x2 mesh to the bare system node.
    set_property -dict [list CONFIG.MESH_ID $mid CONFIG.ILINK $NODE_ILINK \
                             CONFIG.PORTS $NODE_PORTS \
                             CONFIG.L2_MAG_BANKS $L2_MAG_BANKS \
                             CONFIG.L2_MAG_ENTRIES $L2_MAG_ENTRIES \
                             CONFIG.DRAM_CDC $DRAM_CDC \
                             CONFIG.DRAM_AR_MAX $KX_RB_BEATS] [get_bd_cells mesh_$mid]

    # axi_aclk IS the AXI ports' clock and they live in the node: the ONE
    # sysnode clock, with die mid's copy of the ONE sysnode reset. The DRAM
    # master is on it too (DRAM_CDC 0): it meets the Xache with no crossing.
    connect_bd_net [get_bd_pins [v8_sys_clk $mid]] [get_bd_pins mesh_$mid/axi_aclk]
    connect_bd_net [get_bd_pins [v8_rstn $mid]] [get_bd_pins mesh_$mid/axi_aresetn]
    connect_bd_net [get_bd_pins [v8_sys_clk $mid]] [get_bd_pins mesh_$mid/dram_aclk]
    connect_bd_net [get_bd_pins [v8_rstn $mid]] [get_bd_pins mesh_$mid/dram_aresetn]
    # No mesh: no noc, vector or matmul clock, no pump divider, and no host
    # stub -- the host reaches the node through S_AXI_CTRL and S_AXI_MEM.
}

# ---- the interlink chain -------------------------------------------------
# Mesh i's LINK1 faces mesh i+1's LINK0 (mag_switch.v CH_SEQ = the index
# order). One kts_pipe_bd per hop and direction: flits forward, credits back,
# u_tx pinned with the leaving die and u_rx with the landing die
# (60_constraints), each half on its own die's reset copy.
proc v8_pipe {name sp dp tx rx} {
    global IL_W IL_VCW IL_CN_W IL_ASYNC IL_CRED IL_STAGES
    create_bd_cell -type module -reference kts_pipe_bd $name
    set_property -dict [list CONFIG.W $IL_W CONFIG.VCW $IL_VCW CONFIG.CN_W $IL_CN_W \
                             CONFIG.ASYNC $IL_ASYNC CONFIG.CRED $IL_CRED \
                             CONFIG.STAGES $IL_STAGES] \
        [get_bd_cells $name]
    connect_bd_net [get_bd_pins [v8_sys_clk $tx]] [get_bd_pins $name/clk]
    connect_bd_net [get_bd_pins [v8_sys_clk $rx]] [get_bd_pins $name/clk_rx]
    connect_bd_net [get_bd_pins [v8_rstn $tx]] [get_bd_pins $name/rstn_tx]
    connect_bd_net [get_bd_pins [v8_rstn $rx]] [get_bd_pins $name/rstn_rx]
    foreach f {valid vc last flit} {
        connect_bd_net [get_bd_pins ${sp}_$f] [get_bd_pins $name/i_$f]
        connect_bd_net [get_bd_pins $name/o_$f] [get_bd_pins ${dp}_$f]
    }
    foreach f {crd_valid crd_vc crd_n} {
        connect_bd_net [get_bd_pins ${dp}_$f] [get_bd_pins $name/i_$f]
        connect_bd_net [get_bd_pins $name/o_$f] [get_bd_pins ${sp}_$f]
    }
}
foreach hop {{0 1} {1 2} {2 3}} {
    lassign $hop lo hi
    v8_pipe pipe_${lo}_to_${hi} mesh_$lo/LINK1_OUT mesh_$hi/LINK0_IN $lo $hi
    v8_pipe pipe_${hi}_to_${lo} mesh_$hi/LINK0_OUT mesh_$lo/LINK1_IN $hi $lo
}
# The ends of the line have no neighbour: their inbound surface is tied off.
foreach {m side} {0 LINK0 3 LINK1} {
    foreach f {valid vc last} {
        connect_bd_net [get_bd_pins $z1] [get_bd_pins mesh_$m/${side}_IN_$f]
    }
    connect_bd_net [get_bd_pins $zfl] [get_bd_pins mesh_$m/${side}_IN_flit]
    foreach f {crd_valid crd_vc} {
        connect_bd_net [get_bd_pins $z1] [get_bd_pins mesh_$m/${side}_OUT_$f]
    }
    connect_bd_net [get_bd_pins $z4] [get_bd_pins mesh_$m/${side}_OUT_crd_n]
}
puts "@@@ v8t3 meshes: [llength [get_bd_cells -quiet mesh_*]], 4 MIGs by SLR, [llength [get_bd_cells -quiet pipe_*]] interlink pipes on the sysnode clock"

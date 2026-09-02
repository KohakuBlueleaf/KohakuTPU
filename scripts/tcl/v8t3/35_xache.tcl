# Kohaku Xache, partition-aware: ONE kx_pbd_4x4 (kx_pxache, P = 4) between the
# four nodes' DRAM masters and the four MIGs, on the sysnode clock, at the
# routed proof shape (K 2, 16384 sets, one bank). Its only clock crossings are
# its four DRAM edges (h_clk<h> = ddr4_h's ui_clk); partition h = die h takes
# die h's copy of the sysnode reset.

# Bit-pair swap lists as ONE 256-bit binary literal each (the wrapper's fixed
# width; IPI rejects a Verilog concatenation, IP_Flow 19-3450). Pair k is byte
# k, k = 0 the LSB; bytes above NSWAP are zero and unread.
proc v8_bytes {vals} {
    if {[llength $vals] > 32} { error "v8t3 xache: [llength $vals] swap pairs, the wrapper holds 32" }
    set s [string repeat 0 [expr {8 * (32 - [llength $vals])}]]
    foreach v [lreverse $vals] {
        for {set b 7} {$b >= 0} {incr b -1} { append s [expr {($v >> $b) & 1}] }
    }
    return "0b$s"
}

set kx [create_bd_cell -type module -reference kx_pbd_4x4 xache]
set_property -dict [list \
    CONFIG.SETS $KX_SETS CONFIG.SET_W $KX_SET_W CONFIG.K $KX_K \
    CONFIG.RAM_STYLE {ultra} CONFIG.BANKS $KX_BANKS CONFIG.CDC_DEPTH $KX_CDC_DEPTH \
    CONFIG.RD_OUTQ $KX_RD_OUTQ CONFIG.WR_OUTQ $KX_WR_OUTQ CONFIG.RB_BEATS $KX_RB_BEATS \
    CONFIG.HOP_DEPTH $KX_HOP_DEPTH CONFIG.HOP_RXREG $KX_HOP_RXREG \
    CONFIG.BND_TRUNK $KX_BND_TRUNK CONFIG.PCLK $KX_PCLK CONFIG.NSWAP $KX_NSWAP \
    CONFIG.MEM_TRUNK $KX_MEM_TRUNK CONFIG.MEM_RB $KX_MEM_RB \
    CONFIG.MEM_HRD $KX_MEM_HRD CONFIG.MEM_HWR $KX_MEM_HWR \
    CONFIG.SWAP_A [v8_bytes $KX_SWAP_A] CONFIG.SWAP_B [v8_bytes $KX_SWAP_B]] $kx

# p_clk<p> is partition p's clock and master p's -- so a node meets its partition
# with no crossing, and at PCLK 1 every boundary trunk carries one. There is no
# aclk: IPI gives a clock pin with no ASSOCIATED_BUSIF every interface in the
# module, which collides with each h_clk and fails validation (BD 41-1732).

# Master m = mesh m's node; home h = ddr4_h, the MIG in SLR h.
foreach {mid mod} $MESHES {
    connect_bd_net [get_bd_pins [v8_sys_clk $mid]] [get_bd_pins xache/p_clk$mid]
    connect_bd_net [get_bd_pins [v8_rstn $mid]] [get_bd_pins xache/d_rstn$mid]
    connect_bd_intf_net [get_bd_intf_pins mesh_$mid/M_AXI_DRAM] \
                        [get_bd_intf_pins xache/S0${mid}_AXI]
    connect_bd_intf_net [get_bd_intf_pins xache/M0${mid}_AXI] \
                        [get_bd_intf_pins ddr4_$mid/C0_DDR4_S_AXI]
    connect_bd_net [get_bd_pins ddr4_$mid/c0_ddr4_ui_clk] [get_bd_pins xache/h_clk$mid]
    connect_bd_net [get_bd_pins rst_ddr4_$mid/peripheral_aresetn] \
                   [get_bd_pins xache/h_rstn$mid]
}
puts "@@@ xache: kx_pxache 4x4, P 4, K $KX_K, SETS $KX_SETS, BANKS $KX_BANKS, NSWAP $KX_NSWAP,\
 HOP_DEPTH $KX_HOP_DEPTH, RD_OUTQ $KX_RD_OUTQ, WR_OUTQ $KX_WR_OUTQ, PCLK $KX_PCLK"

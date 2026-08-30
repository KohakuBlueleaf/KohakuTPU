# Kohaku Xache: ONE kx_bd_4x4 between the four nodes' DRAM masters and the
# four MIGs, on the sysnode clock. Its only clock crossings are its four DRAM
# edges (h_clk<h> = that MIG's ui_clk, h_rstn<h> = that MIG's reset).

# Bit-pair swap lists as ONE 256-bit binary literal each (the wrapper's fixed
# width; IPI rejects a Verilog concatenation, IP_Flow 19-3450). Pair k is byte
# k, k = 0 the LSB; bytes above NSWAP are zero and unread.
proc v8_bytes {vals} {
    if {[llength $vals] > 32} { error "v8 xache: [llength $vals] swap pairs, the wrapper holds 32" }
    set s [string repeat 0 [expr {8 * (32 - [llength $vals])}]]
    foreach v [lreverse $vals] {
        for {set b 7} {$b >= 0} {incr b -1} { append s [expr {($v >> $b) & 1}] }
    }
    return "0b$s"
}

set kx [create_bd_cell -type module -reference kx_bd_4x4 xache]
set_property -dict [list \
    CONFIG.SETS $KX_SETS CONFIG.SET_W $KX_SET_W CONFIG.K 1 \
    CONFIG.RAM_STYLE {ultra} CONFIG.RSAMD 1 CONFIG.WSAMD 1 \
    CONFIG.CDC_DEPTH $KX_CDC_DEPTH CONFIG.RD_PIPE 1 CONFIG.RD_OUTQ $KX_RD_OUTQ \
    CONFIG.SLRX $KX_SLRX CONFIG.NSWAP $KX_NSWAP \
    CONFIG.SWAP_A [v8_bytes $KX_SWAP_A] CONFIG.SWAP_B [v8_bytes $KX_SWAP_B]] $kx

connect_bd_net [get_bd_pins $SYS_CLK] [get_bd_pins xache/aclk]

# Master m = mesh m's node; home h = the MIG in SLR h; die m's reset copy
# resets both slices m.
foreach {mid mod} $MESHES {
    connect_bd_net [get_bd_pins [v8_rstn $mid]] [get_bd_pins xache/d_rstn$mid]
    connect_bd_intf_net [get_bd_intf_pins mesh_$mid/M_AXI_DRAM] \
                        [get_bd_intf_pins xache/S0${mid}_AXI]
    set ddr [dict get $DDR_OF_SLR $mid]
    connect_bd_intf_net [get_bd_intf_pins xache/M0${mid}_AXI] \
                        [get_bd_intf_pins ddr4_$ddr/C0_DDR4_S_AXI]
    connect_bd_net [get_bd_pins ddr4_$ddr/c0_ddr4_ui_clk] [get_bd_pins xache/h_clk$mid]
    connect_bd_net [get_bd_pins rst_ddr4_$ddr/peripheral_aresetn] \
                   [get_bd_pins xache/h_rstn$mid]
}
puts "@@@ v8 xache: 4x4, [expr {64 * 4}] URAM, NSWAP $KX_NSWAP ([llength $KX_SWAP_A] pairs),\
 SLRX $KX_SLRX, RD_OUTQ $KX_RD_OUTQ"

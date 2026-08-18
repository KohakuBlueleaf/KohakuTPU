# The map lives in sb_line4's SEG_* table, decoded in RTL. IPI has no segment to
# place for any of it, so the address editor is told to stop asking.

source [file dirname [file normalize [info script]]]/50_addr_lit.tcl

# EVERY CONTROL WINDOW BELOW 4 GiB, because XDMA's M_AXI_LITE is 32-bit. A
# window it cannot reach fails silently, so this is checked, not assumed.
set nseg [llength $seg_base]
for {set k 0} {$k < $nseg} {incr k} {
    if {[lindex $seg_dprt $k] == 0} { continue }
    set win [expr {(~[lindex $seg_mask $k] & $FULL) + 1}]
    set top [expr {[lindex $seg_base $k] + $win}]
    if {$top > (1 << 32)} {
        error "v6 addr: segment $k ends at [format 0x%llX $top], above 4 GiB and\
               unreachable by XDMA AXI-Lite"
    }
}

set_property -dict [list \
    CONFIG.SEG_OVERRIDE {1} \
    CONFIG.SEG_BASE_P  [v6_cat $seg_base $AW] \
    CONFIG.SEG_MASK_P  [v6_cat $seg_mask $AW] \
    CONFIG.SEG_XLT_P   [v6_cat $seg_xlt  $AW] \
    CONFIG.SEG_DST_P   [v6_cat $seg_dst  2] \
    CONFIG.SEG_DPORT_P [v6_cat $seg_dprt 2] \
    CONFIG.SEG_VLD_P   [v6_cat [lrepeat $nseg 1] 1] \
] [get_bd_cells station_bus]

# NOT excluded, and not optional: MAG's own master. Connecting M_AXI_DRAM does
# not address it, and unassigned it ties off (v5 hit BD 41-1356 on all four).
foreach {mid mod} $MESHES {
    set ddr [dict get $DDR_OF_SLR $mid]
    assign_bd_address \
        -target_address_space [get_bd_addr_spaces mesh_$mid/M_AXI_DRAM] \
        [get_bd_addr_segs ddr4_$ddr/C0_DDR4_MEMORY_MAP/C0_DDR4_ADDRESS_BLOCK] -force
}

puts "@@@ v6 addr: $nseg RTL segments, 4 DRAM assigned"

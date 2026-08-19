# Runs AFTER the first validate_bd_design, which computes the map. Excluding
# does NOT work: `get_bd_addr_spaces` omits the station's masters entirely.
assign_bd_address -force

# Explicit and last: MAG's master must land on THIS die's controller, and
# ddr4_0 is in SLR3. Auto-assignment has no way to know that.
set v6_ndram 0
foreach {mid mod} $MESHES {
    if {![v6_has_mesh $mid]} { continue }
    set ddr [dict get $DDR_OF_SLR $mid]
    assign_bd_address \
        -target_address_space [get_bd_addr_spaces mesh_$mid/M_AXI_DRAM] \
        [get_bd_addr_segs ddr4_$ddr/C0_DDR4_MEMORY_MAP/C0_DDR4_ADDRESS_BLOCK] -force
    incr v6_ndram
}
puts "@@@ v6 addr fill: [llength [get_bd_addr_segs -quiet -excluded]] excluded,\
      $v6_ndram DRAM re-pinned to their own die"

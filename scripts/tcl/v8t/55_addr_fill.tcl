# Runs AFTER the first validate_bd_design, which computes the map. Excluding
# does NOT work: `get_bd_addr_spaces` omits the station's masters entirely.
assign_bd_address -force

# The memory path is explicit and last, because auto-assignment cannot know it:
#   node m's DRAM master  -> the Xache's S0m_AXI aperture, at 0
#   the Xache's M0h_AXI   -> the MIG in SLR h, at 0 (4 GB)
# A MIG left off a master's space ties off (BD 41-1356); a Xache aperture not
# at 0 would move every address the compiler emits.
set v8_nmap 0
foreach {mid mod} $MESHES {
    set ddr [dict get $DDR_OF_SLR $mid]
    set kxseg [get_bd_addr_segs -quiet xache/S0${mid}_AXI/*]
    if {![llength $kxseg]} { error "v8t addr: the Xache's S0${mid}_AXI has no segment to assign" }
    assign_bd_address -target_address_space [get_bd_addr_spaces node_$mid/M_AXI_DRAM] \
        $kxseg -offset 0x0 -force
    set kxsp [get_bd_addr_spaces -quiet xache/M0${mid}_AXI]
    if {![llength $kxsp]} { error "v8t addr: the Xache's M0${mid}_AXI has no address space" }
    assign_bd_address -target_address_space $kxsp \
        [get_bd_addr_segs ddr4_$ddr/C0_DDR4_MEMORY_MAP/C0_DDR4_ADDRESS_BLOCK] \
        -offset 0x0 -force
    incr v8_nmap
}
puts "@@@ v8t addr fill: [llength [get_bd_addr_segs -quiet -excluded]] excluded,\
      $v8_nmap node->Xache->MIG paths assigned"

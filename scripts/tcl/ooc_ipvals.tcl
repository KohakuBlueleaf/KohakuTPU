# Print valid values for the System Cache knobs we intend to set.
set part xcvu13p-fhgb2104-2L-e
create_project -force -in_memory -part $part
create_ip -vlnv xilinx.com:ip:system_cache:5.0 -module_name probeip
foreach p {C_CACHE_DATA_MEMORY_TYPE C_CACHE_TAG_MEMORY_TYPE C_CACHE_LRU_MEMORY_TYPE
           C_CACHE_DATA_WIDTH C_CACHE_SIZE C_CACHE_LINE_LENGTH C_GEN0_ENABLE_CACHE
           C_NUM_GENERIC_PORTS C_S0_AXI_GEN_DATA_WIDTH C_M0_AXI_DATA_WIDTH
           C_M0_AXI_ADDR_WIDTH} {
  if {[catch {set v [list_property_value CONFIG.$p [get_ips probeip]]} e]} {
    set v "ERR:$e"
  }
  puts "VALS $p = { $v }"
}
puts "@@@ done"

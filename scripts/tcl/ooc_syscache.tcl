# OOC synth of ONE Xilinx System Cache, correctly configured + caching enabled.
#   -tclargs <cache_bytes> <port_w> <mem_type> <tag> [<tag_mem_type>]
# mem_type -> CONFIG.C_CACHE_DATA_MEMORY_TYPE: 0 automatic, 2 BRAM, 3 URAM
# (the IP's component.xml choice list; there is no 1 for the data memory).
# tag_mem_type -> CONFIG.C_CACHE_TAG_MEMORY_TYPE: 0 automatic, 1 LUTRAM, 2 BRAM,
# 3 URAM; left at the IP's default when not given.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set sz  [lindex $argv 0]
set pw  [lindex $argv 1]
set mt  [lindex $argv 2]
set tag [lindex $argv 3]
set tmt [lindex $argv 4]
if {$sz eq ""} { set sz 32768 }
if {$pw eq ""} { set pw 512 }
if {$mt eq ""} { set mt 0 }
if {$tag eq ""} { set tag syscache }
set_param general.maxThreads 4
create_project -force -in_memory -part $part
create_ip -vlnv xilinx.com:ip:system_cache:5.0 -module_name syscache
set cfg [list \
    CONFIG.C_NUM_GENERIC_PORTS 1 \
    CONFIG.C_GEN0_ENABLE_CACHE 1 \
    CONFIG.C_S0_AXI_GEN_DATA_WIDTH $pw \
    CONFIG.C_M0_AXI_DATA_WIDTH $pw \
    CONFIG.C_M0_AXI_ADDR_WIDTH 40 \
    CONFIG.C_CACHE_DATA_WIDTH $pw \
    CONFIG.C_CACHE_SIZE $sz \
    CONFIG.C_CACHE_DATA_MEMORY_TYPE $mt ]
if {$tmt ne ""} { lappend cfg CONFIG.C_CACHE_TAG_MEMORY_TYPE $tmt }
if {[catch {set_property -dict $cfg [get_ips syscache]} e]} { puts "SETERR $e" }
foreach p {C_NUM_GENERIC_PORTS C_GEN0_ENABLE_CACHE C_S0_AXI_GEN_DATA_WIDTH
           C_M0_AXI_DATA_WIDTH C_CACHE_DATA_WIDTH C_CACHE_SIZE C_CACHE_LINE_LENGTH
           C_CACHE_DATA_MEMORY_TYPE C_CACHE_TAG_MEMORY_TYPE} {
    puts "APPLIED $p = [get_property CONFIG.$p [get_ips syscache]]"
}
generate_target synthesis [get_ips syscache]
synth_design -top syscache -part $part -mode out_of_context
source [file join $root scripts tcl ooc_class.tcl]
ooc_record $tag "system_cache sz=$sz pw=$pw mt=$mt" 2000 2
puts "@@@ syscache done $tag"

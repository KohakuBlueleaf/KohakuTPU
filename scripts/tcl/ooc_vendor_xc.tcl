# Vendor xbar-cache baseline at a UNIFORM shape: M masters -> axi crossbar -> N
# system_caches -> N DRAM ports, all one width, one clock. The number to compare the
# Xache (kx_xache) against (same M/N/W/cache size). Composition is ONE BD so the
# vendor gets the same fusion opportunity; per-cell counts are reported too.
#   vivado -mode batch -source ooc_vendor_xc.tcl -tclargs <out> <ip> <M> <N> <W> <cache_bytes>
#   ip = smartconnect (SAMD) | axi_interconnect (strategy 1 = shared/SASD, 2 = full)
set out   [lindex $argv 0]
set ip    [lindex $argv 1]
set M     [lindex $argv 2]
set N     [lindex $argv 3]
set W     [lindex $argv 4]
set csz   [lindex $argv 5]
if {$W   eq ""} { set W 512 }
if {$csz eq ""} { set csz 16384 }
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join [file dirname [info script]] ooc_class.tcl]

file mkdir $out
create_project vxc [file join $out proj] -part $part -force
config_ip_cache -disable_cache
create_bd_design bd

for {set i 0} {$i < $M} {incr i} {
    set c [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_vip m$i]
    set_property -dict [list CONFIG.INTERFACE_MODE {MASTER} CONFIG.PROTOCOL {AXI4} \
        CONFIG.DATA_WIDTH $W CONFIG.ADDR_WIDTH {40} CONFIG.ID_WIDTH {4}] $c
}
for {set j 0} {$j < $N} {incr j} {
    set c [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_vip d$j]
    set_property -dict [list CONFIG.INTERFACE_MODE {SLAVE} CONFIG.PROTOCOL {AXI4} \
        CONFIG.DATA_WIDTH $W CONFIG.ADDR_WIDTH {40} CONFIG.ID_WIDTH {8}] $c
    set sc [create_bd_cell -type ip -vlnv xilinx.com:ip:system_cache c$j]
    set_property -dict [list CONFIG.C_NUM_GENERIC_PORTS 1 CONFIG.C_GEN0_ENABLE_CACHE 1 \
        CONFIG.C_S0_AXI_GEN_DATA_WIDTH $W CONFIG.C_M0_AXI_DATA_WIDTH $W \
        CONFIG.C_M0_AXI_ADDR_WIDTH 40 CONFIG.C_CACHE_DATA_WIDTH $W \
        CONFIG.C_CACHE_SIZE $csz] $sc
}

if {$ip eq "smartconnect"} {
    set xb [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect xbar]
    set_property -dict [list CONFIG.NUM_SI $M CONFIG.NUM_MI $N CONFIG.NUM_CLKS 1] $xb
} else {
    set xb [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect xbar]
    set_property -dict [list CONFIG.NUM_SI $M CONFIG.NUM_MI $N \
        CONFIG.XBAR_DATA_WIDTH $W CONFIG.STRATEGY 2] $xb
}
for {set i 0} {$i < $M} {incr i} {
    connect_bd_intf_net [get_bd_intf_pins m$i/M_AXI] [get_bd_intf_pins xbar/S[format %02d $i]_AXI]
}
for {set j 0} {$j < $N} {incr j} {
    connect_bd_intf_net [get_bd_intf_pins xbar/M[format %02d $j]_AXI] [get_bd_intf_pins c$j/S0_AXI_GEN]
    connect_bd_intf_net [get_bd_intf_pins c$j/M0_AXI] [get_bd_intf_pins d$j/S_AXI]
}

create_bd_port -dir I -type clk clk
set_property CONFIG.FREQ_HZ 300000000 [get_bd_ports clk]
create_bd_port -dir I -type rst aresetn
for {set i 0} {$i < $M} {incr i} {
    connect_bd_net [get_bd_ports clk] [get_bd_pins m$i/aclk]
    connect_bd_net [get_bd_ports aresetn] [get_bd_pins m$i/aresetn]
}
for {set j 0} {$j < $N} {incr j} {
    connect_bd_net [get_bd_ports clk] [get_bd_pins d$j/aclk]
    connect_bd_net [get_bd_ports aresetn] [get_bd_pins d$j/aresetn]
    connect_bd_net [get_bd_ports clk] [get_bd_pins c$j/ACLK]
    connect_bd_net [get_bd_ports aresetn] [get_bd_pins c$j/ARESETN]
}
if {$ip eq "smartconnect"} {
    connect_bd_net [get_bd_ports clk] [get_bd_pins xbar/aclk]
    connect_bd_net [get_bd_ports aresetn] [get_bd_pins xbar/aresetn]
} else {
    foreach p [get_bd_pins -quiet xbar/*ACLK] { connect_bd_net [get_bd_ports clk] $p }
    foreach p [get_bd_pins -quiet xbar/*ARESETN] { connect_bd_net [get_bd_ports aresetn] $p }
}

for {set i 0} {$i < $M} {incr i} {
    for {set j 0} {$j < $N} {incr j} {
        assign_bd_address -target_address_space [get_bd_addr_spaces m$i/Master_AXI] \
            [get_bd_addr_segs d$j/S_AXI/Reg] -offset [expr {$j * 0x100000000}] \
            -range 0x100000000 -force
    }
}

validate_bd_design
save_bd_design
set bdf [get_files bd.bd]
set_property synth_checkpoint_mode None $bdf
generate_target all $bdf
make_wrapper -files $bdf -top -import
launch_runs synth_1 -jobs 4
wait_on_run synth_1
open_run synth_1 -name synth_1

report_utilization -hierarchical -hierarchical_depth 3 -file "$out/hier.rpt"
report_utilization -file "$out/util.rpt"
puts "@@@ vendor-xc ip=$ip M=$M N=$N W=$W cache=$csz"
ooc_count "xbar" "bd_i/xbar"
for {set j 0} {$j < $N} {incr j} { ooc_count "cache$j" "bd_i/c$j" }
ooc_count TOTAL
set fh [open "$out/result.txt" w]
foreach line [split [report_utilization -return_string] "\n"] {
    if {[regexp {CLB LUTs\*?\s+\|\s+(\d+)} $line -> v]} { puts $fh "LUT=$v" }
    if {[regexp {CLB Registers\s+\|\s+(\d+)} $line -> v]} { puts $fh "FF=$v" }
    if {[regexp {Block RAM Tile\s+\|\s+(\d+)} $line -> v]} { puts $fh "BRAM=$v" }
    if {[regexp {\|\s*URAM\s+\|\s+(\d+)} $line -> v]} { puts $fh "URAM=$v" }
}
close $fh
puts "@@@ vendor_xc done"

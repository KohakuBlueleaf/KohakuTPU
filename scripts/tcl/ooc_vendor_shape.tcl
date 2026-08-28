# Vendor M×N interconnect at a UNIFORM shape (all ports one width, nclk domains),
# for the general-config comparison. Both families:
#   axi_interconnect  strat 1 = min-area SASD, strat 2 = performance (parallel)
#   smartconnect                = full SAMD crossbar
#   vivado -mode batch -source ooc_vendor_shape.tcl -tclargs <out> <ip> <strat> <M> <N> <W> <nclk>
set out   [lindex $argv 0]
set ip    [lindex $argv 1]
set strat [lindex $argv 2]
set M     [lindex $argv 3]
set N     [lindex $argv 4]
set W     [lindex $argv 5]
set nclk  [lindex $argv 6]
if {$strat eq ""} { set strat 1 }
if {$W    eq ""} { set W 512 }
if {$nclk eq ""} { set nclk 4 }
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join [file dirname [info script]] ooc_class.tcl]

file mkdir $out
create_project vshape [file join $out proj] -part $part -force
config_ip_cache -disable_cache
create_bd_design bd

for {set i 0} {$i < $M} {incr i} {
    set c [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_vip m$i]
    set_property -dict [list CONFIG.INTERFACE_MODE {MASTER} CONFIG.PROTOCOL {AXI4} \
        CONFIG.DATA_WIDTH $W CONFIG.ADDR_WIDTH {40} CONFIG.ID_WIDTH {4}] $c
}
for {set j 0} {$j < $N} {incr j} {
    set c [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_vip s$j]
    set_property -dict [list CONFIG.INTERFACE_MODE {SLAVE} CONFIG.PROTOCOL {AXI4} \
        CONFIG.DATA_WIDTH $W CONFIG.ADDR_WIDTH {40} CONFIG.ID_WIDTH {8}] $c
}

if {$ip eq "smartconnect"} {
    set xb [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect xbar]
    set_property -dict [list CONFIG.NUM_SI $M CONFIG.NUM_MI $N CONFIG.NUM_CLKS $nclk] $xb
} else {
    set xb [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect xbar]
    set_property -dict [list CONFIG.NUM_SI $M CONFIG.NUM_MI $N \
        CONFIG.XBAR_DATA_WIDTH $W CONFIG.STRATEGY $strat] $xb
}

for {set i 0} {$i < $M} {incr i} {
    connect_bd_intf_net [get_bd_intf_pins m$i/M_AXI] \
        [get_bd_intf_pins xbar/S[format %02d $i]_AXI]
}
for {set j 0} {$j < $N} {incr j} {
    connect_bd_intf_net [get_bd_intf_pins xbar/M[format %02d $j]_AXI] \
        [get_bd_intf_pins s$j/S_AXI]
}

for {set k 0} {$k < $nclk} {incr k} {
    create_bd_port -dir I -type clk clk$k
    set_property CONFIG.FREQ_HZ [expr {200000000 + $k*50000000}] [get_bd_ports clk$k]
}
create_bd_port -dir I -type rst aresetn

proc dom {idx nclk} { return clk[expr {$idx % $nclk}] }
for {set i 0} {$i < $M} {incr i} {
    connect_bd_net [get_bd_ports [dom $i $nclk]] [get_bd_pins m$i/aclk]
    connect_bd_net [get_bd_ports aresetn] [get_bd_pins m$i/aresetn]
}
for {set j 0} {$j < $N} {incr j} {
    connect_bd_net [get_bd_ports [dom $j $nclk]] [get_bd_pins s$j/aclk]
    connect_bd_net [get_bd_ports aresetn] [get_bd_pins s$j/aresetn]
}
# axi_interconnect takes a clock/reset per SI/MI + core; smartconnect takes NUM_CLKS.
if {$ip eq "smartconnect"} {
    for {set k 0} {$k < $nclk} {incr k} {
        connect_bd_net [get_bd_ports clk$k] [get_bd_pins xbar/aclk[expr {$k==0?"":$k}]]
    }
    connect_bd_net [get_bd_ports aresetn] [get_bd_pins xbar/aresetn]
} else {
    connect_bd_net [get_bd_ports clk0] [get_bd_pins xbar/ACLK]
    connect_bd_net [get_bd_ports aresetn] [get_bd_pins xbar/ARESETN]
    for {set i 0} {$i < $M} {incr i} {
        connect_bd_net [get_bd_ports [dom $i $nclk]] [get_bd_pins xbar/S[format %02d $i]_ACLK]
        connect_bd_net [get_bd_ports aresetn] [get_bd_pins xbar/S[format %02d $i]_ARESETN]
    }
    for {set j 0} {$j < $N} {incr j} {
        connect_bd_net [get_bd_ports [dom $j $nclk]] [get_bd_pins xbar/M[format %02d $j]_ACLK]
        connect_bd_net [get_bd_ports aresetn] [get_bd_pins xbar/M[format %02d $j]_ARESETN]
    }
}

for {set j 0} {$j < $N} {incr j} {
    set sp [get_bd_addr_spaces m0/Master_AXI]
    set seg [get_bd_addr_segs s$j/S_AXI/Reg]
    assign_bd_address -target_address_space $sp $seg \
        -offset [expr {$j * 0x100000000}] -range 0x100000000 -force
}
for {set i 1} {$i < $M} {incr i} {
    for {set j 0} {$j < $N} {incr j} {
        assign_bd_address -target_address_space [get_bd_addr_spaces m$i/Master_AXI] \
            [get_bd_addr_segs s$j/S_AXI/Reg] \
            -offset [expr {$j * 0x100000000}] -range 0x100000000 -force
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
puts "@@@ shape ip=$ip strat=$strat M=$M N=$N W=$W nclk=$nclk"
ooc_count "v-$ip-${M}x${N}"
puts "@@@ vendor_shape done"

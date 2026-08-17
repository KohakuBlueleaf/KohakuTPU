# axi_interconnect at root_smc's shape: the legacy crossbar, STRATEGY 1 being
# minimum-area shared-bus. Refuses to instantiate outside IPI, so it needs a BD.

#   vivado -mode batch -source scripts/tcl/ooc_xbar_baseline.tcl -tclargs <out> <strategy>

# Unlike SmartConnect this IP takes a clock and reset PER PORT, which is why the
# domains land correctly here without any FREQ_HZ propagation argument.

set out  [lindex $argv 0]
set strat [lindex $argv 1]
if {$strat eq ""} { set strat 1 }
set part xcvu13p-fhgb2104-2L-e

set_param general.maxThreads 4
source [file join [file dirname [info script]] ooc_class.tcl]

file mkdir $out
create_project xbarbase [file join $out proj] -part $part -force
# Byte-identical results across three different configs meant the IP cache was
# serving a stale DCP; a fresh output directory does not miss it.
config_ip_cache -disable_cache
create_bd_design bd

set MASTERS {m0 32 AXI4 m1 512 AXI4 m2 32 AXI4LITE}
set SLAVES  {s0 512 s1 512 s2 512 s3 32 s4 32 s5 32 s6 32 s7 32 s8 32}

foreach {nm w proto} $MASTERS {
    set c [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_vip $nm]
    set_property -dict [list CONFIG.INTERFACE_MODE {MASTER} \
                             CONFIG.PROTOCOL $proto CONFIG.DATA_WIDTH $w \
                             CONFIG.ADDR_WIDTH {40} CONFIG.ID_WIDTH {4}] $c
}
foreach {nm w} $SLAVES {
    set c [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_vip $nm]
    set_property -dict [list CONFIG.INTERFACE_MODE {SLAVE} \
                             CONFIG.PROTOCOL {AXI4} CONFIG.DATA_WIDTH $w \
                             CONFIG.ADDR_WIDTH {40} CONFIG.ID_WIDTH {8}] $c
}

set xb [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect xbar]
# XBAR_DATA_WIDTH IS NOT INFERRED. Left alone it defaults to 32 and the whole
# measurement is of a 32-bit crossbar: 6,994 LUT against root_smc's 41,788.
set_property -dict [list CONFIG.NUM_SI {3} CONFIG.NUM_MI {9} \
                         CONFIG.XBAR_DATA_WIDTH {512} \
                         CONFIG.STRATEGY $strat] $xb

set i 0
foreach {nm w proto} $MASTERS {
    connect_bd_intf_net [get_bd_intf_pins $nm/M_AXI] \
        [get_bd_intf_pins xbar/S0${i}_AXI]
    incr i
}
set i 0
foreach {nm w} $SLAVES {
    connect_bd_intf_net [get_bd_intf_pins xbar/M0${i}_AXI] \
        [get_bd_intf_pins $nm/S_AXI]
    incr i
}

foreach {nm hz} {clk_ctrl 100000000 clk_xdma 250000000 \
                 clk_mesh 237000000 clk_ddr 300000000} {
    create_bd_port -dir I -type clk $nm
    set_property CONFIG.FREQ_HZ $hz [get_bd_ports $nm]
}
create_bd_port -dir I -type rst aresetn

array set DOM {m0 clk_ctrl m1 clk_xdma m2 clk_xdma \
                s0 clk_ctrl s1 clk_mesh s2 clk_ctrl s3 clk_mesh s4 clk_ddr \
                s5 clk_ctrl s6 clk_ctrl s7 clk_ctrl s8 clk_ctrl}
foreach {cell clk} [array get DOM] {
    connect_bd_net [get_bd_ports $clk] [get_bd_pins $cell/aclk]
    connect_bd_net [get_bd_ports aresetn] [get_bd_pins $cell/aresetn]
}

# PER-PORT clocks: the interconnect core runs on ACLK and each SI/MI has its
# own, so the four domains are explicit rather than propagated.
connect_bd_net [get_bd_ports clk_ctrl] [get_bd_pins xbar/ACLK]
connect_bd_net [get_bd_ports aresetn]  [get_bd_pins xbar/ARESETN]
set i 0
foreach {nm w proto} $MASTERS {
    connect_bd_net [get_bd_ports $DOM($nm)] [get_bd_pins xbar/S0${i}_ACLK]
    connect_bd_net [get_bd_ports aresetn]   [get_bd_pins xbar/S0${i}_ARESETN]
    incr i
}
set i 0
foreach {nm w} $SLAVES {
    connect_bd_net [get_bd_ports $DOM($nm)] [get_bd_pins xbar/M0${i}_ACLK]
    connect_bd_net [get_bd_ports aresetn]   [get_bd_pins xbar/M0${i}_ARESETN]
    incr i
}

array set SEG {s0 {0x8000000000 0x1000000000} s1 {0x9000000000 0x1000000000} \
               s2 {0xA000000000 0x2000000000} s3 {0x00810000 0x10000} \
               s4 {0x00300000 0x100000}       s5 {0x00900000 0x10000} \
               s6 {0x00910000 0x10000}        s7 {0x00920000 0x10000} \
               s8 {0x00930000 0x10000}}

foreach {nm w proto} $MASTERS {
    set sp [get_bd_addr_spaces $nm/Master_AXI]
    foreach {sl pair} [array get SEG] {
        lassign $pair off rng
        set seg  [get_bd_addr_segs $sl/S_AXI/Reg]
        set mesh [expr {[lsearch {s0 s1 s2} $sl] >= 0}]
        if {($nm eq "m1" && !$mesh) || ($nm eq "m2" && $mesh)} {
            exclude_bd_addr_seg -target_address_space $sp $seg
            continue
        }
        assign_bd_address -target_address_space $sp $seg -offset $off \
                          -range $rng -force
    }
}

# Force what will not propagate. axi_vip does not export DATA_WIDTH onto the
# connected interconnect port, so it stays at the 32-bit default.
set i 0
foreach {nm w proto} $MASTERS {
    catch { set_property CONFIG.DATA_WIDTH $w [get_bd_intf_pins xbar/S0${i}_AXI] }
    incr i
}
set i 0
foreach {nm w} $SLAVES {
    catch { set_property CONFIG.DATA_WIDTH $w [get_bd_intf_pins xbar/M0${i}_AXI] }
    incr i
}

validate_bd_design
save_bd_design

foreach p [get_bd_intf_pins -quiet xbar/*] {
    puts [format "@@@ %-18s DW %s FREQ %s" $p \
              [get_property CONFIG.DATA_WIDTH $p] \
              [get_property CONFIG.FREQ_HZ $p]]
}
puts "@@@ XBAR_DATA_WIDTH [get_property CONFIG.XBAR_DATA_WIDTH [get_bd_cells xbar]]"

set bdf [get_files bd.bd]
set_property synth_checkpoint_mode None $bdf
generate_target all $bdf
make_wrapper -files $bdf -top -import

launch_runs synth_1 -jobs 4
wait_on_run synth_1
open_run synth_1 -name synth_1

puts "@@@ strategy $strat"
set c [get_cells -quiet bd_i/xbar]
if {[llength $c]} { ooc_count "xbar" "bd_i/xbar" } else { puts "@@@ xbar NOT FOUND" }
ooc_count TOTAL
puts "@@@ xbar_baseline done"

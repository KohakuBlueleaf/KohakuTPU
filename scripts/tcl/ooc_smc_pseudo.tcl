# Vendor interconnect at a given port count, from real endpoint IP so widths and
# frequencies propagate; an axi_vip-slave rebuild reads 3.3x low.
#   -tclargs <nsi> <nmi> <si_dw> <mi_dw> <nclk> <tag> <ip> <strategy>
# ip smartconnect|axi_interconnect; strategy 1 min-area (SASD), 2 max-perf.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set nsi   [lindex $argv 0]
set nmi   [lindex $argv 1]
set sidw  [lindex $argv 2]
set midw  [lindex $argv 3]
set nclk  [lindex $argv 4]
set tag   [lindex $argv 5]
set ip    [lindex $argv 6]
set strat [lindex $argv 7]
if {$ip    eq ""} { set ip    smartconnect }
if {$strat eq ""} { set strat 2 }
if {$nsi  eq ""} { set nsi  3 }
if {$nmi  eq ""} { set nmi  9 }
if {$sidw eq ""} { set sidw 512 }
if {$midw eq ""} { set midw 512 }
if {$nclk eq ""} { set nclk 4 }
if {$tag  eq ""} { set tag  "smc${nsi}x${nmi}" }

set_param general.maxThreads 4
create_project -force -in_memory -part $part
create_bd_design bd

# One clk_wiz per domain so each port carries a real, declared FREQ_HZ.
# clk_in1 must reach a real source or validate_bd_design fails BD 41-758.
set cin [create_bd_port -dir I -type clk -freq_hz 100000000 clk_in]
set mhz {250.000 100.000 300.000 237.000 180.000 210.000 200.000 150.000}
for {set c 0} {$c < $nclk} {incr c} {
    set w [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz ck$c]
    set_property -dict [list CONFIG.PRIM_SOURCE {No_buffer} \
        CONFIG.PRIM_IN_FREQ {100.000} \
        CONFIG.CLKOUT1_REQUESTED_OUT_FREQ [lindex $mhz $c] \
        CONFIG.CLKOUT1_DRIVES {Buffer} CONFIG.PRIMITIVE {MMCM} \
        CONFIG.USE_RESET {false} CONFIG.FEEDBACK_SOURCE {FDBK_AUTO}] $w
    connect_bd_net $cin [get_bd_pins ck$c/clk_in1]
    set r [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rs$c]
    connect_bd_net [get_bd_pins ck$c/clk_out1] [get_bd_pins rs$c/slowest_sync_clk]
    connect_bd_net [get_bd_pins ck$c/locked]   [get_bd_pins rs$c/ext_reset_in]
}

set smc [create_bd_cell -type ip -vlnv xilinx.com:ip:$ip smc]
if {$ip eq "smartconnect"} {
    set_property -dict [list CONFIG.NUM_SI $nsi CONFIG.NUM_MI $nmi \
                             CONFIG.NUM_CLKS $nclk] $smc
    for {set c 0} {$c < $nclk} {incr c} {
        set p [expr {$c ? "aclk$c" : "aclk"}]
        connect_bd_net [get_bd_pins ck$c/clk_out1] [get_bd_pins smc/$p]
    }
    connect_bd_net [get_bd_pins rs0/peripheral_aresetn] [get_bd_pins smc/aresetn]
} else {
    # A clock and reset per SI and per MI, plus its own. XBAR_DATA_WIDTH is NOT
    # inferred from connected ports and silently defaults to 32.
    set_property -dict [list CONFIG.NUM_SI $nsi CONFIG.NUM_MI $nmi \
                             CONFIG.STRATEGY $strat \
                             CONFIG.XBAR_DATA_WIDTH $midw] $smc
    if {[get_property CONFIG.XBAR_DATA_WIDTH $smc] != $midw} {
        error "axi_interconnect XBAR_DATA_WIDTH stuck at\
               [get_property CONFIG.XBAR_DATA_WIDTH $smc], wanted $midw"
    }
    connect_bd_net [get_bd_pins ck0/clk_out1] [get_bd_pins smc/ACLK]
    connect_bd_net [get_bd_pins rs0/peripheral_aresetn] [get_bd_pins smc/ARESETN]
    for {set i 0} {$i < $nsi} {incr i} {
        set c [expr {$i % $nclk}]
        connect_bd_net [get_bd_pins ck$c/clk_out1] \
            [get_bd_pins smc/S[format %02d $i]_ACLK]
        connect_bd_net [get_bd_pins rs$c/peripheral_aresetn] \
            [get_bd_pins smc/S[format %02d $i]_ARESETN]
    }
    for {set j 0} {$j < $nmi} {incr j} {
        set c [expr {$j % $nclk}]
        connect_bd_net [get_bd_pins ck$c/clk_out1] \
            [get_bd_pins smc/M[format %02d $j]_ACLK]
        connect_bd_net [get_bd_pins rs$c/peripheral_aresetn] \
            [get_bd_pins smc/M[format %02d $j]_ARESETN]
    }
}

# Masters: axi_vip in master mode, with FREQ_HZ forced onto the interface so
# the clock converter count is not silently zero.
for {set i 0} {$i < $nsi} {incr i} {
    set v [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_vip m$i]
    set_property -dict [list CONFIG.INTERFACE_MODE {MASTER} \
        CONFIG.PROTOCOL {AXI4} CONFIG.DATA_WIDTH $sidw \
        CONFIG.ADDR_WIDTH {43} CONFIG.ID_WIDTH {4} \
        CONFIG.HAS_BURST {1} CONFIG.SUPPORTS_NARROW {1} \
        CONFIG.READ_WRITE_MODE {READ_WRITE}] $v
    set ck [expr {$i % $nclk}]
    connect_bd_net [get_bd_pins ck$ck/clk_out1] [get_bd_pins m$i/aclk]
    connect_bd_net [get_bd_pins rs$ck/peripheral_aresetn] [get_bd_pins m$i/aresetn]
    connect_bd_intf_net [get_bd_intf_pins m$i/M_AXI] \
                        [get_bd_intf_pins smc/S[format %02d $i]_AXI]
}

# Slaves: axi_bram_ctrl, real IP that declares its own width and depth.
for {set j 0} {$j < $nmi} {incr j} {
    set dw [expr {$j == 0 ? $midw : 32}]
    set b [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_bram_ctrl s$j]
    set_property -dict [list CONFIG.DATA_WIDTH $dw \
        CONFIG.SINGLE_PORT_BRAM {1} CONFIG.ECC_TYPE {0}] $b
    set m [create_bd_cell -type ip -vlnv xilinx.com:ip:blk_mem_gen s${j}_mem]
    set_property -dict [list CONFIG.Memory_Type {Single_Port_RAM} \
        CONFIG.use_bram_block {BRAM_Controller}] $m
    connect_bd_intf_net [get_bd_intf_pins s$j/BRAM_PORTA] \
                        [get_bd_intf_pins s${j}_mem/BRAM_PORTA]
    set ck [expr {$j % $nclk}]
    connect_bd_net [get_bd_pins ck$ck/clk_out1] [get_bd_pins s$j/s_axi_aclk]
    connect_bd_net [get_bd_pins rs$ck/peripheral_aresetn] [get_bd_pins s$j/s_axi_aresetn]
    connect_bd_intf_net [get_bd_intf_pins smc/M[format %02d $j]_AXI] \
                        [get_bd_intf_pins s$j/S_AXI]
}

assign_bd_address -force
validate_bd_design
save_bd_design

generate_target synthesis [get_files bd.bd]
synth_design -top bd -part $part -mode out_of_context

source [file join $root scripts tcl ooc_class.tcl]
puts "@@@ ============================ $tag  nsi $nsi nmi $nmi clks $nclk"
ooc_record $tag "ip=$ip strat=$strat nsi=$nsi nmi=$nmi sidw=$sidw midw=$midw nclk=$nclk"
ooc_util
puts "@@@ ============================ hierarchy $tag"
ooc_hier 1
puts "@@@ smc pseudo done $tag"

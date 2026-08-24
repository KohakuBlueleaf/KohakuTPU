# v6 interconnect BD: the station-bus line, JTAG-driven, into BRAM endpoints.
#   vivado -mode batch -source scripts/tcl/v6_bd.tcl -tclargs impl

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set design_name v6bus
set proj_dir C:/Users/apoll/Desktop/vivado/v6_bus

set do_impl [expr {[lsearch $argv impl] >= 0}]

set BUS_MHZ  200.0
set CTRL_MHZ 100.0
set XDMA_MHZ 250.0
set NQ       4
set AW       43
# Station-bus configuration. FW=256 meets the mesh's 256-bit S_AXI_MEM exactly;
# the 512-bit XDMA master splits 2:1 in the NMU.
set FW           256
set OST          4
set STORE_FWD    1
set LUT_PER_BRAM 0
set TIMEOUT      0
set CRED         16
set PIPE         4
set SLR_MHZ  {0 237.000 1 300.000 2 180.000 3 210.000}
set SLR_ROWS {0 {Y0 Y3} 1 {Y4 Y7} 2 {Y8 Y11} 3 {Y12 Y15}}

set_param general.maxThreads 8
create_project -force $design_name $proj_dir -part $part

set common [file join $root src kohakuaccel common]
set stn    [file join $root src kohakuaccel axi station]
set link   [file join $root src kohakuaccel axi link]
set topo   [file join $root src kohakuaccel axi topo]

add_files -norecurse [list \
    [file join $common sync_fifo.v] \
    [file join $common async_fifo.v] \
    [file join $common sb_skid.v] \
    [file join $stn sb_hub.v] \
    [file join $stn sb_nmu.v] \
    [file join $stn sb_nsu.v] \
    [file join $link sb_link.v] \
    [file join $link sb_link_cdc.v] \
    [file join $topo sb_stn_line.v] \
    [file join $topo sb_line4.v] \
    [file join $root src kohakuaccel axi bd sb_v6_bus.v]]
update_compile_order -fileset sources_1

create_bd_design $design_name
current_bd_design $design_name

# One differential clock; every master is JTAG so no other pin is needed.
set sysp [create_bd_intf_port -mode Slave \
              -vlnv xilinx.com:interface:diff_clock_rtl:1.0 system_clk]
set_property CONFIG.FREQ_HZ {100000000} $sysp

set udbs [create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_sys]
set_property CONFIG.C_BUF_TYPE {IBUFDS} $udbs
set udbg [create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_bufg]
set_property CONFIG.C_BUF_TYPE {BUFG} $udbg
connect_bd_intf_net $sysp [get_bd_intf_pins util_ds_buf_sys/CLK_IN_D]
connect_bd_net [get_bd_pins util_ds_buf_sys/IBUF_OUT] \
               [get_bd_pins util_ds_buf_bufg/BUFG_I]

proc wiz {name mhz {mhz2 0}} {
    set w [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz $name]
    set cfg [list \
      CONFIG.PRIM_SOURCE {No_buffer} CONFIG.PRIM_IN_FREQ {100.000} \
      CONFIG.CLKOUT1_REQUESTED_OUT_FREQ [format %.3f $mhz] \
      CONFIG.CLKOUT1_DRIVES {Buffer} CONFIG.PRIMITIVE {MMCM} \
      CONFIG.USE_RESET {false} CONFIG.FEEDBACK_SOURCE {FDBK_AUTO}]
    if {$mhz2 > 0} {
        lappend cfg CONFIG.CLKOUT2_USED {true} \
                    CONFIG.CLKOUT2_REQUESTED_OUT_FREQ [format %.3f $mhz2] \
                    CONFIG.CLKOUT2_DRIVES {Buffer}
    }
    set_property -dict $cfg $w
    connect_bd_net [get_bd_pins $name/clk_in1] \
                   [get_bd_pins util_ds_buf_bufg/BUFG_O]
    return $w
}

proc psr {name clkpin} {
    create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset $name
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/slowest_sync_clk]
    connect_bd_net [get_bd_pins $name/ext_reset_in] \
                   [get_bd_pins clk_wiz_ctrl/locked]
}

wiz clk_wiz_ctrl $CTRL_MHZ $XDMA_MHZ
# One fabric clock per station: a shared clock over four SLRs couples every
# station to the worst one, and the link's CDC exists to make that unnecessary.
foreach slr {0 1 2 3} {
    wiz clk_wiz_bus$slr $BUS_MHZ
    wiz clk_wiz_s$slr [dict get $SLR_MHZ $slr]
}
wiz clk_wiz_ddr 300.000

psr rst_ctrl clk_wiz_ctrl/clk_out1
psr rst_xdma clk_wiz_ctrl/clk_out2
psr rst_ddr  clk_wiz_ddr/clk_out1
foreach slr {0 1 2 3} {
    psr rst_bus$slr clk_wiz_bus$slr/clk_out1
    psr rst_s$slr   clk_wiz_s$slr/clk_out1
}

# The line takes an ACTIVE HIGH bus reset; proc_sys_reset gives active low.
foreach slr {0 1 2 3} {
    set inv [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic \
                 bus_rst_inv$slr]
    set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] $inv
    connect_bd_net [get_bd_pins rst_bus$slr/peripheral_aresetn] \
                   [get_bd_pins bus_rst_inv$slr/Op1]
}

set bus [create_bd_cell -type module -reference sb_bd_line4 station_bus]
# Stated, not inherited: this is the knob set a retune moves, and a default that
# drifts under the BD is the kind of change nobody notices.
set_property -dict [list CONFIG.FW $FW CONFIG.OST $OST \
    CONFIG.STORE_FWD $STORE_FWD CONFIG.LUT_PER_BRAM $LUT_PER_BRAM \
    CONFIG.TIMEOUT $TIMEOUT CONFIG.LINK_CDC 1 CONFIG.LINK_FULL 0 \
    CONFIG.CRED $CRED CONFIG.PIPE $PIPE] $bus
foreach slr {0 1 2 3} {
    connect_bd_net [get_bd_pins clk_wiz_bus$slr/clk_out1] \
                   [get_bd_pins station_bus/bus_clk$slr]
    connect_bd_net [get_bd_pins bus_rst_inv$slr/Res] \
                   [get_bd_pins station_bus/bus_rst$slr]
    connect_bd_net [get_bd_pins clk_wiz_s$slr/clk_out1] \
                   [get_bd_pins station_bus/clk_s$slr]
    connect_bd_net [get_bd_pins rst_s$slr/peripheral_aresetn] \
                   [get_bd_pins station_bus/aresetn_s$slr]
}
connect_bd_net [get_bd_pins clk_wiz_ctrl/clk_out1] [get_bd_pins station_bus/clk_ctrl]
connect_bd_net [get_bd_pins rst_ctrl/peripheral_aresetn] \
               [get_bd_pins station_bus/aresetn_ctrl]
connect_bd_net [get_bd_pins clk_wiz_ctrl/clk_out2] [get_bd_pins station_bus/clk_xdma]
connect_bd_net [get_bd_pins rst_xdma/peripheral_aresetn] \
               [get_bd_pins station_bus/aresetn_xdma]
connect_bd_net [get_bd_pins clk_wiz_ddr/clk_out1] [get_bd_pins station_bus/clk_ddr]
connect_bd_net [get_bd_pins rst_ddr/peripheral_aresetn] \
               [get_bd_pins station_bus/aresetn_ddr]

# PROTOCOL 0 is AXI4; the default 2 is AXI4-Lite and will not connect.
# 64, not 32: the station field sits at bit AW-4, so a 32-bit master reaches
# station 0 only and three quarters of the line goes untested.
proc jtag_master {name clkpin rstpin} {
    set j [create_bd_cell -type ip -vlnv xilinx.com:ip:jtag_axi $name]
    set_property -dict [list CONFIG.PROTOCOL {0} CONFIG.M_AXI_ID_WIDTH {4} \
                             CONFIG.M_AXI_ADDR_WIDTH {64}] $j
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/aclk]
    connect_bd_net [get_bd_pins $rstpin] [get_bd_pins $name/aresetn]
}

jtag_master jtag_ctrl clk_wiz_ctrl/clk_out1 rst_ctrl/peripheral_aresetn
jtag_master jtag_wide clk_wiz_ctrl/clk_out2 rst_xdma/peripheral_aresetn
jtag_master jtag_lite clk_wiz_ctrl/clk_out2 rst_xdma/peripheral_aresetn

# jtag_axi is 32-bit; the wide manager port is 512, so convert.
set dwc [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dwidth_converter dwc_wide]
set_property -dict [list CONFIG.SI_DATA_WIDTH {32} CONFIG.MI_DATA_WIDTH {512}] $dwc
connect_bd_net [get_bd_pins clk_wiz_ctrl/clk_out2] [get_bd_pins dwc_wide/s_axi_aclk]
connect_bd_net [get_bd_pins rst_xdma/peripheral_aresetn] \
               [get_bd_pins dwc_wide/s_axi_aresetn]

connect_bd_intf_net [get_bd_intf_pins jtag_ctrl/M_AXI] [get_bd_intf_pins station_bus/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins jtag_wide/M_AXI] [get_bd_intf_pins dwc_wide/S_AXI]
connect_bd_intf_net [get_bd_intf_pins dwc_wide/M_AXI]  [get_bd_intf_pins station_bus/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins jtag_lite/M_AXI] [get_bd_intf_pins station_bus/S02_AXI]

# Endpoints: geometry comes from the controller, so do not set port B by hand.
proc bram_ep {name dw clkpin rstpin} {
    set c [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_bram_ctrl $name]
    set_property -dict [list CONFIG.DATA_WIDTH $dw CONFIG.SINGLE_PORT_BRAM {1} \
                             CONFIG.ECC_TYPE {0}] $c
    set m [create_bd_cell -type ip -vlnv xilinx.com:ip:blk_mem_gen ${name}_mem]
    set_property -dict [list CONFIG.Memory_Type {Single_Port_RAM} \
                             CONFIG.use_bram_block {BRAM_Controller}] $m
    connect_bd_intf_net [get_bd_intf_pins $name/BRAM_PORTA] \
                        [get_bd_intf_pins ${name}_mem/BRAM_PORTA]
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/s_axi_aclk]
    connect_bd_net [get_bd_pins $rstpin] [get_bd_pins $name/s_axi_aresetn]
}

# Port 0,1 on the station's own clock, 2 on DDR, 3+ on ctrl -- matching
# sb_line4's port_dom(), which is what the wrapper's ASSOCIATED_BUSIF declares.
for {set slr 0} {$slr < 4} {incr slr} {
    for {set p 0} {$p < $NQ} {incr p} {
        set k [expr {$slr * $NQ + $p}]
        set dw [expr {$p == 0 ? 256 : 32}]
        if {$p < 2} {
            set ck clk_wiz_s$slr/clk_out1 ; set rk rst_s$slr/peripheral_aresetn
        } elseif {$p == 2} {
            set ck clk_wiz_ddr/clk_out1   ; set rk rst_ddr/peripheral_aresetn
        } else {
            set ck clk_wiz_ctrl/clk_out1  ; set rk rst_ctrl/peripheral_aresetn
        }
        bram_ep ep$k $dw $ck $rk
        connect_bd_intf_net [get_bd_intf_pins station_bus/[format M%02d_AXI $k]] \
                            [get_bd_intf_pins ep$k/S_AXI]
    }
}

# The station decodes in RTL, so IPI has no segment to place and would report
# every endpoint unassigned; exclude them or a real warning hides in the noise.
# RTL decodes, but IPI needs the same map or the address editor and the
# hardware disagree. sb_line4 adr(): station at AW-4, port at 16, 64 KB each.
set nasg 0
for {set k 0} {$k < [expr {4 * $NQ}]} {incr k} {
    set s   [expr {$k / $NQ}]
    set p   [expr {$k % $NQ}]
    set off [expr {($s << ($AW - 4)) | ($p << 16)}]
    set sp  [get_bd_addr_spaces -quiet station_bus/M[format %02d $k]_AXI]
    set seg [get_bd_addr_segs -quiet ep$k/S_AXI/Mem0]
    if {$sp eq "" || $seg eq ""} {
        puts "@@@ addr MISSING ep$k sp='$sp' seg='$seg'"
        continue
    }
    if {[catch {assign_bd_address -offset $off -range 64K \
                    -target_address_space $sp $seg -force} m]} {
        puts "@@@ addr FAIL ep$k @ $off : $m"
    } else {
        incr nasg
    }
}
puts "@@@ addr assigned $nasg of [expr {4 * $NQ}]"

# Each master sees the whole 2^AW map; the station decides where inside it.
foreach {mst sif} {jtag_ctrl S00 jtag_wide S01 jtag_lite S02} {
    set sp  [get_bd_addr_spaces -quiet $mst/Data]
    set seg [get_bd_addr_segs -quiet station_bus/${sif}_AXI/reg0]
    if {[catch {assign_bd_address -offset 0 \
                    -range [expr {1 << $AW}] \
                    -target_address_space $sp $seg -force} m]} {
        puts "@@@ addr FAIL $mst -> $sif : $m"
    }
}

regenerate_bd_layout
validate_bd_design
save_bd_design

# ---- constraints ---------------------------------------------------------
set xdc $root/build/${design_name}_pblocks.xdc
file mkdir $root/build
set fh [open $xdc w]
puts $fh "# GENERATED by scripts/tcl/v6_bd.tcl"
foreach slr {0 1 2 3} {
    lassign [dict get $SLR_ROWS $slr] ylo yhi
    puts $fh "create_pblock pb_slr$slr"
    puts $fh "resize_pblock \[get_pblocks pb_slr$slr\] -add \{CLOCKREGION_X0${ylo}:CLOCKREGION_X7${yhi}\}"
    # The stations pin; g_link is deliberately left out -- its pipeline
    # registers ARE the die crossing and must be free to place across it.
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet \{${design_name}_i/station_bus/inst/u_line/g_stn\[$slr\].*\}\]"
    for {set p 0} {$p < $NQ} {incr p} {
        set k [expr {$slr * $NQ + $p}]
        puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet \{${design_name}_i/ep$k\}\]"
        puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet \{${design_name}_i/ep${k}_mem\}\]"
    }
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet \{${design_name}_i/clk_wiz_s$slr\}\]"
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet \{${design_name}_i/clk_wiz_bus$slr\}\]"
    puts $fh "set_property CONTAIN_ROUTING false \[get_pblocks pb_slr$slr\]"
}
foreach c {jtag_ctrl jtag_wide jtag_lite dwc_wide clk_wiz_ctrl rst_ctrl \
           rst_xdma util_ds_buf_bufg} {
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr1\] \[get_cells -quiet \{${design_name}_i/$c\}\]"
}
close $fh
add_files -fileset constrs_1 -norecurse $xdc

set clk $root/build/${design_name}_clocks.xdc
set fh [open $clk w]
# WITHOUT THIS NOTHING IS TIMED: no board part and no DDR4 IP here, so nothing
# else defines the reference and no MMCM output clock ever derives.
puts $fh "create_clock -name sys_clk -period 10.000 \[get_ports system_clk_clk_p\]"
# ANY_CMT_COLUMN, not BACKBONE: on UltraScale BACKBONE means SAME_CMT_COLUMN.
# Named by a LOAD pin -- the auto-inserted BUFG has no name until opt_design.
puts $fh "set_property CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN \[get_nets -of \[get_pins ${design_name}_i/clk_wiz_ctrl/clk_in1\]\]"
# One group per MMCM. `get_pins -hier -filter NAME` resolves EMPTY here, which
# makes set_clock_groups a silent no-op -- hence literal paths and the check.
set wizpins {}
dict set wizpins clk_wiz_ctrl {clk_out1 clk_out2}
foreach w {clk_wiz_ddr clk_wiz_bus0 clk_wiz_bus1 clk_wiz_bus2 clk_wiz_bus3 \
           clk_wiz_s0 clk_wiz_s1 clk_wiz_s2 clk_wiz_s3} {
    dict set wizpins $w {clk_out1}
}
set grp {}
foreach w [dict keys $wizpins] {
    set pins {}
    foreach o [dict get $wizpins $w] { lappend pins "${design_name}_i/$w/$o" }
    puts $fh "if {\[llength \[get_clocks -quiet -of_objects \[get_pins -quiet \{$pins\}\]\]\] == 0} {"
    puts $fh "    error \"v6 clocks: $w resolved to no clock -- set_clock_groups would be a no-op\""
    puts $fh "}"
    append grp " \\\n    -group \[get_clocks -include_generated_clocks -of_objects \[get_pins \{$pins\}\]\]"
}
puts $fh "set_clock_groups -asynchronous$grp"
close $fh
add_files -fileset constrs_1 -norecurse $clk

set pins $root/build/${design_name}_pins.xdc
set fh [open $pins w]
puts $fh "set_property CFGBVS GND \[current_design\]"
puts $fh "set_property CONFIG_VOLTAGE 1.8 \[current_design\]"
puts $fh "set_property BITSTREAM.GENERAL.COMPRESS true \[current_design\]"
puts $fh "set_property IOSTANDARD DIFF_SSTL12 \[get_ports system_clk_clk_n\]"
puts $fh "set_property IOSTANDARD DIFF_SSTL12 \[get_ports system_clk_clk_p\]"
puts $fh "set_property PACKAGE_PIN AY23 \[get_ports system_clk_clk_p\]"
close $fh
add_files -fileset constrs_1 -norecurse $pins

set bdf [get_files ${design_name}.bd]
make_wrapper -files $bdf -top -import -force
set_property top ${design_name}_wrapper [current_fileset]
update_compile_order -fileset sources_1

puts "@@@ v6_bd: project at $proj_dir"
if {!$do_impl} {
    puts "@@@ built the design only -- pass -tclargs impl to run synth and impl"
    exit 0
}

launch_runs synth_1 -jobs 8
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synthesis failed: see $proj_dir/${design_name}.runs/synth_1/runme.log"
}
puts "@@@ synth log $proj_dir/${design_name}.runs/synth_1/runme.log"

launch_runs impl_1 -to_step write_bitstream -jobs 8
wait_on_run impl_1
puts "@@@ impl log $proj_dir/${design_name}.runs/impl_1/runme.log"
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    error "implementation failed: see the impl log above"
}

open_run impl_1
set clks [get_clocks -quiet]
puts "@@@ clocks: [llength $clks]"
if {[llength $clks] < 10} {
    error "only [llength $clks] clocks -- routed UNTIMED, any WNS is meaningless"
}
source [file join $root scripts tcl ooc_class.tcl]
ooc_classify 2000
puts "@@@ WNS [get_property SLACK [get_timing_paths -max_paths 1 -setup]]"
report_utilization -slr -file $root/build/${design_name}_util.rpt
puts "@@@ utilization $root/build/${design_name}_util.rpt"
puts "@@@ v6_bd done"

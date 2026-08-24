# Four station blocks, one per SLR, joined by three credited links, driven by
# JTAG masters into per-SLR BRAM endpoints. Its own project: a validation build.
#
#   vivado -mode batch -source scripts/tcl/station_bus_bd.tcl -tclargs impl

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set design_name sbval
set proj_dir C:/Users/apoll/Desktop/vivado/station_bus_val

set do_impl [expr {[lsearch $argv impl] >= 0}]

# The knob the whole design trades against. 250 leaves margin over the 303 MHz
# the four-station OOC reported; raise it once a run closes.
set BUS_MHZ  250.0
set CTRL_MHZ 100.0
set XDMA_MHZ 250.0
# One local clock per SLR, deliberately unrelated to each other and to the bus.
set SLR_MHZ  {0 237.000 1 300.000 2 180.000 3 210.000}
set SLR_ROWS {0 {Y0 Y3} 1 {Y4 Y7} 2 {Y8 Y11} 3 {Y12 Y15}}
# Link k reaches this SLR; the order must match sb_bd_root's segment table.
set LINK_SLR {0 2 3}

set_param general.maxThreads 8

# ---- project -------------------------------------------------------------
create_project -force $design_name $proj_dir -part $part

set common [file join $root src kohakuaccel common]
set stn    [file join $root src kohakuaccel axi station]

add_files -norecurse [list \
    [file join $common sync_fifo.v] \
    [file join $common async_fifo.v] \
    [file join $common sb_skid.v] \
    [file join $stn sb_hub.v] \
    [file join $stn sb_station.v] \
    [file join $stn sb_nmu.v] \
    [file join $stn sb_nsu.v] \
    [file join $root src kohakuaccel axi link sb_link.v] \
    [file join $root src kohakuaccel axi topo sb_stn_root.v] \
    [file join $root src kohakuaccel axi topo sb_stn_leaf.v] \
    [file join $root src kohakuaccel axi bd sb_bd_root.v] \
    [file join $root src kohakuaccel axi bd sb_bd_leaf.v] \
    [file join $root src kohakuaccel axi bd sb_bd_link.v]]

update_compile_order -fileset sources_1

create_bd_design $design_name
current_bd_design $design_name

# ---- boundary ------------------------------------------------------------
# One differential clock and nothing else: every master is JTAG.
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
set clkin [get_bd_pins util_ds_buf_bufg/BUFG_O]

# ---- clocks --------------------------------------------------------------
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
    connect_bd_net [get_bd_pins $name/clk_in1] [get_bd_pins util_ds_buf_bufg/BUFG_O]
    return $w
}

wiz clk_wiz_bus  $BUS_MHZ
wiz clk_wiz_ctrl $CTRL_MHZ $XDMA_MHZ
foreach slr {0 1 2 3} { wiz clk_wiz_s$slr [dict get $SLR_MHZ $slr] }

# A reset generator per domain: a station's local reset must be released in its
# own clock, not synchronised from the bus by hand.
proc psr {name clkpin} {
    set r [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset $name]
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/slowest_sync_clk]
    connect_bd_net [get_bd_pins $name/ext_reset_in] \
                   [get_bd_pins clk_wiz_bus/locked]
    return $r
}

psr rst_bus  clk_wiz_bus/clk_out1
psr rst_ctrl clk_wiz_ctrl/clk_out1
psr rst_xdma clk_wiz_ctrl/clk_out2
foreach slr {0 1 2 3} { psr rst_s$slr clk_wiz_s$slr/clk_out1 }

# The stations take an ACTIVE HIGH bus reset; proc_sys_reset gives active low.
set inv [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic bus_rst_inv]
set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] $inv
connect_bd_net [get_bd_pins rst_bus/peripheral_aresetn] [get_bd_pins bus_rst_inv/Op1]

# ---- stations ------------------------------------------------------------
set rootc [create_bd_cell -type module -reference sb_bd_root stn_slr1]
foreach slr $LINK_SLR {
    create_bd_cell -type module -reference sb_bd_leaf stn_slr$slr
    create_bd_cell -type module -reference sb_bd_link link_slr$slr
}

set busclk [get_bd_pins clk_wiz_bus/clk_out1]
set busrst [get_bd_pins bus_rst_inv/Res]

connect_bd_net $busclk [get_bd_pins stn_slr1/bus_clk]
connect_bd_net $busrst [get_bd_pins stn_slr1/bus_rst]
connect_bd_net [get_bd_pins clk_wiz_ctrl/clk_out1] [get_bd_pins stn_slr1/clk_ctrl]
connect_bd_net [get_bd_pins rst_ctrl/peripheral_aresetn] \
               [get_bd_pins stn_slr1/aresetn_ctrl]
connect_bd_net [get_bd_pins clk_wiz_ctrl/clk_out2] [get_bd_pins stn_slr1/clk_xdma]
connect_bd_net [get_bd_pins rst_xdma/peripheral_aresetn] \
               [get_bd_pins stn_slr1/aresetn_xdma]
connect_bd_net [get_bd_pins clk_wiz_s1/clk_out1] [get_bd_pins stn_slr1/clk_loc]
connect_bd_net [get_bd_pins rst_s1/peripheral_aresetn] \
               [get_bd_pins stn_slr1/aresetn_loc]

# Link k: root REQ out -> link -> leaf REQ in, leaf RSP out -> link -> root.
set k 0
foreach slr $LINK_SLR {
    connect_bd_net $busclk [get_bd_pins link_slr$slr/bus_clk]
    connect_bd_net $busrst [get_bd_pins link_slr$slr/bus_rst]
    connect_bd_net $busclk [get_bd_pins stn_slr$slr/bus_clk]
    connect_bd_net $busrst [get_bd_pins stn_slr$slr/bus_rst]
    connect_bd_net [get_bd_pins clk_wiz_s$slr/clk_out1] \
                   [get_bd_pins stn_slr$slr/clk_loc]
    connect_bd_net [get_bd_pins rst_s$slr/peripheral_aresetn] \
                   [get_bd_pins stn_slr$slr/aresetn_loc]

    connect_bd_intf_net [get_bd_intf_pins stn_slr1/L${k}_REQ] \
                        [get_bd_intf_pins link_slr$slr/S_REQ]
    connect_bd_intf_net [get_bd_intf_pins link_slr$slr/M_REQ] \
                        [get_bd_intf_pins stn_slr$slr/L_REQ]
    connect_bd_intf_net [get_bd_intf_pins stn_slr$slr/L_RSP] \
                        [get_bd_intf_pins link_slr$slr/S_RSP]
    connect_bd_intf_net [get_bd_intf_pins link_slr$slr/M_RSP] \
                        [get_bd_intf_pins stn_slr1/L${k}_RSP]
    incr k
}

# ---- managers ------------------------------------------------------------
# The wide port gets a width converter: jtag_axi is 32-bit only.
proc jtag_master {name clkpin rstpin} {
    set j [create_bd_cell -type ip -vlnv xilinx.com:ip:jtag_axi $name]
    # PROTOCOL 0 is AXI4; the default 2 is AXI4-Lite and will not connect.
    set_property -dict [list CONFIG.PROTOCOL {0} CONFIG.M_AXI_ID_WIDTH {4}] $j
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/aclk]
    connect_bd_net [get_bd_pins $rstpin] [get_bd_pins $name/aresetn]
    return $j
}

jtag_master jtag_ctrl clk_wiz_ctrl/clk_out1 rst_ctrl/peripheral_aresetn
jtag_master jtag_wide clk_wiz_ctrl/clk_out2 rst_xdma/peripheral_aresetn
jtag_master jtag_lite clk_wiz_ctrl/clk_out2 rst_xdma/peripheral_aresetn

set dwc [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dwidth_converter dwc_wide]
set_property -dict [list CONFIG.SI_DATA_WIDTH {32} CONFIG.MI_DATA_WIDTH {512}] $dwc
connect_bd_net [get_bd_pins clk_wiz_ctrl/clk_out2] [get_bd_pins dwc_wide/s_axi_aclk]
connect_bd_net [get_bd_pins rst_xdma/peripheral_aresetn] \
               [get_bd_pins dwc_wide/s_axi_aresetn]

connect_bd_intf_net [get_bd_intf_pins jtag_ctrl/M_AXI] [get_bd_intf_pins stn_slr1/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins jtag_wide/M_AXI] [get_bd_intf_pins dwc_wide/S_AXI]
connect_bd_intf_net [get_bd_intf_pins dwc_wide/M_AXI]  [get_bd_intf_pins stn_slr1/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins jtag_lite/M_AXI] [get_bd_intf_pins stn_slr1/S02_AXI]

# ---- endpoints -----------------------------------------------------------
# Two per SLR, matching sb_bd_root's map: port 0 is 512-bit, port 1 is 32-bit.
proc bram_ep {name dw clkpin rstpin} {
    set c [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_bram_ctrl $name]
    set_property -dict [list CONFIG.DATA_WIDTH $dw CONFIG.SINGLE_PORT_BRAM {1} \
                             CONFIG.ECC_TYPE {0} CONFIG.MEM_DEPTH {512}] $c
    # Geometry comes from the controller: setting port B by hand collides with
    # what propagation then derives, and the IP rejects the pair.
    set m [create_bd_cell -type ip -vlnv xilinx.com:ip:blk_mem_gen ${name}_mem]
    set_property -dict [list CONFIG.Memory_Type {Single_Port_RAM} \
                             CONFIG.use_bram_block {BRAM_Controller}] $m
    connect_bd_intf_net [get_bd_intf_pins $name/BRAM_PORTA] \
                        [get_bd_intf_pins ${name}_mem/BRAM_PORTA]
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins $name/s_axi_aclk]
    connect_bd_net [get_bd_pins $rstpin] [get_bd_pins $name/s_axi_aresetn]
    return $c
}

foreach slr {0 1 2 3} {
    set stn [expr {$slr == 1 ? "stn_slr1" : "stn_slr$slr"}]
    bram_ep ep${slr}_wide 512 clk_wiz_s$slr/clk_out1 rst_s$slr/peripheral_aresetn
    bram_ep ep${slr}_lite 32  clk_wiz_s$slr/clk_out1 rst_s$slr/peripheral_aresetn
    connect_bd_intf_net [get_bd_intf_pins $stn/M00_AXI] \
                        [get_bd_intf_pins ep${slr}_wide/S_AXI]
    connect_bd_intf_net [get_bd_intf_pins $stn/M01_AXI] \
                        [get_bd_intf_pins ep${slr}_lite/S_AXI]
}

# The stations decode in RTL, so IPI has no segment to place and reports every
# endpoint as unassigned. Exclude them, or a real warning hides in the noise.
foreach sp [get_bd_addr_spaces] {
    foreach seg [get_bd_addr_segs -addressables -quiet -of_objects $sp] {
        catch {exclude_bd_addr_seg -target_address_space $sp $seg}
    }
}

regenerate_bd_layout
validate_bd_design
save_bd_design

# ---- constraints ---------------------------------------------------------
# Links are NOT pinned: their pipe registers ARE the die crossing.
set xdc $root/build/${design_name}_pblocks.xdc
file mkdir $root/build
set fh [open $xdc w]
puts $fh "# GENERATED by scripts/tcl/station_bus_bd.tcl"
foreach slr {0 1 2 3} {
    lassign [dict get $SLR_ROWS $slr] ylo yhi
    puts $fh "create_pblock pb_slr$slr"
    puts $fh "resize_pblock \[get_pblocks pb_slr$slr\] -add \{CLOCKREGION_X0${ylo}:CLOCKREGION_X7${yhi}\}"
    foreach c [list stn_slr$slr ep${slr}_wide ep${slr}_wide_mem \
                    ep${slr}_lite ep${slr}_lite_mem clk_wiz_s$slr rst_s$slr] {
        puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet {${design_name}_i/$c}\]"
    }
    puts $fh "set_property CONTAIN_ROUTING false \[get_pblocks pb_slr$slr\]"
}
foreach c {jtag_ctrl jtag_wide jtag_lite dwc_wide clk_wiz_ctrl rst_ctrl \
           rst_xdma clk_wiz_bus rst_bus util_ds_buf_bufg} {
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr1\] \[get_cells -quiet {${design_name}_i/$c}\]"
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
puts $fh "set_property CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN \[get_nets -of \[get_pins ${design_name}_i/clk_wiz_bus/clk_in1\]\]"
set grp {}
foreach w {clk_wiz_bus clk_wiz_ctrl clk_wiz_s0 clk_wiz_s1 clk_wiz_s2 clk_wiz_s3} {
    append grp " \\\n    -group \[get_clocks -include_generated_clocks -of_objects \[get_pins -hier -filter {NAME =~ *${w}*clk_out*}\]\]"
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

# ---- wrapper and runs ----------------------------------------------------
set bdf [get_files ${design_name}.bd]
make_wrapper -files $bdf -top -import -force
set_property top ${design_name}_wrapper [current_fileset]
update_compile_order -fileset sources_1

puts "@@@ station_bus_bd: project at $proj_dir"
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

# A design with no clocks routes clean and reports enormous slack. Refuse to
# print a number until the clocks that should exist do.
set clks [get_clocks -quiet]
puts "@@@ clocks: [llength $clks] -- [lsort [get_property NAME $clks]]"
if {[llength $clks] < 7} {
    error "only [llength $clks] clocks constrained -- the design was routed UNTIMED and any WNS from it is meaningless"
}

source [file join $root scripts tcl ooc_class.tcl]
ooc_classify 2000
puts "@@@ WNS [get_property SLACK [get_timing_paths -max_paths 1 -setup]]"
report_utilization -slr -file $root/build/${design_name}_util.rpt
puts "@@@ utilization $root/build/${design_name}_util.rpt"
puts "@@@ station_bus_bd done"

# v5 clocking WITHOUT the mesh: does the placer accept 5 MMCMs over SLR0..SLR3
# from ONE IO-driven buffer, given CLOCK_DEDICATED_ROUTE? Minutes, not an hour.

set part     xcvu13p-fhgb2104-2L-e
set proj_dir C:/Users/apoll/Desktop/vivado/clock_demo
set root     [file normalize [file dirname [info script]]/../..]

create_project -force clock_demo $proj_dir -part $part
set_property target_language Verilog [current_project]
add_files -norecurse -fileset sources_1 $root/src/synth_top/ktpu_div2.v

create_bd_design demo
current_bd_design demo

# ---- clock input -----------------------------------------------------------
create_bd_intf_port -mode Slave \
    -vlnv xilinx.com:interface:diff_clock_rtl:1.0 system
set_property CONFIG.FREQ_HZ 100000000 [get_bd_intf_ports system]

create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_sys
set_property CONFIG.C_BUF_TYPE {IBUFDS} [get_bd_cells util_ds_buf_sys]
connect_bd_intf_net [get_bd_intf_ports system] \
                    [get_bd_intf_pins util_ds_buf_sys/CLK_IN_D]

# THE POINT OF THE TEST. Vivado's auto-inserted buffer exists only from
# opt_design, so no xdc can name it; an explicit one is nameable at parse time.
create_bd_cell -type ip -vlnv xilinx.com:ip:util_ds_buf util_ds_buf_bufg
set_property CONFIG.C_BUF_TYPE {BUFG} [get_bd_cells util_ds_buf_bufg]
connect_bd_net [get_bd_pins util_ds_buf_sys/IBUF_OUT] \
               [get_bd_pins util_ds_buf_bufg/BUFG_I]

# ---- five generators -------------------------------------------------------
# Four outputs each: noc, mag, vec, and the 2x whose /2 becomes mat_clk.
proc mesh_wiz {name} {
    create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz $name
    set_property -dict [list \
        CONFIG.PRIM_IN_FREQ {100.000} \
        CONFIG.CLKOUT1_USED {true} CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {300.000} \
        CONFIG.CLKOUT2_USED {true} CONFIG.CLKOUT2_REQUESTED_OUT_FREQ {600.000} \
        CONFIG.CLKOUT3_USED {true} CONFIG.CLKOUT3_REQUESTED_OUT_FREQ {250.000} \
        CONFIG.CLKOUT4_USED {true} CONFIG.CLKOUT4_REQUESTED_OUT_FREQ {200.000} \
        CONFIG.USE_LOCKED {true} CONFIG.USE_RESET {false}] [get_bd_cells $name]
}

create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz clk_wiz_ctrl
set_property -dict [list CONFIG.PRIM_IN_FREQ {100.000} \
    CONFIG.CLKOUT1_USED {true} CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {100.000} \
    CONFIG.USE_LOCKED {true} CONFIG.USE_RESET {false}] [get_bd_cells clk_wiz_ctrl]

set WIZ {clk_wiz_ctrl}
foreach i {0 1 2 3} { mesh_wiz clk_wiz_mesh_$i; lappend WIZ clk_wiz_mesh_$i }

set clkin {}
foreach w $WIZ { lappend clkin [get_bd_pins $w/clk_in1] }
connect_bd_net [get_bd_pins util_ds_buf_bufg/BUFG_O] {*}$clkin

# ---- the pumped 1x ---------------------------------------------------------
# One is enough to prove the structure places: BUFGCE_DIV(2) of the 600 MHz,
# CLR released by lock, exactly as a mesh derives mat_clk.
create_bd_cell -type module -reference ktpu_div2 div2_mesh_3
connect_bd_net [get_bd_pins clk_wiz_mesh_3/clk_out2] [get_bd_pins div2_mesh_3/clk2x]
set_property CONFIG.FREQ_HZ 300000000 [get_bd_pins div2_mesh_3/clk1x]
create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic dclr
set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] [get_bd_cells dclr]
connect_bd_net [get_bd_pins clk_wiz_mesh_3/locked] [get_bd_pins dclr/Op1]
connect_bd_net [get_bd_pins dclr/Res] [get_bd_pins div2_mesh_3/clr]

# ---- resets ----------------------------------------------------------------
# One per domain: a reset released on the wrong clock passes place and fails on
# hardware.
set DOM {
    {ctrl  clk_wiz_ctrl/clk_out1    clk_wiz_ctrl/locked}
    {ram0  clk_wiz_mesh_0/clk_out1  clk_wiz_mesh_0/locked}
    {ram1  clk_wiz_mesh_1/clk_out3  clk_wiz_mesh_1/locked}
    {ram2  clk_wiz_mesh_2/clk_out4  clk_wiz_mesh_2/locked}
    {ram3  div2_mesh_3/clk1x        clk_wiz_mesh_3/locked}
}
foreach d $DOM {
    lassign $d nm clkpin lockpin
    create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_$nm
    connect_bd_net [get_bd_pins $clkpin]  [get_bd_pins rst_$nm/slowest_sync_clk]
    connect_bd_net [get_bd_pins $lockpin] [get_bd_pins rst_$nm/dcm_locked]
    connect_bd_net [get_bd_pins $lockpin] [get_bd_pins rst_$nm/ext_reset_in]
}

# ---- master and fabric -----------------------------------------------------
create_bd_cell -type ip -vlnv xilinx.com:ip:jtag_axi jtag_axi_0
set_property CONFIG.PROTOCOL {2} [get_bd_cells jtag_axi_0]
connect_bd_net [get_bd_pins clk_wiz_ctrl/clk_out1] [get_bd_pins jtag_axi_0/aclk]
connect_bd_net [get_bd_pins rst_ctrl/peripheral_aresetn] [get_bd_pins jtag_axi_0/aresetn]

create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect smc
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {5} \
                         CONFIG.NUM_CLKS {5}] [get_bd_cells smc]
connect_bd_intf_net [get_bd_intf_pins jtag_axi_0/M_AXI] [get_bd_intf_pins smc/S00_AXI]
connect_bd_net [get_bd_pins clk_wiz_ctrl/clk_out1] [get_bd_pins smc/aclk]
connect_bd_net [get_bd_pins rst_ctrl/peripheral_aresetn] [get_bd_pins smc/aresetn]

# ---- five RAMs, five clocks, one per SLR -----------------------------------
# BRAM and URAM alternate so both primitive kinds are exercised on their own
# clock -- a URAM has a different placement rule than a block RAM.
set RAM {
    {0 ram0 clk_wiz_mesh_0/clk_out1 aclk1 bram 0}
    {1 ram1 clk_wiz_mesh_1/clk_out3 aclk2 uram 1}
    {2 ram2 clk_wiz_mesh_2/clk_out4 aclk3 bram 2}
    {3 ram3 div2_mesh_3/clk1x       aclk4 uram 3}
    {4 ram4 clk_wiz_ctrl/clk_out1   aclk  bram 1}
}
foreach r $RAM {
    lassign $r mi nm clkpin smcclk kind slr
    set rstsrc [expr {$nm eq "ram4" ? "rst_ctrl" : "rst_$nm"}]
    create_bd_cell -type ip -vlnv xilinx.com:ip:axi_bram_ctrl ${nm}_ctrl
    # Widths and depth come from PROPAGATION. Setting them here fights the
    # controller and fails validation on the port-B ratio.
    set_property -dict [list CONFIG.SINGLE_PORT_BRAM {1}] [get_bd_cells ${nm}_ctrl]
    create_bd_cell -type ip -vlnv xilinx.com:ip:blk_mem_gen ${nm}_mem
    set_property -dict [list CONFIG.Memory_Type {Single_Port_RAM} \
        CONFIG.PRIM_type_to_Implement [expr {$kind eq "uram" ? "URAM" : "BRAM"}] \
        CONFIG.use_bram_block {BRAM_Controller}] [get_bd_cells ${nm}_mem]
    connect_bd_intf_net [get_bd_intf_pins ${nm}_ctrl/BRAM_PORTA] \
                        [get_bd_intf_pins ${nm}_mem/BRAM_PORTA]
    connect_bd_intf_net [get_bd_intf_pins smc/M0${mi}_AXI] \
                        [get_bd_intf_pins ${nm}_ctrl/S_AXI]
    connect_bd_net [get_bd_pins $clkpin] [get_bd_pins ${nm}_ctrl/s_axi_aclk]
    connect_bd_net [get_bd_pins $rstsrc/peripheral_aresetn] \
                   [get_bd_pins ${nm}_ctrl/s_axi_aresetn]
    if {$smcclk ne "aclk"} { connect_bd_net [get_bd_pins $clkpin] [get_bd_pins smc/$smcclk] }
}

assign_bd_address
validate_bd_design
save_bd_design

set bdf [get_files demo.bd]
generate_target all $bdf
make_wrapper -files $bdf -top -import -force
set_property top demo_wrapper [current_fileset]
update_compile_order -fileset sources_1

# ---- constraints -----------------------------------------------------------
set xdc $proj_dir/demo.xdc
set fh [open $xdc w]
puts $fh "set_property IOSTANDARD DIFF_SSTL12 \[get_ports system_clk_n\]"
puts $fh "set_property IOSTANDARD DIFF_SSTL12 \[get_ports system_clk_p\]"
puts $fh "set_property PACKAGE_PIN AY23 \[get_ports system_clk_p\]"
puts $fh "create_clock -period 10.000 -name sys_clk_100 \[get_ports system_clk_p\]"
puts $fh ""
puts $fh "# The rule under test: one IO-driven buffer, five MMCMs, four SLRs."
puts $fh "set_property CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN \\"
puts $fh "    \[get_nets -of \[get_pins demo_i/clk_wiz_ctrl/clk_in1\]\]"
puts $fh ""
puts $fh "set_clock_groups -asynchronous \\"
puts $fh "    -group \[get_clocks -include_generated_clocks -of_objects \[get_pins -hier -filter {NAME =~ *clk_wiz_ctrl*clk_out1}\]\] \\"
foreach i {0 1 2 3} {
    puts $fh "    -group \[get_clocks -include_generated_clocks -of_objects \[get_pins -hier -filter {NAME =~ *clk_wiz_mesh_$i*clk_out*}\]\] \\"
}
puts $fh ""
set SLRROW {0 {Y0 Y3} 1 {Y4 Y7} 2 {Y8 Y11} 3 {Y12 Y15}}
foreach {slr rows} $SLRROW {
    lassign $rows lo hi
    puts $fh "create_pblock pb_slr$slr"
    puts $fh "resize_pblock \[get_pblocks pb_slr$slr\] -add {CLOCKREGION_X0${lo}:CLOCKREGION_X7${hi}}"
    puts $fh "set_property CONTAIN_ROUTING false \[get_pblocks pb_slr$slr\]"
}
foreach r $RAM {
    lassign $r mi nm clkpin smcclk kind slr
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$slr\] \[get_cells -quiet {demo_i/${nm}_ctrl demo_i/${nm}_mem}\]"
}
foreach i {0 1 2 3} {
    puts $fh "add_cells_to_pblock \[get_pblocks pb_slr$i\] \[get_cells -quiet {demo_i/clk_wiz_mesh_$i}\]"
}
puts $fh "add_cells_to_pblock \[get_pblocks pb_slr3\] \[get_cells -quiet {demo_i/div2_mesh_3}\]"
close $fh
add_files -fileset constrs_1 -norecurse $xdc
set_property PROCESSING_ORDER LATE [get_files -of_objects [get_filesets constrs_1] */demo.xdc]

puts "@@@ demo built: 5 MMCMs, 5 clocks, 5 AXI RAMs, pblocked SLR0..3"

launch_runs synth_1 -jobs 16
wait_on_run synth_1
puts "@@@ synth: [get_property STATUS [get_runs synth_1]]"
launch_runs impl_1 -jobs 16
wait_on_run impl_1
puts "@@@ impl: [get_property STATUS [get_runs impl_1]]  PROGRESS [get_property PROGRESS [get_runs impl_1]]"

# XDMA channel-count sweep, OOC: v5's config with only the channels varied.
#   vivado -mode batch -source scripts/tcl/ooc_xdma.tcl -tclargs <rd> <wr>

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set rd [lindex $argv 0]
set wr [lindex $argv 1]
if {$rd eq ""} { set rd 4 }
if {$wr eq ""} { set wr 4 }

set_param general.maxThreads 8
create_project -force -in_memory -part $part

# Copied verbatim from multimesh_v5_bd.tcl:233-244 -- a config that differs
# anywhere else makes the comparison against the 76,319 baseline meaningless.
create_ip -vlnv xilinx.com:ip:xdma -module_name xdma_sweep -dir $root/build/xdma_sweep
set_property -dict [list \
  CONFIG.axi_data_width {512_bit} CONFIG.axi_id_width {4} \
  CONFIG.axilite_master_en {true} CONFIG.axilite_master_scale {Megabytes} \
  CONFIG.axilite_master_size {16} CONFIG.axisten_freq {250} \
  CONFIG.functional_mode {DMA} CONFIG.mode_selection {Basic} \
  CONFIG.pcie_blk_locn {X0Y1} CONFIG.pf0_device_id {903F} \
  CONFIG.pf0_subsystem_id {0007} CONFIG.pf0_subsystem_vendor_id {10EE} \
  CONFIG.pl_link_cap_max_link_speed {8.0_GT/s} \
  CONFIG.pl_link_cap_max_link_width {X16} CONFIG.ref_clk_freq {100_MHz} \
  CONFIG.xdma_num_usr_irq {1} CONFIG.xdma_rnum_chnl $rd \
  CONFIG.xdma_wnum_chnl $wr \
] [get_ips xdma_sweep]

puts "@@@ xdma sweep rd $rd wr $wr"
synth_ip [get_ips xdma_sweep]

open_run -name netlist_1 [get_runs -quiet *] -quiet
link_design -name lnk -part $part
puts "@@@ ============================ rd $rd wr $wr"
source [file join $root scripts tcl ooc_class.tcl]
ooc_count TOTAL
ooc_util
puts "@@@ xdma sweep done rd $rd wr $wr"

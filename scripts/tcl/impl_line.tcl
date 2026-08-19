# Place and route the line with per-SLR pblocks, out of context -- no BD, no IO.
#   -tclargs <fw> <nq> <cdc> <halflink> <period_ns> <tag>

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set fw   [lindex $argv 0]
set nq   [lindex $argv 1]
set cdc  [lindex $argv 2]
set half [lindex $argv 3]
set per  [lindex $argv 4]
set tag  [lindex $argv 5]
if {$fw   eq ""} { set fw   256 }
if {$nq   eq ""} { set nq   4 }
if {$cdc  eq ""} { set cdc  1 }
if {$half eq ""} { set half 0 }
if {$per  eq ""} { set per  5.000 }
if {$tag  eq ""} { set tag  v6 }

set portw 1
while {[expr {1 << $portw}] < $nq} { incr portw }

set_param general.maxThreads 8

read_verilog [list \
    [file join $root src kohakuaccel common sync_fifo.v] \
    [file join $root src kohakuaccel common async_fifo.v] \
    [file join $root src kohakuaccel common sb_skid.v] \
    [file join $root src kohakuaccel axi station sb_hub.v] \
    [file join $root src kohakuaccel axi station sb_nmu.v] \
    [file join $root src kohakuaccel axi station sb_nsu.v] \
    [file join $root src kohakuaccel axi link sb_link.v] \
    [file join $root src kohakuaccel axi link sb_link_cdc.v] \
    [file join $root src kohakuaccel axi topo sb_stn_line.v] \
    [file join $root src kohakuaccel axi topo sb_line4.v]]

set xdc $root/build/impl_line_${tag}.xdc
file mkdir $root/build
set fh [open $xdc w]
foreach c {bus_clk0 bus_clk1 bus_clk2 bus_clk3} {
    puts $fh "create_clock -name $c -period $per \[get_ports $c\]"
}
puts $fh "create_clock -name clk_ctrl -period 10.000 \[get_ports clk_ctrl\]"
puts $fh "create_clock -name clk_xdma -period 4.000  \[get_ports clk_xdma\]"
puts $fh "create_clock -name clk_s0   -period 4.219  \[get_ports clk_s0\]"
puts $fh "create_clock -name clk_s1   -period 3.333  \[get_ports clk_s1\]"
puts $fh "create_clock -name clk_s2   -period 5.545  \[get_ports clk_s2\]"
puts $fh "create_clock -name clk_s3   -period 4.746  \[get_ports clk_s3\]"
# clk_ddr0..3, NOT clk_ddr: the scalar port went when port 2 became per-station,
# so this matched nothing and left all four DDR domains untimed and ungrouped.
foreach c {clk_ddr0 clk_ddr1 clk_ddr2 clk_ddr3} {
    puts $fh "create_clock -name $c -period 3.333 \[get_ports $c\]"
}
set grp {}
foreach c {bus_clk0 bus_clk1 bus_clk2 bus_clk3 clk_ctrl clk_xdma \
           clk_s0 clk_s1 clk_s2 clk_s3 \
           clk_ddr0 clk_ddr1 clk_ddr2 clk_ddr3} {
    append grp " -group \[get_clocks $c\]"
}
puts $fh "set_clock_groups -asynchronous$grp"
close $fh

puts "@@@ impl_line fw $fw nq $nq cdc $cdc half $half period $per tag $tag"

synth_design -top sb_line4 -part $part -mode out_of_context \
             -generic FW=$fw -generic NQ=$nq -generic LINK_CDC=$cdc \
             -generic LINK_FULL=$half -generic PORTW=$portw
read_xdc $xdc

# Station i to SLR i. The LINKS ARE DELIBERATELY UNPINNED: their pipeline
# registers are the die crossing and must be free to place across it.
set rows {0 {Y0 Y3} 1 {Y4 Y7} 2 {Y8 Y11} 3 {Y12 Y15}}
foreach slr {0 1 2 3} {
    lassign [dict get $rows $slr] ylo yhi
    create_pblock pb_slr$slr
    resize_pblock [get_pblocks pb_slr$slr] \
        -add "CLOCKREGION_X0${ylo}:CLOCKREGION_X7${yhi}"
    set cells [get_cells -quiet -hier -filter "NAME =~ g_stn\[$slr\].*"]
    if {[llength $cells]} {
        add_cells_to_pblock [get_pblocks pb_slr$slr] $cells
    } else {
        puts "@@@ WARNING pb_slr$slr matched no cells"
    }
    set_property CONTAIN_ROUTING false [get_pblocks pb_slr$slr]
}

opt_design
place_design
phys_opt_design
route_design

source [file join $root scripts tcl ooc_class.tcl]

puts "@@@ ============================ routed totals $tag"
ooc_count TOTAL
puts "@@@ ============================ vivado utilization $tag"
ooc_util
puts "@@@ ============================ control sets $tag"
ooc_ctrlsets

set clks [get_clocks -quiet]
puts "@@@ clocks: [llength $clks]"
if {[llength $clks] < 11} {
    error "only [llength $clks] clocks -- the design routed UNTIMED and any WNS is meaningless"
}

puts "@@@ ============================ Fmax $tag"
ooc_classify 2000
puts "@@@ WNS [get_property SLACK [get_timing_paths -max_paths 1 -setup]]"

report_utilization -file $root/build/impl_line_${tag}_util.rpt
puts "@@@ utilization $root/build/impl_line_${tag}_util.rpt"
puts "@@@ impl_line done $tag"

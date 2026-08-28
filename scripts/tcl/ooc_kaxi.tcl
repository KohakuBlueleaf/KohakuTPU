# OOC synth of kaxi_top (KohakuAXI M x N same-width xbar + per-home L3 cache).
#   -tclargs <M> <N_HOME> <data_w> <period_ns> <tag>
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set m   [lindex $argv 0]
set n   [lindex $argv 1]
set dw  [lindex $argv 2]
set per [lindex $argv 3]
set tag [lindex $argv 4]
if {$m   eq ""} { set m   4 }
if {$n   eq ""} { set n   4 }
if {$dw  eq ""} { set dw  256 }
if {$per eq ""} { set per 2.5 }
if {$tag eq ""} { set tag kaxi }

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakuaccel common sync_fifo.v] \
    [file join $root src kohakuaccel common async_fifo.v] \
    [file join $root src kohakuaccel axi simple axi_n1.v] \
    [file join $root src kohakuaxi kaxi_xbar.v] \
    [file join $root src kohakuaxi kaxi_l3.v] \
    [file join $root src kohakuaxi kaxi_top.v]]

set xdc $root/build/ooc_kaxi_${tag}.xdc
file mkdir $root/build
set fh [open $xdc w]
puts $fh "create_clock -name clk -period $per \[get_ports clk\]"
close $fh
read_xdc $xdc

synth_design -top kaxi_top -part $part -mode out_of_context \
             -flatten_hierarchy none -directive default \
             -generic M=$m -generic N_HOME=$n -generic DATA_W=$dw

ooc_record $tag "top=kaxi_top M=$m N=$n dw=$dw period=$per" 2000 2

puts "@@@ ============================ per instance $tag"
set insts {u_xbar}
for {set i 0} {$i < $n} {incr i} { lappend insts "g_l3\[$i\].u_l3" }
foreach inst $insts {
    if {[llength [get_cells -quiet $inst]] == 0} { continue }
    ooc_count $inst $inst
}
puts "@@@ ============================ Fmax $tag"
ooc_classify 2000
puts "@@@ kaxi done $tag"

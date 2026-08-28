# OOC synth of kaxi_top2 (kaxi_xbar2 + N kaxi_l3).  -tclargs <M> <N> <dw> <tag>
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set m   [lindex $argv 0]
set n   [lindex $argv 1]
set dw  [lindex $argv 2]
set tag [lindex $argv 3]
if {$m eq ""}  { set m 4 }
if {$n eq ""}  { set n 4 }
if {$dw eq ""} { set dw 256 }
if {$tag eq ""} { set tag kaxi2 }
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
read_verilog [file join $root src kohakuaxi kaxi_xbar2.v]
read_verilog [file join $root src kohakuaxi kaxi_l3.v]
read_verilog [file join $root src kohakuaxi kaxi_top2.v]
set xdc $root/build/ooc_kaxi2_${tag}.xdc
file mkdir $root/build
set fh [open $xdc w]
puts $fh "create_clock -name clk -period 2.5 \[get_ports clk\]"
close $fh
read_xdc $xdc
synth_design -top kaxi_top2 -part $part -mode out_of_context \
             -flatten_hierarchy none -generic M=$m -generic N_HOME=$n \
             -generic DATA_W=$dw
ooc_record $tag "top=kaxi_top2 M=$m N=$n dw=$dw" 2000 2
ooc_classify 2000
puts "@@@ kaxi2 done $tag"

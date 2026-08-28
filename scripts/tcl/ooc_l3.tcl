# OOC synth of ONE kaxi_l3 cache, standalone.  -tclargs <sets> <data_w> <tag>
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set sets [lindex $argv 0]
set dw   [lindex $argv 1]
set tag  [lindex $argv 2]
if {$sets eq ""} { set sets 512 }
if {$dw eq ""}   { set dw 512 }
if {$tag eq ""}  { set tag l3 }
set setw [expr {int(log($sets)/log(2)+0.5)}]
set llsb [expr {int(log($dw/8)/log(2)+0.5)}]
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
read_verilog [file join $root src kohakuaccel common kohaku_sdpram.v]
read_verilog [file join $root src kohakuaxi kaxi_l3.v]
set xdc $root/build/ooc_l3_${tag}.xdc
file mkdir $root/build
set fh [open $xdc w]
puts $fh "create_clock -name clk -period 2.5 \[get_ports clk\]"
close $fh
read_xdc $xdc
synth_design -top kaxi_l3 -part $part -mode out_of_context -flatten_hierarchy none \
    -generic DATA_W=$dw -generic SETS=$sets -generic SET_W=$setw -generic LINE_LSB=$llsb
ooc_record $tag "kaxi_l3 sets=$sets dw=$dw (=[expr {$sets*$dw/8}] bytes)" 2000 2
puts "@@@ l3 done $tag"

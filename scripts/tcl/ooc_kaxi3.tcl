# OOC synth of kaxi_top3 (kaxi_xbar3 + N URAM kaxi_l3). LUT is cache-depth-
# independent, so a shallow SETS synths fast; URAM count scales with depth.
#   -tclargs <M> <N> <dw> <sets> <ram> <tag>
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set m    [lindex $argv 0]
set n    [lindex $argv 1]
set dw   [lindex $argv 2]
set sets [lindex $argv 3]
set ram  [lindex $argv 4]
set tag  [lindex $argv 5]
if {$m eq ""}   { set m 4 }
if {$n eq ""}   { set n 4 }
if {$dw eq ""}  { set dw 512 }
if {$sets eq ""} { set sets 512 }
if {$ram eq ""} { set ram ultra }
if {$tag eq ""} { set tag kaxi3 }
set setw [expr {int(log($sets)/log(2)+0.5)}]
set llsb [expr {int(log($dw/8)/log(2)+0.5)}]
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
read_verilog [file join $root src kohakuaccel common kohaku_sdpram.v]
read_verilog [file join $root src kohakuaxi kaxi_xbar3.v]
read_verilog [file join $root src kohakuaxi kaxi_l3.v]
read_verilog [file join $root src kohakuaxi kaxi_top3.v]
set xdc $root/build/ooc_kaxi3_${tag}.xdc
file mkdir $root/build
set fh [open $xdc w]
puts $fh "create_clock -name clk -period 2.5 \[get_ports clk\]"
close $fh
read_xdc $xdc
synth_design -top kaxi_top3 -part $part -mode out_of_context -flatten_hierarchy none \
    -generic M=$m -generic N_HOME=$n -generic DATA_W=$dw \
    -generic L3_SETS=$sets -generic L3_SET_W=$setw -generic L3_LSB=$llsb \
    -generic L3_RAM=$ram
ooc_record $tag "kaxi_top3 M=$m N=$n dw=$dw sets=$sets ram=$ram" 2000 2
puts "@@@ kaxi3 done $tag"

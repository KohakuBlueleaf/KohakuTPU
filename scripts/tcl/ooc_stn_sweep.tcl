# One station, swept over master and slave count, to show how a station scales
# with ports.  -tclargs <nm> <nq> <fw> <lut_per_bram> <tag>

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set nm  [lindex $argv 0]
set nq  [lindex $argv 1]
set fw  [lindex $argv 2]
set lpb [lindex $argv 3]
set tag [lindex $argv 4]
if {$nm  eq ""} { set nm  3 }
if {$nq  eq ""} { set nq  4 }
if {$fw  eq ""} { set fw  256 }
if {$lpb eq ""} { set lpb 820 }
if {$tag eq ""} { set tag "${nm}x${nq}" }

proc clog2 {n} {
    set w 1
    while {[expr {1 << $w}] < $n} { incr w }
    return $w
}
set portw [clog2 $nq]
set srcw  [clog2 $nm]

set_param general.maxThreads 4

read_verilog [list \
    [file join $root src common sync_fifo.v] \
    [file join $root src common async_fifo.v] \
    [file join $root src kohakuaxi station sb_skid.v] \
    [file join $root src kohakuaxi station sb_hub.v] \
    [file join $root src kohakuaxi station sb_stn_line.v]]

set xdc $root/build/ooc_stn_${tag}.xdc
file mkdir $root/build
set fh [open $xdc w]
puts $fh "create_clock -name clk -period 5.000 \[get_ports clk\]"
close $fh
read_xdc $xdc

puts "@@@ stn sweep nm $nm nq $nq fw $fw lpb $lpb tag $tag"

# LUT_PER_BRAM was parsed, defaulted and printed in the banner but never reached
# synth_design, so every point of an lpb sweep was the SAME netlist relabelled.
synth_design -top sb_stn_line -part $part -mode out_of_context \
             -generic NM=$nm -generic NQ=$nq -generic FW=$fw \
             -generic PORTW=$portw -generic SRCW=$srcw -generic NSTN=4 \
             -generic LUT_PER_BRAM=$lpb

source [file join $root scripts tcl ooc_class.tcl]
puts "@@@ ============================ $tag"
ooc_util
ooc_classify 500
puts "@@@ stn sweep done $tag"

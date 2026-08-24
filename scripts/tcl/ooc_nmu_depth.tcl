# What AXI4 compliance costs at one manager port: sb_nmu at a given width and
# REQ_DEPTH. AXI4 allows AWLEN 255, and sb_nmu's own rule is REQ_DEPTH >= max
# AxLEN + 1, so 257 is the compliant depth and the shipped 64 is not.
#
#   vivado -mode batch -source scripts/tcl/ooc_nmu_depth.tcl -tclargs <mw> <depth> [mem]

set mw    [lindex $argv 0]
set depth [lindex $argv 1]
set mem   [lindex $argv 2]
if {$mw eq ""}    { set mw 64 }
if {$depth eq ""} { set depth 64 }
if {$mem eq ""}   { set mem block }

set part xcvu13p-fhgb2104-2L-e
set root [file normalize [file join [file dirname [info script]] .. ..]]
set ::ooc_period 2.857

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakuaccel common sync_fifo.v] \
    [file join $root src kohakuaccel common async_fifo.v] \
    [file join $root src kohakuaccel axi station sb_nmu.v]]

puts "@@@ mw $mw req_depth $depth mem $mem"
synth_design -top sb_nmu -part $part -mode out_of_context \
             -flatten_hierarchy rebuilt -directive default \
             -generic MW=$mw -generic FW=256 -generic AW=43 \
             -generic MIDW=4 -generic TAGW=4 -generic DSTW=2 -generic NSEG=8 \
             -generic REQ_DEPTH=$depth -generic RSP_DEPTH=$depth \
             -generic REQ_MEM=$mem -generic RSP_MEM=$mem

ooc_count TOTAL
ooc_util
puts "@@@ nmu_depth done mw=$mw depth=$depth mem=$mem"

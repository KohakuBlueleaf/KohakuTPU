# OOC synthesis of the FLOAT ACCUMULATE LOOP -- flop, add, flop, and nothing
# else. SYNTH ONLY.
#   vivado -mode batch -source scripts/tcl/ooc_facc.tcl -tclargs <period_ns> <mw>
#
# It answers one question: can `acc <= acc + x` in the cluster's accumulator
# float close at the DSP PE's clock in ONE cycle? At yes, the float tier keeps
# tier 1's shape. At no, `vfmacc` needs rotating accumulators and that becomes a
# rule in the programming model.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set per [lindex $argv 0]
set mw  [lindex $argv 1]
if {$per eq ""} { set per 3.333 }
if {$mw  eq ""} { set mw  14 }
set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakutpu matmul mx_fpacc.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_facc_loop.v]]

read_xdc [file join $root scripts xdc ooc_khd.xdc]

puts "@@@ top khd_facc_loop mw $mw period $per"

synth_design -top khd_facc_loop -part $part -mode out_of_context \
             -flatten_hierarchy none -directive default \
             -generic MW=$mw

ooc_record "facc-mw$mw-t$per" "top=khd_facc_loop mw=$mw period=$per" 2000 2

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ Fmax per clock"
ooc_classify 2000

puts "@@@ ooc_facc done"

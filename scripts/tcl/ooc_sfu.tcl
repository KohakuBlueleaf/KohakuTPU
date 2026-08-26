# OOC synthesis of KohakuMPE's FP32 seed unit, alone.
#   vivado -mode batch -source scripts/tcl/ooc_sfu.tcl -tclargs <period>
#
# ONE UNIT, so the coefficient table's BRAM and the backend's LUT are separable
# from the FMA they sit beside. `khs_fp32_alu` prices the pair.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set per [lindex $argv 0]
if {$per eq ""} { set per 3.333 }
set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakumpe simd khs_lead1.v] \
    [file join $root src kohakumpe simd generated khs_seed_tab.v] \
    [file join $root src kohakumpe simd khs_fp32_sfu.v]]

read_xdc [file join $root scripts xdc ooc_rv_pe.xdc]

synth_design -top khs_fp32_sfu -part $part -mode out_of_context \
             -flatten_hierarchy rebuilt -directive default

ooc_record "sfu-t$per" "period=$per" 1 2

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ vivado utilization"
ooc_util

puts "@@@ ooc_sfu done"

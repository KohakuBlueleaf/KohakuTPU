# OOC synthesis of the FIXED-POINT float accumulator -- align outside the loop,
# integer add inside it. SYNTH ONLY.
#   vivado -mode batch -source scripts/tcl/ooc_facc_fixed.tcl -tclargs <ns> <aw>
#
# Read it against ooc_facc.tcl, which measured the float loop at 152.3 MHz and
# 25 levels. If this closes at the PE's clock, `vfmacc` needs no
# accumulator-rotation contract and NACC = 1 is enough; the price is this
# module's LUT against the float form's 420 per slot.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set per [lindex $argv 0]
set aw  [lindex $argv 1]
if {$per eq ""} { set per 3.333 }
if {$aw  eq ""} { set aw  96 }
set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakuaccel pe rv32 simd khs_facc_fixed.v]]

read_xdc [file join $root scripts xdc ooc_khs.xdc]

puts "@@@ top khs_facc_fixed aw $aw period $per"

synth_design -top khs_facc_fixed -part $part -mode out_of_context \
             -flatten_hierarchy none -directive default \
             -generic AW=$aw

ooc_record "faccfx-aw$aw-t$per" "top=khs_facc_fixed aw=$aw period=$per" 2000 2

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ Fmax per clock"
ooc_classify 2000

puts "@@@ ooc_facc_fixed done"

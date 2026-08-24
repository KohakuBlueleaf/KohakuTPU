# The float lane priced by LIVE OPCODE SET. SYNTH ONLY, results are the @@@ lines.
#   vivado -mode batch -source scripts/tcl/ooc_opcost.tcl -tclargs <nops> <ns> <flat>
#
# CONSTRAINED BEFORE SYNTHESIS, unlike ooc_valu.tcl, which calls create_clock
# AFTER synth_design and so reports UNCONSTRAINED area -- figures that cannot be
# set beside a PE measured at an ask.

set root [file normalize [file join [file dirname [info script]] .. ..]]

set nops [lindex $argv 0]
set per  [lindex $argv 1]
set flat [lindex $argv 2]
if {$nops eq ""} { set nops 1 }
if {$per  eq ""} { set per 2.857 }
if {$flat eq ""} { set flat rebuilt }
set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakutpu vector vec_dsp.v] \
    [file join $root src kohakutpu vector vec_delay.v] \
    [file join $root src kohakutpu vector vec_cvt.v] \
    [file join $root src kohakutpu matmul mx_fpacc.v] \
    [file join $root src kohakutpu vector vec_tables.v] \
    [file join $root src kohakutpu vector vec_alu.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_float_lane.v] \
    [file join $root tests pe probe khg_opcost.v]]

read_xdc [file join $root scripts xdc ooc_khg.xdc]

synth_design -top khg_opcost -part xcvu13p-fhgb2104-2L-e -mode out_of_context \
             -flatten_hierarchy $flat -directive default \
             -generic NOPS=$nops

if {[llength [get_clocks -quiet]] == 0} {
    error "no clock: ooc_khg.xdc did not apply, so every figure is unconstrained"
}

ooc_record "opcost-n$nops-t$per-$flat" "nops=$nops period=$per flatten=$flat" 2000 3
puts "@@@ ============================ device totals"
ooc_count TOTAL
puts "@@@ ooc_opcost done nops=$nops period=$per flatten=$flat"

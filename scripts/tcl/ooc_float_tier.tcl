# OOC synthesis of the FLOAT TIER ALONE against its lane count. SYNTH ONLY.
#   vivado -mode batch -source scripts/tcl/ooc_float_tier.tcl -tclargs <period_ns>
#
# One row per lane count, one Vivado session, because the rows are only useful
# against each other: the tier at 16 lanes is what khs_unit builds today, and
# the smaller ones are `vfmacc` at II = NARROW_SLOTS/FLANES.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set per [lindex $argv 0]
if {$per eq ""} { set per 3.333 }
set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

set srcs [list \
    [file join $root src kohakuaccel common kohaku_sdpram.v] \
    [file join $root src kohakutpu matmul mx_fpacc.v] \
    [file join $root src kohakutpu vector vec_dsp.v] \
    [file join $root src kohakutpu vector vec_delay.v] \
    [file join $root src kohakutpu vector vec_tables.v] \
    [file join $root src kohakutpu vector vec_cvt.v] \
    [file join $root src kohakutpu vector vec_alu.v] \
    [file join $root src kohakumpe simd khs_float_lane.v] \
    [file join $root src kohakumpe simd khs_facc.v] \
    [file join $root src kohakumpe simd khs_ffold.v] \
    [file join $root src kohakumpe simd khs_float_tier.v]]

foreach n {16 8 4 2} {
    puts "@@@ ============================ float_tier lanes $n"
    read_verilog $srcs
    read_xdc [file join $root scripts xdc ooc_khs.xdc]
    synth_design -top khs_float_tier -part $part -mode out_of_context \
                 -flatten_hierarchy none -directive default \
                 -generic NARROW_SLOTS=16 -generic FLANES=$n
    ooc_record "float_tier-l$n-t$per" "top=khs_float_tier flanes=$n period=$per" 2000 2
    ooc_count "lanes$n"
    ooc_classify 2000
    close_design
}

puts "@@@ ooc_float_tier done"

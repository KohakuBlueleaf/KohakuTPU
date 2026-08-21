# OOC synthesis of the SHIPPED E8M15 vector lane, unmodified, at the DSP PE's
# own clock ask. SYNTH ONLY. Results are the @@@ lines.
#   vivado -mode batch -source scripts/tcl/ooc_veclane.tcl -tclargs <period_ns>
#
# It exists to price tier 2 with a number rather than an estimate. The vector
# core's own results quote 1,249 LUT and 3 DSP per lane at a 300 MHz target on
# its own flow; this measures the same RTL on the same part, at the same period,
# through the same script the DSP unit's rows come from -- so the two are
# comparable, which quoted figures from two flows are not.
#
# NOTHING IS MODIFIED. `vec_alu` is another project's shipped module and this
# reads it. What a STRIPPED lane would cost -- FMA only, no transcendental
# tables, no third DSP -- is a subtraction from this, and the subtraction is
# named in the notes rather than smuggled into a build.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set per [lindex $argv 0]
if {$per eq ""} { set per 3.333 }
set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakuaccel common kohaku_sdpram.v] \
    [file join $root src kohakutpu matmul mx_fpacc.v] \
    [file join $root src kohakutpu vector vec_dsp.v] \
    [file join $root src kohakutpu vector vec_delay.v] \
    [file join $root src kohakutpu vector vec_tables.v] \
    [file join $root src kohakutpu vector vec_alu.v]]

read_xdc [file join $root scripts xdc ooc_khd.xdc]

puts "@@@ top vec_alu period $per"

synth_design -top vec_alu -part $part -mode out_of_context \
             -flatten_hierarchy none -directive default \
             -generic MODEL=0

ooc_record "vec_alu-t$per" "top=vec_alu period=$per" 2000 2

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ vivado utilization"
ooc_util

puts "@@@ ============================ per unit"
foreach inst {u_tab u_dsp_e u_dsp_m u_dsp_p u_l1} {
    if {[llength [get_cells -quiet $inst]] == 0} {
        puts "@@@ $inst MISSING"
        continue
    }
    ooc_count $inst $inst
}

puts "@@@ ============================ Fmax per clock"
ooc_classify 2000

puts "@@@ ooc_veclane done period $per"

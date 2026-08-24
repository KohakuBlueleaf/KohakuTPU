# OOC synthesis of ONE STRIPPED E8M15 lane -- the shipped vec_alu with `op` tied
# to FMA, so what the transcendental path costs comes off by constant
# propagation rather than by editing another project's module. SYNTH ONLY.
#   vivado -mode batch -source scripts/tcl/ooc_e8fma.tcl -tclargs <period_ns>
#
# Read this beside ooc_veclane.tcl: same part, same ask, same script family. The
# difference between the two rows IS the price of the seeds, and it is what
# decides whether a tier-2 DSP lane can afford one FMA per element.

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
    [file join $root src kohakutpu vector vec_alu.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_e8_fma.v]]

read_xdc [file join $root scripts xdc ooc_khs.xdc]

puts "@@@ top khs_e8_fma period $per"

synth_design -top khs_e8_fma -part $part -mode out_of_context \
             -flatten_hierarchy none -directive default

# Depth 5, not 2: the point of this run is WHERE the LUTs are inside the lane --
# aligner, leading-one search, normaliser, rounder -- because each of those is a
# candidate to move onto a DSP48, and a flat total cannot say which.
ooc_record "e8fma-t$per" "top=khs_e8_fma period=$per" 2000 5

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ Fmax per clock"
ooc_classify 2000

puts "@@@ ooc_e8fma done"

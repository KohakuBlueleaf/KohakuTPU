# OOC synthesis of ONE scalar FP32 ALU, against the E8M15 lane it would replace.
#   vivado -mode batch -source scripts/tcl/ooc_fpu.tcl -tclargs <top> <period>
#
# ONE UNIT, NOT A LANE ARRAY: the whole point is to optimise FALU area where a
# mistake is paid once rather than eight times, and to compare like with like --
# `vec_alu` at HAS_POLY=0 is the same FMA back-end without the seed tables.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set top [lindex $argv 0]
set per [lindex $argv 1]
if {$top eq ""} { set top rv_fpu }
if {$per eq ""} { set per 3.333 }
set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakutpu matmul mx_fpacc.v] \
    [file join $root src kohakutpu vector vec_dsp.v] \
    [file join $root src kohakutpu vector vec_delay.v] \
    [file join $root src kohakutpu vector vec_tables.v] \
    [file join $root src kohakutpu vector vec_cvt.v] \
    [file join $root src kohakutpu vector vec_alu.v] \
    [file join $root src kohakuaccel pe rv32 core rv_fpu.v]]

read_xdc [file join $root scripts xdc ooc_rv_pe.xdc]

if {$top eq "vec_alu_nopoly"} {
    synth_design -top vec_alu -part $part -mode out_of_context \
                 -flatten_hierarchy rebuilt -directive default \
                 -generic HAS_POLY=0 -generic MODEL=0
    set tag "vec_alu-nopoly-t$per"
    set cfg "top=vec_alu poly=0 period=$per"
} elseif {$top eq "vec_alu"} {
    synth_design -top vec_alu -part $part -mode out_of_context \
                 -flatten_hierarchy rebuilt -directive default \
                 -generic MODEL=0
    set tag "vec_alu-full-t$per"
    set cfg "top=vec_alu poly=1 period=$per"
} else {
    synth_design -top rv_fpu -part $part -mode out_of_context \
                 -flatten_hierarchy rebuilt -directive default
    set tag "rv_fpu-t$per"
    set cfg "top=rv_fpu poly=0 period=$per"
}

ooc_record $tag $cfg 1 2

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ vivado utilization"
ooc_util

puts "@@@ ============================ the binding path"
foreach l [split [report_timing -max_paths 1 -nworst 1 -setup -input_pins \
                      -return_string] "\n"] {
    puts "@@@P $l"
}

puts "@@@ ooc_fpu done $tag"

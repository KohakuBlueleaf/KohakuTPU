# OOC synthesis of the WHOLE vector core -- sequencer, L1, AGU, the 16-lane
# array and its register file -- at the shipped configuration. SYNTH ONLY.
# Results are the @@@ lines.
#
#   vivado -mode batch -source scripts/tcl/ooc_vec_core.tcl \
#          -tclargs <period_ns> <flatten> <tag> <srcdir>
#
# ooc_veclane.tcl tops at `vec_alu` -- one lane, below everything that decides
# an instruction -- so predication, the write-enable decode and the sequencer are
# invisible there, and the only other instrument was the whole assembled mesh.
#
# `srcdir` is a DIRECTORY OF FLAT .v FILES, or "" for the repo tree: with other
# agents editing src/, an A/B is attributable only if both arms read snapshots.
#
# MODEL=0 (real DSP48E2), L1_DEPTH=512, "block" memories and `rebuilt` are what
# mm_mesh builds. `none` is a DIAGNOSTIC -- it keeps the boundaries the per-unit
# counts name -- and must never share a table with a `rebuilt` row uncolumned.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set per  [lindex $argv 0]
if {$per eq ""} { set per 3.333 }
set flat [lindex $argv 1]
if {$flat eq ""} { set flat rebuilt }
set tag  [lindex $argv 2]
if {$tag eq ""} { set tag base }
set srcd [lindex $argv 3]

set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

# Named by BASENAME so one list serves both the repo tree and a flat snapshot;
# a snapshot that is missing a file then fails on read_verilog rather than
# silently synthesising a black box.
set repo_path [dict create \
    kohaku_sdpram.v [file join $root src kohakuaccel common kohaku_sdpram.v] \
    mx_fpacc.v      [file join $root src kohakutpu matmul mx_fpacc.v] \
    vec_dsp.v       [file join $root src kohakutpu vector vec_dsp.v] \
    vec_delay.v     [file join $root src kohakutpu vector vec_delay.v] \
    vec_tables.v    [file join $root src kohakutpu vector vec_tables.v] \
    vec_alu.v       [file join $root src kohakutpu vector vec_alu.v] \
    vec_cvt.v       [file join $root src kohakutpu vector vec_cvt.v] \
    vec_regfile.v   [file join $root src kohakutpu vector vec_regfile.v] \
    vec_lanes.v     [file join $root src kohakutpu vector vec_lanes.v] \
    vec_agu.v       [file join $root src kohakutpu vector vec_agu.v] \
    vec_core.v      [file join $root src kohakutpu vector vec_core.v]]

set files {}
foreach f [dict keys $repo_path] {
    set p [expr {$srcd eq "" ? [dict get $repo_path $f] : [file join $srcd $f]}]
    if {![file exists $p]} { error "missing source: $p" }
    lappend files $p
}
puts "@@@ sources from [expr {$srcd eq {} ? {the repo tree} : $srcd}]"

read_verilog $files
read_xdc [file join $root scripts xdc ooc_khs.xdc]

puts "@@@ top vec_core tag $tag period $per flatten $flat"

synth_design -top vec_core -part $part -mode out_of_context \
             -flatten_hierarchy $flat -directive default \
             -generic MODEL=0 -generic L1_DEPTH=512 \
             -generic L1_PRIM=block -generic RF_PRIM=block

ooc_record "veccore-$tag-t$per-h$flat" \
    "top=vec_core period=$per flatten=$flat" 2000 3

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ vivado utilization"
ooc_util

# At `rebuilt` the lane array keeps its boundary but the ALUs below it may not,
# so a MISSING row here is a flattening outcome and not an empty instance.
puts "@@@ ============================ per unit"
foreach inst {u_lanes u_agu u_imem u_l1} {
    if {[llength [get_cells -quiet $inst]] == 0} {
        puts "@@@ $inst MISSING"
        continue
    }
    ooc_count $inst $inst
}

puts "@@@ ============================ control sets"
ooc_ctrlsets

puts "@@@ ============================ Fmax per clock"
ooc_classify 2000

puts "@@@ ============================ the binding path"
foreach l [split [report_timing -max_paths 1 -nworst 1 -setup -input_pins \
                      -return_string] "\n"] {
    puts "@@@P $l"
}

puts "@@@ ooc_vec_core done tag $tag period $per flatten $flat"

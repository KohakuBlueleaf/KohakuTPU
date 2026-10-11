# OOC synthesis of the V2 vector core (src/kohakutpu/vector2), the same way
# ooc_vec_core.tcl synthesises v1, so the two report side by side. SYNTH ONLY.
#
#   vivado -mode batch -source scripts/tcl/ooc_v2_core.tcl \
#          -tclargs <period_ns> <flatten> <tag> <srcdir> <generics>
#
# `srcdir` is a directory of flat .v files, or "" for the repo tree.
# `generics` is NAME:VALUE joined with + (vivado.bat splits arguments at "=").

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set per  [lindex $argv 0]
if {$per eq ""} { set per 3.333 }
set flat [lindex $argv 1]
if {$flat eq ""} { set flat rebuilt }
set tag  [lindex $argv 2]
if {$tag eq ""} { set tag base }
set srcd [lindex $argv 3]
# "-" names the repo tree too: an empty argument does not survive vivado.bat.
if {$srcd eq "-"} { set srcd "" }
set gens {}
foreach g [split [lindex $argv 4] "+"] {
    if {$g ne ""} { lappend gens -generic [string map {: =} $g] }
}

set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

set repo_path [dict create \
    kohaku_sdpram.v [file join $root src kohakuaccel common kohaku_sdpram.v] \
    mx_fpacc.v      [file join $root src kohakutpu matmul mx_fpacc.v] \
    vec_dsp.v       [file join $root src kohakutpu vector vec_dsp.v] \
    vec_delay.v     [file join $root src kohakutpu vector vec_delay.v] \
    vec_tables.v    [file join $root src kohakutpu vector vec_tables.v] \
    vec_cvt.v       [file join $root src kohakutpu vector vec_cvt.v] \
    v2_agu.v        [file join $root src kohakutpu vector2 v2_agu.v] \
    v2_fifo.v       [file join $root src kohakutpu vector2 v2_fifo.v] \
    v2_gt4.v        [file join $root src kohakutpu vector2 v2_gt4.v] \
    v2_mxq.v        [file join $root src kohakutpu vector2 v2_mxq.v] \
    v2_cvt.v        [file join $root src kohakutpu vector2 v2_cvt.v] \
    v2_xbar.v       [file join $root src kohakutpu vector2 v2_xbar.v] \
    v2_alu.v        [file join $root src kohakutpu vector2 v2_alu.v] \
    v2_lanes.v      [file join $root src kohakutpu vector2 v2_lanes.v] \
    v2_core.v       [file join $root src kohakutpu vector2 v2_core.v]]

set files {}
foreach f [dict keys $repo_path] {
    set p [expr {$srcd eq "" ? [dict get $repo_path $f] : [file join $srcd $f]}]
    if {![file exists $p]} { error "missing source: $p" }
    lappend files $p
}
puts "@@@ sources from [expr {$srcd eq {} ? {the repo tree} : $srcd}]"

read_verilog $files
read_xdc [file join $root scripts xdc ooc_khs.xdc]

puts "@@@ top v2_core tag $tag period $per flatten $flat generics {$gens}"

synth_design -top v2_core -part $part -mode out_of_context \
             -flatten_hierarchy $flat -directive default \
             -generic MODEL=0 -generic L1_DEPTH=512 -generic L1_PRIM=block {*}$gens

ooc_record "v2core-$tag-t$per-h$flat" \
    "top=v2_core period=$per flatten=$flat" 2000 3

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ vivado utilization"
ooc_util

puts "@@@ ============================ per unit"
foreach inst {u_lanes u_agu u_imem u_ugt u_pgt g_mx.u_mxq u_mq u_uq g_q1.u_uq1 u_pq g_q1.u_pq1
              u_fq u_dq u_uf u_pf u_ft} {
    if {[llength [get_cells -quiet $inst]] == 0} {
        puts "@@@ $inst MISSING"
        continue
    }
    ooc_count $inst $inst
}

puts "@@@ ============================ hierarchy"
foreach l [split [report_utilization -hierarchical -hierarchical_depth 3 -return_string] "\n"] {
    puts "@@@H $l"
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

ooc_cones 10

puts "@@@ ============================ LUT census"
ooc_lut_census ALL "" 60
foreach inst {u_agu u_ugt g_mx.u_mxq u_mq u_uq u_pq u_pf} {
    ooc_lut_census $inst $inst 12
}

puts "@@@ ooc_v2_core done tag $tag period $per flatten $flat"

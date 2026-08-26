# OOC synthesis of one SysCore module, or the whole thing.
#   vivado -mode batch -source scripts/tcl/ooc_syscore.tcl \
#          -tclargs <top> <period> [mem_prim]
#
# LUT is the objective and BRAM/URAM are not, so this prints the memory columns
# beside the LUT count: a module that simulates perfectly can still fall out of
# block RAM, and nothing but synthesis says so.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set top  [lindex $argv 0]
set per  [lindex $argv 1]
set prim [lindex $argv 2]
if {$top  eq ""} { set top rv64_regfile }
if {$per  eq ""} { set per 3.333 }
if {$prim eq ""} { set prim block }
set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [glob \
    [file join $root src kohakuaccel common kohaku_sdpram.v] \
    [file join $root src kohakuaccel common sync_fifo.v] \
    [file join $root src kohakuaccel common sb_skid.v] \
    [file join $root src kohakuaccel noc endpoint noc_cu_base.v] \
    [file join $root src kohakutpu transform mx_quant.v] \
    [file join $root src kohakutpu transform xform_bank.v] \
    [file join $root src kohakuaccel sysnode core mag_xform.v] \
    [file join $root src kohakuaccel sysnode mover mx_tdesc.v] \
    [file join $root src kohakuaccel sysnode mover mm_prng.v] \
    [file join $root src kohakuaccel sysnode mover mm_mover.v] \
    [file join $root src kohakuaccel sysnode cpu rv64_mag_pe.v] \
    [file join $root src kohakuaccel pe rv64-sys *.v] \
    [file join $root src kohakuaccel pe rv64-sys core *.v]]

read_xdc [file join $root scripts xdc ooc_rv_pe.xdc]

set gen [list MEM_PRIM=$prim]
set flat rebuilt
foreach extra [lrange $argv 3 end] {
    if {$extra eq "HIER"} { set flat none } else { lappend gen $extra }
}

# `none` ATTRIBUTES, `rebuilt` SHIPS. A hierarchical report on a rebuilt netlist
# re-parents leaves and lies about where the LUTs went; `none` keeps boundaries
# but is NOT the number to quote as the design's area.
synth_design -top $top -part $part -mode out_of_context \
             -flatten_hierarchy $flat -directive default \
             -generic $gen

if {$flat eq "none"} {
    puts "@@@ ============================ hierarchy (flatten=none)"
    set rpt [report_utilization -hierarchical -return_string]
    foreach line [split $rpt "\n"] {
        if {[regexp {^\|\s+\S} $line]} { puts "@@@HIER $line" }
    }
}

ooc_record "syscore-$top-$prim-t$per" "top=$top prim=$prim period=$per" 1 2

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ vivado utilization"
ooc_util

# EVERY FAILING PATH, not the worst one. Reporting a single path twice sent
# this work chasing the wrong cut; a design has a critical REGION and one
# endpoint rarely names it.
puts "@@@ ============================ every path with negative slack"
# GROUPED, not enumerated. 200 individual paths are one region reported 200
# times; the structure is in which START and which END repeat, so the endpoint
# register and the source register are collapsed to their base names and
# counted. Chasing the single worst path fixes one row and reveals the next.
set bad [get_timing_paths -max_paths 2000 -nworst 1 -setup -slack_lesser_than 0]
puts "@@@FAILN [llength $bad]"

proc base {pin} {
    # a/b/c_reg[3]/CE -> a/b/c_reg
    set c [file dirname $pin]
    regsub {\[[0-9]+\]$} $c "" c
    return $c
}

array set grp {}
foreach p $bad {
    set k "[base [get_property STARTPOINT_PIN $p]] -> [base [get_property ENDPOINT_PIN $p]]"
    set s [get_property SLACK $p]
    set l [get_property LOGIC_LEVELS $p]
    if {[info exists grp($k)]} {
        lassign $grp($k) cnt worst lvl
        set grp($k) [list [expr {$cnt + 1}] [expr {$s < $worst ? $s : $worst}] \
                          [expr {$l > $lvl ? $l : $lvl}]]
    } else {
        set grp($k) [list 1 $s $l]
    }
}
# Worst group first, because that is the one that decides the clock.
set rows {}
foreach k [array names grp] {
    lassign $grp($k) cnt worst lvl
    lappend rows [list $worst $cnt $lvl $k]
}
foreach r [lsort -real -index 0 $rows] {
    lassign $r worst cnt lvl k
    puts [format "@@@GROUP %4d paths  worst %+7.3f  lvl %3s  %s" $cnt $worst $lvl $k]
}

puts "@@@ ============================ the worst one in full"
foreach l [split [report_timing -max_paths 1 -nworst 1 -setup -input_pins \
                      -return_string] "\n"] {
    puts "@@@P $l"
}

puts "@@@ ooc_syscore done $top $prim"

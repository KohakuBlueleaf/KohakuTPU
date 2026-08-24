# Every failing setup path in a saved checkpoint, grouped so the report says
# WHERE the negative slack is rather than only how negative the worst one is.
#
#   vivado -mode batch -source scripts/tcl/slack_list.tcl -tclargs <dcp> <out>
#
# Reads a DCP rather than re-running synthesis: one probe slot for minutes
# instead of a whole rebuild, and the numbers are the ones already measured.

set dcp [lindex $argv 0]
set out [lindex $argv 1]

open_checkpoint $dcp

set fh [open $out w]

# ---- 1. the summary tables, per clock ---------------------------------------
puts $fh "==== clocks ===="
foreach c [get_clocks] {
    set p [get_property PERIOD $c]
    puts $fh [format "%-24s period %8.3f  freq %8.2f MHz" $c $p [expr {1000.0/$p}]]
}

# ---- 2. WNS/TNS/count per clock group --------------------------------------
# Per clock, not just globally: one clock at -0.1 across 400 endpoints and
# another at -1.2 across 3 are different problems with the same headline.
puts $fh ""
puts $fh "==== per-clock setup ===="
puts $fh [format "%-24s %10s %12s %8s" "clock" "WNS" "TNS" "FEP"]
foreach c [get_clocks] {
    set paths [get_timing_paths -setup -to [get_clocks $c] -max_paths 100000 \
                   -slack_lesser_than 0 -quiet]
    set n [llength $paths]
    set tns 0.0
    set wns 0.0
    foreach p $paths {
        set s [get_property SLACK $p]
        set tns [expr {$tns + $s}]
        if {$s < $wns} { set wns $s }
    }
    if {$n > 0} {
        puts $fh [format "%-24s %10.3f %12.3f %8d" $c $wns $tns $n]
    }
}

# ---- 3. every failing path, worst first -------------------------------------
# -nworst 1 per endpoint would hide a hot spot: 400 endpoints behind ONE net is
# the shape that says "fix the net", and only the unfiltered list shows it.
set failing [get_timing_paths -setup -max_paths 100000 -slack_lesser_than 0 \
                 -sort_by slack -quiet]
puts $fh ""
puts $fh "==== failing setup paths: [llength $failing] ===="
# DATAPATH_DELAY only: a timing_path has no LOGIC_DELAY / NET_DELAY property,
# and asking for one aborts the whole script. The logic-vs-route split comes
# from the report_timing append at the end.
puts $fh [format "%9s %6s %9s %-50s %-50s" \
              "slack" "levels" "datapath" "from" "to"]

# Count how many failing paths each start and end point owns, so the report can
# name the shared net instead of listing 400 near-identical rows.
array set from_n {}
array set to_n {}

set i 0
foreach p $failing {
    set s   [get_property SLACK $p]
    set lv  [get_property LOGIC_LEVELS $p]
    set dl  [get_property DATAPATH_DELAY $p]
    set src [get_property STARTPOINT_PIN $p]
    set dst [get_property ENDPOINT_PIN $p]
    if {![info exists from_n($src)]} { set from_n($src) 0 }
    if {![info exists to_n($dst)]}   { set to_n($dst) 0 }
    incr from_n($src)
    incr to_n($dst)
    if {$i < 60} {
        puts $fh [format "%9.3f %6s %9.3f %-50s %-50s" $s $lv $dl $src $dst]
    }
    incr i
}
if {$i >= 60} {
    puts $fh "... [expr {$i - 60}] more failing paths not listed individually"
}

# ---- 4. the hot spots -------------------------------------------------------
proc dump_hot {fh label arrname} {
    upvar 1 $arrname a
    set rows {}
    foreach k [array names a] { lappend rows [list $a($k) $k] }
    set rows [lsort -integer -decreasing -index 0 $rows]
    puts $fh ""
    puts $fh "==== $label ===="
    set n 0
    foreach r $rows {
        puts $fh [format "%6d  %s" [lindex $r 0] [lindex $r 1]]
        if {[incr n] >= 25} { break }
    }
}
dump_hot $fh "failing paths per STARTPOINT (worst offenders)" from_n
dump_hot $fh "failing paths per ENDPOINT (worst offenders)" to_n

# ---- 5. the single worst path, in full --------------------------------------
puts $fh ""
puts $fh "==== the worst 12 paths in full: logic vs route is only here ===="
close $fh
report_timing -setup -max_paths 12 -nworst 1 -path_type full_clock_expanded \
    -append -file $out

puts "@@@ SLACKLIST wrote $out"

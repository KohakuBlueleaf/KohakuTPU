# OOC synth of ANY module for ONE config, every report to <outdir>: synth.log,
# hier.rpt (depth 6), util.rpt, timing.rpt (100 paths), timing_summary.rpt,
# census.txt (LUTs bucketed by driven signal), result.txt (one line). One run per
# config, never two.
#
#   vivado -mode batch -source scripts/tcl/ooc_mod.tcl \
#       -tclargs <outdir> <top> <period_ns> <clock_port_globs|-> <generics|-> <file>...
#   clock_port_globs: port globs joined by +, each made its own async clock
#                     (e.g. "s_aclk+m_aclk"); "-" = every port named *clk*.
#                     Not commas: vivado.bat splits a comma into two arguments.
#   generics:         NAME:VALUE joined by +

if {[llength $argv] < 6} { puts "usage: ooc_mod.tcl <outdir> <top> <period> <clocks|-> <generics|-> <file>..."; exit 2 }
set outdir [lindex $argv 0]
set top    [lindex $argv 1]
set period [lindex $argv 2]
set cspec  [lindex $argv 3]
set gspec  [lindex $argv 4]
set files  [lrange $argv 5 end]
set part   xcvu13p-fhgb2104-2L-e
file mkdir $outdir

set generics {}
if {$gspec ne "-"} {
    foreach kv [split $gspec "+"] {
        set p [split $kv ":"]
        lappend generics "[lindex $p 0]=[lindex $p 1]"
    }
}
foreach f $files { read_verilog $f }
set cmd [list synth_design -top $top -part $part -mode out_of_context]
foreach g $generics { lappend cmd -generic $g }
if {[catch {eval $cmd} err]} { puts "SYNTH FAILED: $err"; exit 1 }

set clkports {}
if {$cspec eq "-"} {
    set clkports [get_ports -quiet -filter {DIRECTION == IN} *clk*]
} else {
    foreach cg [split $cspec "+"] { set clkports [concat $clkports [get_ports -quiet -filter {DIRECTION == IN} $cg]] }
}
set names {}
foreach cp $clkports {
    set nm [get_property NAME $cp]
    create_clock -name $nm -period $period $cp
    lappend names $nm
}
if {[llength $names] > 1} {
    set grp {}
    foreach nm $names { lappend grp -group [get_clocks $nm] }
    set_clock_groups -asynchronous {*}$grp
}
if {[llength $names] == 0} { puts "NO CLOCK PORT MATCHED"; exit 1 }

report_utilization -hierarchical -hierarchical_depth 6 -file "$outdir/hier.rpt"
report_utilization -file "$outdir/util.rpt"

set cells [get_cells -quiet -hier -filter "REF_NAME =~ LUT?"]
array set bucket {}
foreach c $cells {
    set n [get_property NAME $c]
    regsub {_i_[0-9]+$} $n "" n
    regsub {__[0-9]+$} $n "" n
    regsub -all {\[[0-9]+\]} $n "" n
    regsub {_reg$} $n "" n
    if {$n eq ""} { set n "(unnamed)" }
    incr bucket($n)
}
set rows {}
foreach {k v} [array get bucket] { lappend rows [list $v $k] }
set rows [lsort -integer -decreasing -index 0 $rows]
set cf [open "$outdir/census.txt" w]
puts $cf "total [llength $cells] LUT in [llength $rows] signal(s)"
set i 0
foreach r $rows { if {[incr i] > 80} break; puts $cf [format "%6d  %s" [lindex $r 0] [lindex $r 1]] }
close $cf
report_timing -delay_type max -max_paths 100 -nworst 1 -sort_by slack -file "$outdir/timing.rpt"
report_timing_summary -file "$outdir/timing_summary.rpt"

set paths [get_timing_paths -delay_type max -max_paths 1 -nworst 1]
set wns [get_property SLACK [lindex $paths 0]]
set fmax [expr {1000.0 / ($period - $wns)}]

set lut "?"; set ff "?"; set uram "?"; set bram "?"
foreach line [split [report_utilization -return_string] "\n"] {
    if {[regexp {CLB LUTs\*?\s+\|\s+(\d+)} $line -> v]} { set lut $v }
    if {[regexp {CLB Registers\s+\|\s+(\d+)} $line -> v]} { set ff $v }
    if {[regexp {\|\s*URAM\s+\|\s+(\d+)} $line -> v]} { set uram $v }
    if {[regexp {Block RAM Tile\s+\|\s+([\d.]+)} $line -> v]} { set bram $v }
}
set fh [open "$outdir/result.txt" w]
puts $fh "LUT=$lut FF=$ff URAM=$uram BRAM=$bram WNS=[format %.3f $wns] Fmax=[format %.1f $fmax] top=$top generics={$generics}"
close $fh
puts "RESULT LUT=$lut FF=$ff URAM=$uram BRAM=$bram WNS=[format %.3f $wns] Fmax=[format %.1f $fmax]"

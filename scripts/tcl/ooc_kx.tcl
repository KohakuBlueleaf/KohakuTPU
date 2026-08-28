# OOC synth of kx_mempath_e for ONE config, capturing EVERYTHING to <outdir>:
#   synth.log (full Vivado log), hier.rpt (per-module LUT/FF/BRAM/URAM to the
#   leaf), timing.rpt (every path, -max_paths 100 across all endpoints), util.rpt,
#   result.txt (one-line LUT|FF|URAM|BRAM|WNS|Fmax). One run per config, never two.
#
#   vivado -mode batch -source scripts/tcl/ooc_kx.tcl -tclargs <outdir> <generics|-> <file>...

if {[llength $argv] < 3} { puts "usage: ooc_kx.tcl <outdir> <generics|-> <file>..."; exit 2 }
set outdir [lindex $argv 0]
set gspec  [lindex $argv 1]
set files  [lrange $argv 2 end]
set top    kx_mempath_e
set period 3.333
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

# clocks: the xbar clk, plus every per-port clock bit (m_clk[i], h_clk[j]); a
# port bit that is not used (its CDC bit is 0) drives nothing and is harmless.
# All async to each other.
set clkports [concat [get_ports -quiet clk] \
                     [get_ports -quiet -filter {DIRECTION == IN} m_clk*] \
                     [get_ports -quiet -filter {DIRECTION == IN} h_clk*]]
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

report_utilization -hierarchical -hierarchical_depth 6 -file "$outdir/hier.rpt"
report_utilization -file "$outdir/util.rpt"

# LUT census: bucket every LUT by the signal it drives, so a flat lump names
# the exact select/mux that is not packing (census.txt). Own file channel --
# Vivado's Tcl has no `dup`, and redirecting stdout killed the run.
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
    if {[regexp {Block RAM Tile\s+\|\s+(\d+)} $line -> v]} { set bram $v }
}
set fh [open "$outdir/result.txt" w]
puts $fh "LUT=$lut FF=$ff URAM=$uram BRAM=$bram WNS=[format %.3f $wns] Fmax=[format %.1f $fmax] generics={$generics}"
close $fh
puts "RESULT LUT=$lut FF=$ff URAM=$uram BRAM=$bram WNS=[format %.3f $wns] Fmax=[format %.1f $fmax]"

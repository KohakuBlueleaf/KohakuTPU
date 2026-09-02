# OOC synth of one module at one period, then a PATH-CLASS census: every
# failing (or near-failing) path bucketed by its start and end register
# groups with bit indices stripped, so one run says which handshakes and
# counters miss and by how much, not just the worst endpoint 512 times.
#
#   vivado -mode batch -source scripts/tcl/ooc_paths.tcl \
#       -tclargs <outdir> <top> <period_ns> <clock_port_globs|-> <generics|-> <file>...
# Same arguments as ooc_mod.tcl. Writes <outdir>/paths.txt (the census),
# levels.txt (logic-level histogram of the same paths) and result.txt.

if {[llength $argv] < 6} { puts "usage: ooc_paths.tcl <outdir> <top> <period> <clocks|-> <generics|-> <file>..."; exit 2 }
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
set incdirs {}
foreach f $files { lappend incdirs [file dirname $f] }
set incdirs [lsort -unique $incdirs]
set cmd [list synth_design -top $top -part $part -mode out_of_context -include_dirs $incdirs]
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

# A register group: the cell name without bit indices, generate indices kept
# (they say WHICH partition / home / master), the pin dropped.
proc v8_group {pin} {
    set n [get_property NAME $pin]
    regsub {/[A-Z0-9_]+$} $n "" n
    regsub -all {_reg(\[[0-9]+\])*$} $n "" n
    regsub -all {_rep(_[0-9]+)?$} $n "" n
    regsub -all {\[[0-9]+\](?=/|$)} $n "" n
    return $n
}

set paths [get_timing_paths -delay_type max -max_paths 4000 -nworst 1 -slack_lesser_than 0.25]
array set cnt {}
array set worst {}
array set lev {}
array set dly {}
array set lvhist {}
foreach p $paths {
    set s [v8_group [get_property STARTPOINT_PIN $p]]
    set e [v8_group [get_property ENDPOINT_PIN $p]]
    set k "$s -> $e"
    set sl [get_property SLACK $p]
    set lv [get_property LOGIC_LEVELS $p]
    set dp [get_property DATAPATH_DELAY $p]
    incr cnt($k)
    if {![info exists worst($k)] || $sl < $worst($k)} { set worst($k) $sl; set lev($k) $lv; set dly($k) $dp }
    incr lvhist($lv)
}
set rows {}
foreach {k v} [array get cnt] { lappend rows [list $worst($k) $v $lev($k) $dly($k) $k] }
set rows [lsort -real -index 0 $rows]
set fh [open "$outdir/paths.txt" w]
puts $fh "period $period ns; [llength $paths] paths with slack < 0.25 ns, [llength $rows] classes"
puts $fh [format "%8s %6s %4s %7s  %s" slack n lvl dpath "start -> end"]
foreach r $rows {
    puts $fh [format "%8.3f %6d %4d %7.3f  %s" [lindex $r 0] [lindex $r 1] [lindex $r 2] [lindex $r 3] [lindex $r 4]]
}
close $fh
set fh [open "$outdir/levels.txt" w]
foreach lv [lsort -integer [array names lvhist]] { puts $fh [format "%3d levels: %6d paths" $lv $lvhist($lv)] }
close $fh

report_utilization -file "$outdir/util.rpt"
report_timing_summary -file "$outdir/timing_summary.rpt"
set wp [get_timing_paths -delay_type max -max_paths 1 -nworst 1]
set wns [expr {[llength $wp] ? [get_property SLACK [lindex $wp 0]] : 0.0}]
set fmax [expr {1000.0 / ($period - $wns)}]
set lut "?"
foreach line [split [report_utilization -return_string] "\n"] {
    if {[regexp {CLB LUTs\*?\s+\|\s+(\d+)} $line -> v]} { set lut $v }
}
set fh [open "$outdir/result.txt" w]
puts $fh "LUT=$lut WNS=[format %.3f $wns] Fmax=[format %.1f $fmax] top=$top period=$period generics={$generics}"
close $fh
puts "RESULT LUT=$lut WNS=[format %.3f $wns] Fmax=[format %.1f $fmax] classes=[llength $rows] paths=[llength $paths]"

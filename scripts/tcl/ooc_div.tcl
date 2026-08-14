# OOC for a block that DERIVES its slow clock internally with a BUFGCE_DIV.

# ooc_pump.tcl creates clk1x and clk2x on two PORTS; a unit that divides
# locally has one port, so the 1x clock cannot exist until the cell does.

# OOC_TOP OOC_SRCS OOC_GEN OOC_TAG as ooc_check.tcl. OOC_CLK names the clock
# PORTS (default "clk"), OOC_PERIOD is THEIR period, OOC_IMPL=1 places+routes.

# I/O delay goes on AFTER synthesis, against the SLOWEST clock: a pumped CU's
# ports are 1x, and timing them at 2x reports a boundary artefact as its Fmax.

set root C:/Users/apoll/Desktop/code/Project/KohakuTPU
set top  $::env(OOC_TOP)
set gen  [expr {[info exists ::env(OOC_GEN)]    ? $::env(OOC_GEN)    : ""}]
set tag  [expr {[info exists ::env(OOC_TAG)]    ? $::env(OOC_TAG)    : ""}]
set clks [expr {[info exists ::env(OOC_CLK)]    ? $::env(OOC_CLK)    : "clk"}]
set per  [expr {[info exists ::env(OOC_PERIOD)] ? $::env(OOC_PERIOD) : 1.667}]
set out  $root/build/ooc/$top$tag

# Integer picoseconds. Vivado stores periods at ps resolution and a divided
# clock whose source is not a whole number of ps stops being harmonic with it.
set psr [expr {round($per * 1000)}]
set per [expr {$psr / 1000.0}]

# OOC_THREADS, default 32: a mesh-sized synth is the one that notices.
catch {set_param general.maxThreads \
    [expr {[info exists ::env(OOC_THREADS)] ? $::env(OOC_THREADS) : 32}]}

file mkdir $out
create_project -in_memory -part xcvu13p-fhgb2104-2L-e
foreach f $::env(OOC_SRCS) { read_verilog $root/$f }

set xdc $out/ooc.xdc
set fh [open $xdc w]
foreach c $clks { puts $fh "create_clock -period $per -name $c \[get_ports $c\]" }
# {*rst* *reset*}: `resetn` matches neither *rst* nor *aresetn*, and its fanout
# to every DSP RSTCTRL then reports as the critical path.
puts $fh "set_false_path -from \[get_ports {*rst* *reset*}\]"
close $fh
read_xdc -mode out_of_context $xdc

set gargs {}
foreach g $gen { lappend gargs -generic $g }
# OOC_DEFS selects value-less build options the way the benches do, since a
# parameter cannot reach a module four levels down without being threaded.
if {[info exists ::env(OOC_DEFS)]} {
    foreach d $::env(OOC_DEFS) { lappend gargs -verilog_define $d }
}
synth_design -top $top -mode out_of_context {*}$gargs

set made [get_clocks -quiet]
if {[llength $made] < [llength $clks]} {
    puts "@@@ FAIL only [llength $made] clock(s) created from '$clks'"
    puts "@@@ FAIL ports are: [get_property NAME [get_ports -quiet *]]"
    error "clock constraint did not apply -- set OOC_CLK to the real port names"
}

# ---- what Vivado propagated through the divider ------------------------
set slowest ""
set slowper 0
foreach c [get_clocks] {
    set p [get_property PERIOD $c]
    if {$p > $slowper} { set slowper $p ; set slowest $c }
    puts [format "@@@ CLOCK %-22s period %6.3f ns  generated %s" \
              [get_property NAME $c] $p [get_property IS_GENERATED $c]]
}

set iod [expr {$slowper * 0.30}]
# *clk* excluded: a derived clock brought out as a port is not a data path, and
# constraining it reports the clock tree as a boundary path.
set dports [get_ports -quiet -filter {NAME !~ "*clk*"}]
set_input_delay  -clock $slowest $iod [filter $dports {DIRECTION == IN}]
set_output_delay -clock $slowest $iod [filter $dports {DIRECTION == OUT}]
puts "@@@ IODELAY $iod ns against [get_property NAME $slowest] on [llength $dports] ports"

# ---- the multicycle, on whatever the clocks turned out to be -----------
# Without this a phase-aligned 1x -> 2x path is timed against 0.00 ns: the
# tightest launch/capture pair is the SAME edge.
set npair 0
foreach a [get_clocks] {
    foreach b [get_clocks] {
        if {[get_property NAME $a] eq [get_property NAME $b]} { continue }
        set pa [expr {round([get_property PERIOD $a] * 1000)}]
        set pb [expr {round([get_property PERIOD $b] * 1000)}]
        if {$pa != 2 * $pb} { continue }
        set_multicycle_path -setup 2 -from $a -to $b
        set_multicycle_path -hold  1 -from $a -to $b
        puts "@@@ MCP setup 2 / hold 1  [get_property NAME $a] -> [get_property NAME $b]"
        incr npair
    }
}
if {$npair == 0} {
    puts "@@@ MCP NONE -- no clock is exactly twice another. If this design"
    puts "@@@ MCP divides internally, the divided clock was NOT propagated."
}

if {[info exists ::env(OOC_IMPL)] && $::env(OOC_IMPL) == 1} {
    opt_design
    # WITHOUT A PBLOCK the numbers are placement noise: one cluster CU landed
    # across TWO SLRs, its DSP cascade 43 rows apart, 96% of the path in route.
    if {[info exists ::env(OOC_PBLOCK)]} {
        create_pblock ooc_p
        add_cells_to_pblock ooc_p [get_cells -quiet -hier -filter {IS_PRIMITIVE}]
        resize_pblock ooc_p -add $::env(OOC_PBLOCK)
        puts "@@@ PBLOCK $::env(OOC_PBLOCK) on [llength [get_cells -quiet -hier -filter {IS_PRIMITIVE}]] cells"
    }
    place_design
    phys_opt_design
    route_design
    report_utilization    -file $out/util_routed.rpt
    report_utilization    -hierarchical -file $out/util_routed_hier.rpt
    report_timing_summary -file $out/timing_routed.rpt
    report_timing -max_paths 200 -nworst 200 -path_type full_clock_expanded \
                  -input_pins -file $out/timing_routed_all.rpt
    write_checkpoint -force $out/${top}_routed.dcp
    puts "@@@ ROUTED $top"
}

write_checkpoint -force $out/$top.dcp
report_utilization    -file $out/util.rpt
report_utilization    -hierarchical -file $out/util_hier.rpt
report_timing_summary -file $out/timing_summary.rpt
report_timing -max_paths 200 -nworst 200 -path_type full_clock_expanded \
              -input_pins -file $out/timing_all.rpt

source $root/scripts/tcl/ooc_class.tcl
puts "@@@ TOP $top  clock ports '$clks' at $per ns"
ooc_classify
foreach p [get_timing_paths -max_paths 8 -setup] {
    puts [format "@@@ %7.3f ns lvl %-3s %-38s -> %s" \
              [get_property SLACK $p] [get_property LOGIC_LEVELS $p] \
              [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
}
puts "@@@ SAVED $out"
puts "@@@ DONE"

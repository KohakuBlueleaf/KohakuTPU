# OOC_TOP, OOC_SRCS (space-separated, repo relative), OOC_CLK (clock PORT names,
# default "clk"), OOC_PERIOD and OOC_IO (default 3.125ns and 30% of it).

# A bare create_clock leaves PORT PATHS UNREPORTED: mag_dram_port measured
# 358.7 MHz that way inside a mesh managing 248.2.

# One matching NOTHING is worse: `*aclk*` missed every plain-`clk` block and
# still reported a worst path -- WNS=NA, 21,294 unconstrained endpoints.

set root C:/Users/apoll/Desktop/code/Project/KohakuTPU
set top  $::env(OOC_TOP)
# OOC_GEN "NAME=V ..." overrides parameters; OOC_TAG keeps the runs apart, or
# a sweep overwrites its own earlier results.
set gen  [expr {[info exists ::env(OOC_GEN)] ? $::env(OOC_GEN) : ""}]
set tag  [expr {[info exists ::env(OOC_TAG)] ? $::env(OOC_TAG) : ""}]
set out  $root/build/ooc/$top$tag
set per  [expr {[info exists ::env(OOC_PERIOD)] ? $::env(OOC_PERIOD) : 3.125}]
set iod  [expr {[info exists ::env(OOC_IO)] ? $::env(OOC_IO) : $per * 0.30}]
set clks [expr {[info exists ::env(OOC_CLK)] ? $::env(OOC_CLK) : "clk"}]

# OOC_THREADS, default 32: a mesh-sized synth is the one that notices.
catch {set_param general.maxThreads \
    [expr {[info exists ::env(OOC_THREADS)] ? $::env(OOC_THREADS) : 32}]}

file mkdir $out

# -2L, NOT -2: the board is the low-voltage part and it is the slower of the
# two. Every Fmax measured on -2-e before 2026-08-11 is optimistic.
create_project -in_memory -part xcvu13p-fhgb2104-2L-e
foreach f $::env(OOC_SRCS) { read_verilog $root/$f }

# Constrain against the FIRST clock: one period for the block, which is what an
# Fmax number means. Extra clocks get their own create_clock so their paths are
# not left unconstrained, but I/O delay references the first.
set primary [lindex $clks 0]
set xdc $out/ooc.xdc
set fh [open $xdc w]
foreach c $clks {
    puts $fh "create_clock -period $per -name $c \[get_ports $c\]"
}
puts $fh "set_input_delay  -clock $primary $iod \[all_inputs\]"
puts $fh "set_output_delay -clock $primary $iod \[all_outputs\]"
# OOC_ASYNC=1 puts every clock in its own group. Without it an async FIFO's gray
# pointers are timed synchronously and a CORRECT crossing fails setup AND hold.
if {[info exists ::env(OOC_ASYNC)] && $::env(OOC_ASYNC) == 1} {
    set grp ""
    foreach c $clks { append grp " -group \[get_clocks $c\]" }
    puts $fh "set_clock_groups -asynchronous$grp"
}
# *reset* as well as *rst*: mx_cluster_cu's port is `resetn`, which matched
# neither, so its fanout to every DSP's RSTCTRL was timed as the worst path.
puts $fh "set_false_path -from \[get_ports {*rst* *reset*}\]"
close $fh
read_xdc -mode out_of_context $xdc

set gargs {}
foreach g $gen { lappend gargs -generic $g }
synth_design -top $top -mode out_of_context {*}$gargs

# ABORT rather than report a number from an unconstrained design.
set made [get_clocks -quiet]
if {[llength $made] < [llength $clks]} {
    puts "@@@ FAIL only [llength $made] clock(s) created from '$clks'"
    puts "@@@ FAIL ports are: [get_property NAME [get_ports -quiet *]]"
    error "clock constraint did not apply -- set OOC_CLK to the real port names"
}

# OOC_PLACE=1 places before reporting. MANDATORY FOR ANY DESIGN INSTANTIATING A
# GLOBAL BUFFER, and the reason is not obvious:

# an UNPLACED clock net is estimated as fabric routing -- a BUFGCE output
# measured 2.584 ns at fanout 15,641, against a clock PORT's net of `unset`.

# So a gated arm reads ~2.6 ns worse than an ungated one for a buffer whose CELL
# delay is 28 ps. The number is a placement artifact, not a gating cost.
if {[info exists ::env(OOC_PLACE)] && $::env(OOC_PLACE) == 1} {
    opt_design
    place_design
    report_utilization    -file $out/util_placed.rpt
    report_timing_summary -file $out/timing_placed.rpt
    report_timing -max_paths 50 -nworst 50 -path_type full_clock_expanded \
                  -input_pins -file $out/timing_placed_all.rpt
    puts "@@@ PLACED $top"
    foreach p [get_timing_paths -max_paths 6 -setup] {
        puts [format "@@@ PLACED %7.3f ns  lvl %-3s  %-42s -> %s" \
                  [get_property SLACK $p] [get_property LOGIC_LEVELS $p] \
                  [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
    }
}

write_checkpoint -force $out/$top.dcp
report_utilization      -file $out/util.rpt
report_timing_summary   -file $out/timing_summary.rpt
# EVERY path, not the worst one: the next question is usually about the second.
report_timing -max_paths 200 -nworst 200 -path_type full_clock_expanded \
              -input_pins -file $out/timing_all.rpt

puts "@@@ TOP $top  clocks: [get_property NAME $made]  period $per ns"
foreach p [get_timing_paths -max_paths 12 -setup] {
    set s [get_property SLACK $p]
    set d [get_property DATAPATH_DELAY $p]
    set l [get_property LOGIC_LEVELS $p]
    puts [format "@@@ %7.3f ns  lvl %-3s  %-46s -> %s" $s $l \
              [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
}
puts "@@@ SAVED $out"
puts "@@@ DONE"

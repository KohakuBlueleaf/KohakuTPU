# clk2x is exactly half clk1x, phase aligned, as one MMCM produces them, so
# Vivado TIMES the crossings -- a double pump is only safe if it does.

# OOC_TOP, OOC_SRCS, OOC_GEN, OOC_TAG as ooc_check.tcl; OOC_P1 is the 1x period.

set root C:/Users/apoll/Desktop/code/Project/KohakuTPU
set top  $::env(OOC_TOP)
set gen  [expr {[info exists ::env(OOC_GEN)] ? $::env(OOC_GEN) : ""}]
set tag  [expr {[info exists ::env(OOC_TAG)] ? $::env(OOC_TAG) : ""}]
set p1   [expr {[info exists ::env(OOC_P1)] ? $::env(OOC_P1) : 4.0}]
# EXACTLY 2:1 IN PICOSECONDS, or Vivado rounds p2 and the clocks stop being
# harmonic: OOC_P1=3.333 gave a 1.168 ns requirement on a 1.667 ns clock.
set ps1  [expr {round($p1 * 1000)}]
if {$ps1 % 2} { error "OOC_P1 must be an even number of ps, got $p1 ns" }
set p1   [expr {$ps1 / 1000.0}]
set p2   [expr {($ps1 / 2) / 1000.0}]
set out  $root/.plan/ooc/$top$tag

file mkdir $out
create_project -in_memory -part xcvu13p-fhgb2104-2L-e
foreach f $::env(OOC_SRCS) { read_verilog $root/$f }

set xdc $out/ooc.xdc
set fh [open $xdc w]
puts $fh "create_clock -period $p1 -name clk1x \[get_ports clk1x\]"
puts $fh "create_clock -period $p2 -name clk2x \[get_ports clk2x\]"
puts $fh "set_input_delay  -clock clk1x [expr {$p1 * 0.30}] \[all_inputs\]"
puts $fh "set_output_delay -clock clk1x [expr {$p1 * 0.30}] \[all_outputs\]"
puts $fh "set_false_path -from \[get_ports {*rst* *reset*}\]"
# WITHOUT THIS EVERY 1x->2x PATH IS TIMED AGAINST 0 ns. The clocks are phase
# aligned, so the tightest launch/capture pair is the SAME edge: req = 0.00.
puts $fh "set_multicycle_path -setup 2 -from \[get_clocks clk1x\] -to \[get_clocks clk2x\]"
puts $fh "set_multicycle_path -hold  1 -from \[get_clocks clk1x\] -to \[get_clocks clk2x\]"
close $fh
read_xdc -mode out_of_context $xdc

set gargs {}
foreach g $gen { lappend gargs -generic $g }
synth_design -top $top -mode out_of_context {*}$gargs

if {[llength [get_clocks -quiet]] < 2} { error "clocks did not apply" }

# OOC_IMPL=1 places and routes. Synthesis CANNOT see the 2x domain's routing
# pressure, and that is the entire claim double pumping rests on.
if {[info exists ::env(OOC_IMPL)] && $::env(OOC_IMPL) == 1} {
    opt_design
    place_design
    phys_opt_design
    route_design
    report_utilization    -file $out/util_routed.rpt
    report_timing_summary -file $out/timing_routed.rpt
    puts "@@@ ROUTED"
}

write_checkpoint -force $out/$top.dcp
report_utilization    -file $out/util.rpt
report_timing_summary -file $out/timing_summary.rpt
report_timing -max_paths 200 -nworst 200 -path_type full_clock_expanded \
              -input_pins -file $out/timing_all.rpt

source $root/scripts/tcl/ooc_class.tcl
puts "@@@ TOP $top  1x $p1 ns  2x $p2 ns"
ooc_classify
foreach p [get_timing_paths -max_paths 8 -setup] {
    puts [format "@@@ %7.3f ns  lvl %-3s %-34s -> %s" \
              [get_property SLACK $p] [get_property LOGIC_LEVELS $p] \
              [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
}
puts "@@@ DONE"

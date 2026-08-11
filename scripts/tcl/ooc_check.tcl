# One OOC measurement, kept. Set OOC_TOP and OOC_SRCS (space-separated, repo
# relative); OOC_PERIOD and OOC_IO default to 3.125ns and 30% of it.
#
# A bare create_clock leaves PORT PATHS UNCONSTRAINED and unreported: that is
# how mag_dram_port measured 358.7 MHz inside a mesh managing 248.2.

set root C:/Users/apoll/Desktop/code/Project/KohakuTPU
set top  $::env(OOC_TOP)
set out  $root/.plan/ooc/$top
set per  [expr {[info exists ::env(OOC_PERIOD)] ? $::env(OOC_PERIOD) : 3.125}]
set iod  [expr {[info exists ::env(OOC_IO)] ? $::env(OOC_IO) : $per * 0.30}]

file mkdir $out

# -2L, NOT -2: the board is the low-voltage part and it is the slower of the
# two. Every Fmax measured on -2-e before 2026-08-11 is optimistic.
create_project -in_memory -part xcvu13p-fhgb2104-2L-e
foreach f $::env(OOC_SRCS) { read_verilog $root/$f }

set xdc $out/ooc.xdc
set fh [open $xdc w]
puts $fh "create_clock -period $per -name clk \[get_ports {*aclk*}\]"
puts $fh "set_input_delay  -clock clk $iod \[all_inputs\]"
puts $fh "set_output_delay -clock clk $iod \[all_outputs\]"
puts $fh "set_false_path -from \[get_ports {*aresetn*}\]"
close $fh
read_xdc -mode out_of_context $xdc

synth_design -top $top -mode out_of_context

write_checkpoint -force $out/$top.dcp
report_utilization      -file $out/util.rpt
report_timing_summary   -file $out/timing_summary.rpt
# EVERY path, not the worst one: the next question is usually about the second.
report_timing -max_paths 200 -nworst 200 -path_type full_clock_expanded \
              -input_pins -file $out/timing_all.rpt

puts "@@@ TOP $top"
foreach p [get_timing_paths -max_paths 8 -setup] {
    puts [format "@@@ %7.3f ns  %-52s -> %s" [get_property SLACK $p] \
              [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
}
puts "@@@ SAVED $out"
puts "@@@ DONE"

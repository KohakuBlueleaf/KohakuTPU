# Read-only census of the failed v6 impl at the physopt checkpoint (what the
# router consumed). Decides the -7.596 owner: reset net vs constraints vs
# congestion -- the gate for the per-domain reset work (kh_rst_sync).
set_param general.maxThreads 16

set dcp {C:/Users/apoll/Desktop/vivado/JTAG-DMA-test/JTAG-DMA-test.runs/impl_1/multimesh_v6_wrapper_physopt.dcp}
set out {C:/Users/apoll/Desktop/vivado/JTAG-DMA-test/JTAG-DMA-test.runs/impl_1/census_v6fail}
file mkdir $out

puts "@@@ census open [clock format [clock seconds]]"
open_checkpoint $dcp
puts "@@@ census opened [clock format [clock seconds]]"

report_timing_summary -max_paths 50 -file $out/timing_summary.rpt
report_high_fanout_nets -max_nets 120 -file $out/high_fanout.rpt
report_control_sets -file $out/control_sets.rpt
report_utilization -slr -file $out/util_slr.rpt
report_design_analysis -congestion -complexity -file $out/design_analysis.rpt
report_qor_suggestions -file $out/qor_suggestions.rpt

# The worst paths per clock, enough to see whether reset-release or the
# cdc_ready gating owns them rather than ordinary datapath.
foreach ck [get_clocks] {
    set name [string map {/ _ : _} [get_property NAME $ck]]
    report_timing -setup -max_paths 8 -to [get_clocks $ck] \
        -file $out/worst_$name.rpt
}

puts "@@@ census done [clock format [clock seconds]]"
exit

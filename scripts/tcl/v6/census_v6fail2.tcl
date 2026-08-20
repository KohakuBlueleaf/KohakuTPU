# Round 2 of the failed-v6 census: only the mesh-domain worst paths, with
# SHORT output names -- round 1 died at Windows' 260-byte path limit on a GT
# clock's 500-char name (census_v6fail.log:135).
set_param general.maxThreads 16

set dcp {C:/Users/apoll/Desktop/vivado/JTAG-DMA-test/JTAG-DMA-test.runs/impl_1/multimesh_v6_wrapper_physopt.dcp}
set out {C:/Users/apoll/Desktop/vivado/JTAG-DMA-test/JTAG-DMA-test.runs/impl_1/census_v6fail}

puts "@@@ census2 open [clock format [clock seconds]]"
open_checkpoint $dcp
puts "@@@ census2 opened [clock format [clock seconds]]"

foreach {tag pat} {
    mag0 clk_out4_multimesh_v6_clk_wiz_mesh0_0
    mag1 clk_out4_multimesh_v6_clk_wiz_mesh1_0
    mag3 clk_out4_multimesh_v6_clk_wiz_mesh3_0
    noc0 clk_out1_multimesh_v6_clk_wiz_mesh0_0
    noc2 clk_out1_multimesh_v6_clk_wiz_mesh2_0
    m2x0 clk_out2_multimesh_v6_clk_wiz_mesh0_0
    c1x3 div2_mesh3_clk1x
    bus2 clk_out1_multimesh_v6_clk_wiz_bus2_0
} {
    report_timing -setup -max_paths 12 -to [get_clocks $pat] \
        -file $out/worst_$tag.rpt
}

puts "@@@ census2 done [clock format [clock seconds]]"
exit

# Synth sb_slr1 at ship width and dump the worst bus_clk setup path, so the Fmax
# bottleneck module is identified before pipelining.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set fw 256
set_param general.maxThreads 4
set common [file join $root src kohakuaccel common]
set stn    [file join $root src kohakuaccel axi station]
set topo   [file join $root src kohakuaccel axi topo]
read_verilog [list \
    [file join $common sync_fifo.v] [file join $common async_fifo.v] \
    [file join $common sb_skid.v] [file join $stn sb_hub.v] \
    [file join $stn sb_station.v] [file join $stn sb_nmu.v] \
    [file join $stn sb_nsu.v] [file join $topo sb_slr1.v]]
read_xdc [file join $root scripts xdc ooc_station.xdc]
synth_design -top sb_slr1 -part $part -mode out_of_context -flatten_hierarchy none \
    -generic PRESET=2 -generic OST=4 -generic FW=$fw -generic AW=43
puts "@@@ ---- worst bus_clk setup paths ----"
foreach p [get_timing_paths -to [get_clocks bus_clk] -max_paths 3 -nworst 1 \
             -sort_by slack] {
    puts [format "@@@ SLACK %s  from %s  to %s" \
          [get_property SLACK $p] [get_property STARTPOINT_PIN $p] \
          [get_property ENDPOINT_PIN $p]]
    foreach c [get_cells -of [get_timing_paths -to [get_clocks bus_clk]]] {}
}
report_timing -to [get_clocks bus_clk] -max_paths 1 -nworst 1 -input_pins \
    -file $root/build/slr1_worstpath.rpt
puts "@@@ path_done"

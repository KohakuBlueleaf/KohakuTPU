# Ship station bus (sb_line4, FW=256, LINK_FULL=0) with a HIERARCHY report and a
# PER-SLR (per-station) LUT split -- the 4 stations sit one per die, so g_stn[i] IS
# the SLRi station. Managers are on station MGR_STN=1 (SLR1, the heavy die).
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set fw 256
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set common [file join $root src kohakuaccel common]
set stn    [file join $root src kohakuaccel axi station]
set link   [file join $root src kohakuaccel axi link]
set topo   [file join $root src kohakuaccel axi topo]
read_verilog [list \
    [file join $common sync_fifo.v] [file join $common async_fifo.v] \
    [file join $common sb_skid.v] [file join $stn sb_hub.v] \
    [file join $stn sb_station.v] [file join $stn sb_nmu.v] [file join $stn sb_nsu.v] \
    [file join $link sb_link.v] [file join $link sb_link_cdc.v] \
    [file join $topo sb_stn_line.v] [file join $topo sb_line4.v]]
read_xdc [file join $root scripts xdc ooc_line4.xdc]
synth_design -top sb_line4 -part $part -mode out_of_context -flatten_hierarchy none \
    -directive default -generic FW=$fw -generic AW=43 -generic LINK_FULL=0 \
    -generic OST=4 -generic STORE_FWD=1
puts "@@@ ==== device totals"
ooc_count TOTAL
puts "@@@ ==== PER-SLR (per station g_stn\[i\], one die each)"
for {set s 0} {$s < 4} {incr s} { ooc_count "SLR$s-g_stn$s" "g_stn\[$s\]" }
puts "@@@ ==== SLR1 managers (only die with masters)"
for {set m 0} {$m < 3} {incr m} {
    ooc_count "SLR1-nmu$m" "g_stn\[1\].g_mgr.g_nmu\[$m\].u_nmu" }
puts "@@@ ==== cross-die links"
ooc_count "links-all" "g_link"
puts "@@@ ==== full hierarchy report -> build/line_hier.rpt"
report_utilization -hierarchical -hierarchical_depth 4 \
    -file $root/build/line_hier.rpt
puts "@@@ line_hier done"

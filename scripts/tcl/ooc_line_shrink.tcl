# Ship station bus (sb_line4, FW=256, LINK_FULL=0) with the per-manager NMU
# shrink applied: OUTST cut to serial/DMA reality (JTAG 4, xdma 8, ctrl 2) and
# FORCE_PLACE on the 32-bit single-beat control port. Baseline SLR1 = 8,311 LUT.
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
    -generic OST=4 -generic STORE_FWD=1 \
    -generic MOST0=4 -generic MOST1=8 -generic MOST2=2 -generic MPLC2=1
puts "@@@ ==== device totals (shrunk)"
ooc_count TOTAL
puts "@@@ ==== full hierarchy report -> build/line_shrink.rpt"
report_utilization -hierarchical -hierarchical_depth 4 \
    -file $root/build/line_shrink.rpt
puts "@@@ line_shrink done"
puts "LINE_SHRINK_DONE"

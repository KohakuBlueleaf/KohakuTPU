# OOC synthesis of sb_bd_line4 (the v7 station bus with sb_axi2lite Lite
# converters). SYNTH ONLY -- no opt, no place, no route.
#   vivado -mode batch -source scripts/tcl/ooc_v7bus.tcl

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set_param general.maxThreads 4

source [file join $root scripts tcl ooc_class.tcl]

set stn [file join $root src kohakuaccel axi station]
read_verilog [list \
    [file join $root src kohakuaccel common sync_fifo.v] \
    [file join $root src kohakuaccel common async_fifo.v] \
    [file join $root src kohakuaccel common sb_skid.v] \
    [file join $stn sb_hub.v] \
    [file join $stn sb_station.v] \
    [file join $stn sb_nmu.v] \
    [file join $stn sb_nsu.v] \
    [file join $stn sb_axi2lite.v] \
    [file join $root src kohakuaccel axi link sb_link.v] \
    [file join $root src kohakuaccel axi link sb_link_cdc.v] \
    [file join $root src kohakuaccel axi topo sb_stn_line.v] \
    [file join $root src kohakuaccel axi topo sb_line4.v] \
    [file join $root src kohakuaccel axi bd sb_v6_bus.v]]

read_xdc [file join $root scripts xdc ooc_bd_line4.xdc]

puts "@@@ top sb_bd_line4 (v7 station bus, Lite converters in)"

synth_design -top sb_bd_line4 -part $part -mode out_of_context \
             -flatten_hierarchy none

ooc_record "sb_bd_line4-v7fix" "top=sb_bd_line4 fw=256 aw=43 lite=conv" 2000 2

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ vivado utilization"
ooc_util

puts "@@@ ============================ converters"
foreach k {2 3 6 7 10 11 14 15} {
    ooc_count "u_a2l_$k" "u_a2l_$k"
}

puts "@@@ ============================ stations and managers"
foreach inst {u_line/g_stn[0].u_stn u_line/g_stn[1].u_stn \
              u_line/g_stn[2].u_stn u_line/g_stn[3].u_stn \
              u_line/g_stn[1].g_mgr.g_nmu[0].u_nmu \
              u_line/g_stn[1].g_mgr.g_nmu[1].u_nmu \
              u_line/g_stn[1].g_mgr.g_nmu[2].u_nmu} {
    ooc_count $inst $inst
}

puts "@@@ ============================ control sets"
ooc_ctrlsets

puts "@@@ ============================ Fmax per clock"
ooc_classify 2000

puts "@@@ ooc_v7bus done"

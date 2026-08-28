# D2 station-bus synth: the SHIP recipe of ooc_line_shrink.tcl (FW=256, AW=43,
# LINK_FULL=0, OST 4/8/2, ctrl FORCE_PLACE) at the SHIP shape NQ=4, one run,
# everything to disk under <out>: hier.rpt (depth 4), util.rpt, timing (100
# paths), census.txt, result.txt. Extra -generic pairs may follow <out>.
#   vivado -mode batch -source ooc_line_d2.tcl -tclargs <out> [NAME=VALUE ...]
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set out  [lindex $argv 0]
set extra [lrange $argv 1 end]
file mkdir $out
set_param general.maxThreads 4
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
set cmd [list synth_design -top sb_line4 -part $part -mode out_of_context \
    -flatten_hierarchy none -directive default -generic FW=256 -generic AW=43 \
    -generic LINK_FULL=0 -generic NQ=4 -generic PORTW=2 -generic OST=4 -generic STORE_FWD=1 \
    -generic MOST0=4 -generic MOST1=8 -generic MOST2=2 -generic MPLC2=1]
foreach g $extra { lappend cmd -generic $g }
if {[catch {eval $cmd} err]} { puts "SYNTH FAILED: $err"; exit 1 }

report_utilization -hierarchical -hierarchical_depth 4 -file "$out/hier.rpt"
report_utilization -file "$out/util.rpt"
report_timing -delay_type max -max_paths 100 -nworst 1 -sort_by slack -file "$out/timing.rpt"
report_timing_summary -file "$out/timing_summary.rpt"

set cells [get_cells -quiet -hier -filter "REF_NAME =~ LUT?"]
array set bucket {}
foreach c $cells {
    set n [get_property NAME $c]
    regsub {_i_[0-9]+$} $n "" n
    regsub {__[0-9]+$} $n "" n
    regsub -all {\[[0-9]+\]} $n "" n
    regsub {_reg$} $n "" n
    if {$n eq ""} { set n "(unnamed)" }
    incr bucket($n)
}
set rows {}
foreach {k v} [array get bucket] { lappend rows [list $v $k] }
set rows [lsort -integer -decreasing -index 0 $rows]
set cf [open "$out/census.txt" w]
puts $cf "total [llength $cells] LUT in [llength $rows] signal(s)"
set i 0
foreach r $rows { if {[incr i] > 80} break; puts $cf [format "%6d  %s" [lindex $r 0] [lindex $r 1]] }
close $cf

set lut "?"; set ff "?"; set bram "?"
foreach line [split [report_utilization -return_string] "\n"] {
    if {[regexp {CLB LUTs\*?\s+\|\s+(\d+)} $line -> v]} { set lut $v }
    if {[regexp {CLB Registers\s+\|\s+(\d+)} $line -> v]} { set ff $v }
    if {[regexp {Block RAM Tile\s+\|\s+(\d+)} $line -> v]} { set bram $v }
}
# per-station + SLR1 per-port, from the hierarchy (the number the plan tracks)
set fh [open "$out/result.txt" w]
puts $fh "LUT=$lut FF=$ff BRAM=$bram extra={$extra}"
foreach inst {g_stn[0].u_stn g_stn[1].u_stn g_stn[2].u_stn g_stn[3].u_stn \
              g_stn[1].g_mgr.g_nmu[0].u_nmu g_stn[1].g_mgr.g_nmu[1].u_nmu \
              g_stn[1].g_mgr.g_nmu[2].u_nmu \
              g_stn[1].g_nsu[0].u_nsu g_stn[1].g_nsu[1].u_nsu \
              g_stn[1].g_nsu[2].u_nsu g_stn[1].g_nsu[3].u_nsu} {
    set pfx [string map [list "\[" "\\\[" "\]" "\\\]"] $inst]
    set n [llength [get_cells -quiet -hier -filter "REF_NAME =~ LUT? && NAME =~ $pfx/*"]]
    puts $fh [format "%-36s LUT %6d" $inst $n]
}
close $fh
puts "D2 RESULT LUT=$lut FF=$ff BRAM=$bram"

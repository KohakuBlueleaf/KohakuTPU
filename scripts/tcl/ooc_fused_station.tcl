# Fused single station M x N (fused input engine -> hub -> N subs). Single-station
# M x N compares direct to vendor M x N; a 4-station split = 4 x (M/4 x N/4) + links.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set cm  [file join $root src kohakuaccel common]
set st  [file join $root src kohakuaccel axi station]
set tp  [file join $root src kohakuaccel axi topo]
set files [list \
    [file join $cm async_fifo.v] [file join $cm sync_fifo.v] [file join $cm sb_skid.v] \
    [file join $st sb_hub.v] [file join $st sb_nmu.v] [file join $st sb_nsu.v] \
    [file join $st sb_axi_concentrate.v] [file join $st sb_station_in.v] \
    [file join $tp sb_stn_line.v] \
    [file join $root tests axi sb_fused_station.v]]
proc syn {files part mn} {
    catch {close_design}
    read_verilog $files
    synth_design -top sb_fused_station -part $part -mode out_of_context \
        -generic M=$mn -generic N=$mn -generic DW=512
    create_clock -name clk -period 2.0 [get_ports clk]
    ooc_count "fstn-${mn}x${mn}"
}
foreach mn {1 2 4 8} { syn $files $part $mn }
puts "FSTN_DONE"

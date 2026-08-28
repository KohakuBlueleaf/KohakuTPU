# Fused input engine vs M independent NMUs, one station, single clock, N=8 slaves.
# The delta at M>1 is the in-station SASD fusion win (shared decode+tag+pack).
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set st  [file join $root src kohakuaccel axi station]
set cm  [file join $root src kohakuaccel common]
set files [list \
    [file join $cm async_fifo.v] [file join $st sb_nmu.v] \
    [file join $st sb_axi_concentrate.v] [file join $st sb_station_in.v] \
    [file join $root tests axi station_fuse_probes.v]]

proc syn {files part top M} {
    catch {close_design}
    read_verilog $files
    synth_design -top $top -part $part -mode out_of_context \
        -generic M=$M -generic N=8 -generic DW=512
    ooc_count "$top-M$M"
}
foreach M {1 4 8} {
    syn $files $part indep_probe      $M
    syn $files $part station_in_probe $M
}
puts "FUSE_DONE"

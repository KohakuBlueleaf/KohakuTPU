# Fused station BRAM levers: distributed FIFOs (0 BRAM, LUT cost) and cut-through
# (STORE_FWD=0, less buffering). Baseline (block, SFW=1): 4x4 9,028 LUT/86 BRAM.
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
proc syn {files part tag mn mem sfw} {
    catch {close_design}
    read_verilog $files
    synth_design -top sb_fused_station -part $part -mode out_of_context \
        -generic M=$mn -generic N=$mn -generic DW=512 \
        -generic MEM=$mem -generic SFW=$sfw
    ooc_count "$tag"
}
syn $files $part fstn-4x4-dist     4 distributed 1
syn $files $part fstn-4x4-dist-ct  4 distributed 0
syn $files $part fstn-8x8-dist     8 distributed 1
puts "FSTN2_DONE"

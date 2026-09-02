# The station-bus line at the v8t2 ship knobs (one system clock: LINK_CDC 0,
# JTAG on the bus clock MGR0_DOM 1) against the v7 shape (LINK_CDC 1, JTAG on
# ctrl), both at NQ=4 / FW=256 / AW=43 / LINK_FULL=0. One run, every number:
# totals, Fmax per clock, the per-station hierarchy, per-station primitives.
#   vivado -mode batch -source scripts/tcl/ooc_line4_sysclk.tcl
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 8
source [file join $root scripts tcl ooc_class.tcl]
set common [file join $root src kohakuaccel common]
set stn    [file join $root src kohakuaccel axi station]
set link   [file join $root src kohakuaccel axi link]
set topo   [file join $root src kohakuaccel axi topo]
set kts    [file join $root src kohakutransmit]

proc synth_line {root common stn link topo kts part tag xdc extra} {
    catch {close_design}
    read_verilog [list \
        [file join $common sync_fifo.v] [file join $common async_fifo.v] \
        [file join $common sb_skid.v] [file join $stn sb_hub.v] \
        [file join $stn sb_station.v] [file join $stn sb_nmu.v] [file join $stn sb_nsu.v] \
        [file join $link sb_link.v] [file join $link sb_link_cdc.v] [file join $link sb_link_kts.v] \
        [file join $kts prim kts_fifo.v] [file join $kts link kts_tx.v] \
        [file join $kts link kts_rx.v] [file join $kts carrier kts_pipe.v] \
        [file join $topo sb_stn_line.v] [file join $topo sb_line4.v]]
    read_xdc [file join $root scripts xdc $xdc]
    set cmd "synth_design -top sb_line4 -part $part -mode out_of_context \
        -flatten_hierarchy none -directive default \
        -generic FW=256 -generic AW=43 -generic NQ=4 -generic PORTW=2 \
        -generic LINK_FULL=0 -generic OST=4 -generic STORE_FWD=1 $extra"
    eval $cmd
    set out [file join $root build ooc line4_$tag]
    file mkdir $out
    puts "@@@ ==== $tag totals"
    ooc_record $tag "cfg=$tag" 2000 3
    ooc_count "$tag" ""
    # A generate scope's cells are `g_stn[s].u_stn/...`: the dot, not a slash,
    # follows the bracket, so the prefix needs the `.*`.
    foreach s {0 1 2 3} {
        ooc_count "$tag stn$s" "g_stn\[$s\].*"
    }
    ooc_count "$tag links" "g_link*"
    ooc_count "$tag nmu0" "g_stn\[1\].g_mgr.g_nmu\[0\].*"
    report_utilization -hierarchical -hierarchical_depth 4 -file $out/hier.rpt
    report_utilization -file $out/util.rpt
    report_timing_summary -file $out/timing.rpt
    write_checkpoint -force $out/synth.dcp
}

synth_line $root $common $stn $link $topo $kts $part v7shape ooc_line4.xdc \
    "-generic LINK_CDC=1 -generic MGR0_DOM=0"
synth_line $root $common $stn $link $topo $kts $part sysclk ooc_line4_sysclk.xdc \
    "-generic LINK_CDC=0 -generic MGR0_DOM=1"
puts "LINE4_SYSCLK_DONE"

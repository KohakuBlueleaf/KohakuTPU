# Ship shape is NQ=4 (v6: mesh MEM, mesh CTRL, DDR, clk-wiz per station), NOT the
# sb_line4 default NQ=2. Measure baseline vs the NMU shrink at the REAL ship shape,
# both with a per-die hierarchy report. FW=256, LINK_FULL=0.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set common [file join $root src kohakuaccel common]
set stn    [file join $root src kohakuaccel axi station]
set link   [file join $root src kohakuaccel axi link]
set topo   [file join $root src kohakuaccel axi topo]

proc synth_line {root common stn link topo part tag nq extra} {
    catch {close_design}
    read_verilog [list \
        [file join $common sync_fifo.v] [file join $common async_fifo.v] \
        [file join $common sb_skid.v] [file join $stn sb_hub.v] \
        [file join $stn sb_station.v] [file join $stn sb_nmu.v] [file join $stn sb_nsu.v] \
        [file join $link sb_link.v] [file join $link sb_link_cdc.v] \
        [file join $topo sb_stn_line.v] [file join $topo sb_line4.v]]
    read_xdc [file join $root scripts xdc ooc_line4.xdc]
    set cmd "synth_design -top sb_line4 -part $part -mode out_of_context \
        -flatten_hierarchy none -directive default \
        -generic FW=256 -generic AW=43 -generic NQ=$nq -generic PORTW=2 \
        -generic LINK_FULL=0 -generic OST=4 -generic STORE_FWD=1 $extra"
    eval $cmd
    puts "@@@ ==== $tag totals"
    ooc_count "$tag" ""
    report_utilization -hierarchical -hierarchical_depth 4 \
        -file $root/build/line_nq4_$tag.rpt
}

synth_line $root $common $stn $link $topo $part base   4 ""
synth_line $root $common $stn $link $topo $part shrunk 4 \
    "-generic MOST0=4 -generic MOST1=8 -generic MOST2=2 -generic MPLC2=1"
puts "LINE_NQ4_DONE"

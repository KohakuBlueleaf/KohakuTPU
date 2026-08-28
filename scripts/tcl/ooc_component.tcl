# Isolated OOC synth of ONE station-bus component, to build a per-port cost model.
# Area only (no clocks): the calculator sums these; Fmax is measured on the line.
#   -tclargs <module> <dw> <fw> <tag> [midw] [ost]
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set mod  [lindex $argv 0]
set dw   [lindex $argv 1]
set fw   [lindex $argv 2]
set tag  [lindex $argv 3]
set midw [lindex $argv 4]
set ost  [lindex $argv 5]
if {$fw   eq ""} { set fw   256 }
if {$dw   eq ""} { set dw   256 }
if {$midw eq ""} { set midw 4 }
if {$ost  eq ""} { set ost  4 }

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakuaccel common sync_fifo.v] \
    [file join $root src kohakuaccel common async_fifo.v] \
    [file join $root src kohakuaccel common sb_skid.v] \
    [file join $root src kohakuaccel axi station sb_nmu.v] \
    [file join $root src kohakuaccel axi station sb_nsu.v] \
    [file join $root src kohakuaccel axi station sb_nmu_lite.v] \
    [file join $root src kohakuaccel axi station sb_nsu_lite.v] \
    [file join $root src kohakuaccel axi station sb_axi2lite.v] \
    [file join $root src kohakuaccel axi station sb_hub.v] \
    [file join $root src kohakuaccel axi link sb_link.v] \
    [file join $root src kohakuaccel axi link sb_link_cdc.v]]

# Per-module width generic. NMU takes MW, NSU takes SDW; both take FW/AW.
set g {}
switch -glob -- $mod {
    sb_nmu       { set g "-generic MW=$dw -generic MIDW=$midw -generic FW=$fw -generic AW=43 -generic TAGW=4 -generic STORE_FWD=1 -generic NSEG=1 -generic SEG_VLD=1" }
    sb_nmu_lite  { set g "-generic MW=$dw -generic MIDW=$midw -generic FW=$fw -generic AW=43 -generic TAGW=4 -generic NSEG=1 -generic SEG_VLD=1" }
    sb_nsu       { set g "-generic SDW=$dw -generic SIDW=$midw -generic FW=$fw -generic AW=43 -generic TAGW=4 -generic WOST=$ost -generic ROST=$ost" }
    sb_nsu_lite  { set g "-generic SDW=$dw -generic SIDW=$midw -generic FW=$fw -generic AW=43 -generic TAGW=4" }
    sb_link      { set g "-generic W=$dw" }
    sb_link_cdc  { set g "-generic W=$dw" }
    default      { set g "" }
}

synth_design -top $mod -part $part -mode out_of_context \
             -flatten_hierarchy none -directive default {*}$g

ooc_record $tag "top=$mod dw=$dw fw=$fw midw=$midw ost=$ost" 2000 2
puts "@@@ component done $tag"

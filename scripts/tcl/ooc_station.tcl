# OOC synthesis of sb_root9. SYNTH ONLY -- no opt, no place, no route.
#   vivado -mode batch -source scripts/tcl/ooc_station.tcl

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set top  [lindex $argv 0]
set dirv [lindex $argv 1]
set pset [lindex $argv 2]
set lpb  [lindex $argv 3]
set tagw [lindex $argv 4]
set ost  [lindex $argv 5]
set sfwd [lindex $argv 6]
if {$lpb  eq ""} { set lpb  0 }
if {$tagw eq ""} { set tagw 4 }
if {$ost  eq ""} { set ost  4 }
if {$sfwd eq ""} { set sfwd 1 }
set fw [lindex $argv 7]
if {$fw eq ""} { set fw 512 }
set tmo [lindex $argv 8]
if {$tmo eq ""} { set tmo 0 }
set aw [lindex $argv 9]
if {$aw eq ""} { set aw 43 }
# sb_link_pair carries whole flits, so its widths follow FW and AW both.
set lrq [expr {2 + 3 + 4 + 3 + $aw + 8 + 3 + $fw + $fw/8}]
set lrs [expr {2 + 4 + 2 + 2 + $fw}]
if {$top  eq ""} { set top  sb_root9 }
if {$dirv eq ""} { set dirv default }
if {$pset eq ""} { set pset 0 }

set_param general.maxThreads 4

source [file join $root scripts tcl ooc_class.tcl]

set common [file join $root src kohakuaccel common]
set stn    [file join $root src kohakuaccel axi station]
set link   [file join $root src kohakuaccel axi link]
set topo   [file join $root src kohakuaccel axi topo]
set bd     [file join $root src kohakuaccel axi bd]

read_verilog [list \
    [file join $common sync_fifo.v] \
    [file join $common async_fifo.v] \
    [file join $common sb_skid.v] \
    [file join $stn sb_hub.v] \
    [file join $stn sb_station.v] \
    [file join $stn sb_nmu.v] \
    [file join $stn sb_nsu.v] \
    [file join $topo sb_root9.v] \
    [file join $root src kohakutransmit prim kts_fifo.v] \
    [file join $root src kohakutransmit link kts_tx.v] \
    [file join $root src kohakutransmit link kts_rx.v] \
    [file join $root src kohakutransmit carrier kts_pipe.v] \
    [file join $root src kohakutransmit prim kts_afifo.v] \
    [file join $root src kohakutransmit carrier kts_cdc.v] \
    [file join $link sb_link.v] \
    [file join $link sb_link_kts.v] \
    [file join $link sb_link_pair.v] \
    [file join $topo sb_slr1.v] \
    [file join $topo sb_leaf.v] \
    [file join $topo sb_stn_root.v] \
    [file join $topo sb_stn_leaf.v] \
    [file join $topo sb_quad.v] \
    [file join $link sb_link_cdc.v] \
    [file join $topo sb_stn_line.v] \
    [file join $topo sb_line4.v] \
    [file join $bd sb_bd_root.v] \
    [file join $bd sb_bd_leaf.v] \
    [file join $bd sb_bd_link.v] \
    [file join $topo sb_chain2.v]]

# ooc_sweep.py build_ports() emits one wrapper per K x Q shape; read whichever
# exist so a port sweep does not need this list edited per shape.
foreach f [glob -nocomplain [file join $root src attic sweeps sb_p*x*.v]] {
    read_verilog $f
}

if {$top eq "sb_link_pair"} {
    read_xdc [file join $root scripts xdc ooc_link.xdc]
} elseif {$top eq "sb_quad"} {
    read_xdc [file join $root scripts xdc ooc_quad.xdc]
} elseif {$top eq "sb_line4"} {
    read_xdc [file join $root scripts xdc ooc_line4.xdc]
} elseif {[string match "sb_bd_*" $top]} {
    read_xdc [file join $root scripts xdc ooc_[string map {sb_ {}} $top].xdc]
} else {
    read_xdc [file join $root scripts xdc ooc_station.xdc]
}

set lfull [lindex $argv 11]
set lmem  [lindex $argv 12]
set iskid   [lindex $argv 13]
set cfgonly [lindex $argv 14]
if {$lfull eq ""} { set lfull 1 }
if {$lmem  eq ""} { set lmem distributed }
if {$iskid eq ""} { set iskid 0 }
if {$cfgonly eq ""} { set cfgonly 0 }
# These generics exist only on specific tops; add them just there.
set gextra {}
set lcdc [lindex $argv 16]
if {$lcdc eq ""} { set lcdc 0 }
set kts [lindex $argv 15]
if {$kts eq ""} { set kts 0 }
if {$top eq "sb_line4" || $top eq "sb_quad"} {
    set gextra [list -generic LINK_FULL=$lfull -generic LINK_MEM=$lmem \
                     -generic LINK_CDC=$lcdc -generic LINK_KTS=$kts]
}
if {$top eq "sb_slr1"} {
    set gextra [list -generic ISKID=$iskid -generic CFG_ONLY=$cfgonly]
}
if {$top eq "sb_link_pair"} {
    set gextra [list -generic KTS=$kts]
}
puts "@@@ top $top dir $dirv preset $pset lpb $lpb tagw $tagw ost $ost sf $sfwd fw $fw tmo $tmo lfull $lfull lmem $lmem"

# flatten none: the per-instance breakdown is the point -- which of NMU, NSU or
# station carries the cost is the whole question the design makes a claim about.
synth_design -top $top -part $part -mode out_of_context \
             -flatten_hierarchy none -directive $dirv \
             -generic PRESET=$pset -generic LUT_PER_BRAM=$lpb \
             -generic TAGW=$tagw -generic OST=$ost \
             -generic STORE_FWD=$sfwd -generic FW=$fw -generic TIMEOUT=$tmo \
             -generic AW=$aw -generic REQ_W=$lrq -generic RSP_W=$lrs {*}$gextra

ooc_record "$top-$pset-fw$fw-ost$ost-sf$sfwd-lpb$lpb-aw$aw-kts$kts-cdc$lcdc-lf$lfull" \
    "top=$top preset=$pset fw=$fw aw=$aw ost=$ost sfwd=$sfwd lpb=$lpb tmo=$tmo kts=$kts cdc=$lcdc lfull=$lfull" \
    2000 2

puts "@@@ ============================ device totals"
ooc_count TOTAL

# Primitive counts and Vivado's own site accounting, side by side: the LUT-equiv
# figures in the doc are only comparable to a vendor report if these agree.
puts "@@@ ============================ vivado utilization"
ooc_util

puts "@@@ ============================ by primitive"
foreach p {LUT1 LUT2 LUT3 LUT4 LUT5 LUT6 FDRE FDSE FDCE FDPE RAMD32 RAMD64E \
           RAMS32 SRL16E SRLC32E RAMB36E2 RAMB18E2 MUXF7 MUXF8 CARRY8} {
    set n [llength [get_cells -hier -filter "REF_NAME == $p"]]
    if {$n} { puts [format "@@@ %-10s %8d" $p $n] }
}

puts "@@@ ============================ per instance"
if {$top eq "sb_line4"} {
    set insts {g_stn[0].u_stn g_stn[1].u_stn g_stn[2].u_stn g_stn[3].u_stn \
               g_stn[1].g_mgr.g_nmu[0].u_nmu g_stn[1].g_mgr.g_nmu[1].u_nmu \
               g_stn[1].g_mgr.g_nmu[2].u_nmu \
               g_stn[0].g_nsu[0].u_nsu g_stn[0].g_nsu[1].u_nsu \
               g_link[0].g_sync.u_rq_fwd g_link[0].g_sync.u_rs_fwd \
               g_link[0].g_cdc.u_rq_fwd g_link[0].g_cdc.u_rs_fwd}
} elseif {$top eq "sb_quad"} {
    set insts {u_root u_root/u_stn u_root/u_nmu0 u_root/u_nmu1 u_root/u_nmu2 \
               g_leaf[0].u_leaf g_leaf[1].u_leaf g_leaf[2].u_leaf \
               g_leaf[0].u_lk_req g_leaf[0].u_lk_rsp}
} else {
    set insts {u_stn u_nmu0 u_nmu1 u_nmu2 g_nsu[0].u_nsu g_nsu[1].u_nsu \
               g_nsu[2].u_nsu g_nsu[3].u_nsu g_nsu[4].u_nsu g_nsu[5].u_nsu \
               g_nsu[6].u_nsu g_nsu[7].u_nsu g_nsu[8].u_nsu}
}
foreach inst $insts {
    if {[llength [get_cells -quiet $inst]] == 0} {
        puts "@@@ $inst MISSING"
        continue
    }
    ooc_count $inst $inst
}

puts "@@@ ============================ control sets"
ooc_ctrlsets

# Opt-in: the per-cell pin walk takes ~10 minutes on a 26k-FF netlist.
# argv 10, not 9 -- 9 is `aw`, so this was never selectable on its own.
if {[lindex $argv 10] eq "1"} {
    puts "@@@ ============================ reset endpoints"
    ooc_resets
}

puts "@@@ ============================ Fmax per clock"
ooc_classify 2000

puts "@@@ ooc_station done $top $dirv preset $pset"

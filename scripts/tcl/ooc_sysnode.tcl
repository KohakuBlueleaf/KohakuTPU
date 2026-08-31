# What the system node costs, whole: the RV64 complex, the mover, the transform
# slot and MAG, synthesised as one top. THE GATE IS THIS RUN, not a projection
# off any part of it.
#
#   vivado -mode batch -source scripts/tcl/ooc_sysnode.tcl -tclargs <generics> [<tag>]
#
# <generics> is NAME:VALUE pairs joined by `+` (vivado.bat splits on `,` and
# `=`), e.g. PORTS:2+ILINK:0+DRAM_CDC:0+STAGE_BANKS:4. Anything not named
# keeps the RTL default, which is the shipping value. <tag> names the report
# set under build/; it defaults to the generics joined by `_`.
#
# EVERYTHING GOES TO DISK, ONE RUN PER CONFIG: utilization, the hierarchy to
# depth 8, the timing summary, the 2,000 worst setup paths, the checkpoint.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set gen_arg [lindex $argv 0]
set tag     [lindex $argv 1]
# Optional third arg: the synth_design directive (default "default"), so an
# area directive is one measured config beside the RTL knobs, never a rebase.
set sdir    [lindex $argv 2]
if {$sdir eq ""} { set sdir default }
# Optional fourth arg: an extra XDC read BEFORE synth_design, for block-level
# synthesis strategies (BLOCK_SYNTH.*) on named cells.
set sxdc    [lindex $argv 3]

set generics {}
set parts {}
foreach kv [split $gen_arg "+"] {
    if {$kv eq ""} { continue }
    lassign [split $kv ":"] k v
    if {$k eq "" || $v eq ""} { error "generic '$kv' is not NAME:VALUE" }
    lappend generics -generic "${k}=${v}"
    lappend parts "${k}${v}"
}
if {$tag eq ""} {
    set tag [expr {[llength $parts] ? "sn_[join $parts _]" : "sn_default"}]
}
set out [file join $root build "node_${tag}"]

set part xcvu13p-fhgb2104-2L-e
create_project -in_memory -part $part

read_verilog [list \
    [file join $root src kohakuaccel common sync_fifo.v] \
    [file join $root src kohakuaccel common async_fifo.v] \
    [file join $root src kohakuaccel common sb_skid.v] \
    [file join $root src kohakuaccel common kohaku_sdpram.v] \
    [file join $root src kohakuaccel common kohaku_sdpram_be.v] \
    [file join $root src kohakuaccel noc ctrl noc_orchestrator.v] \
    [file join $root src kohakuaccel noc endpoint noc_cu_base.v] \
    [file join $root src kohakutpu transform mx_quant.v] \
    [file join $root src kohakutpu transform xform_bank.v] \
    [file join $root src kohakuaccel sysnode core mag_xform.v] \
    [file join $root src kohakuaccel sysnode mover mx_tdesc.v] \
    [file join $root src kohakuaccel sysnode mover mm_prng.v] \
    [file join $root src kohakuaccel sysnode mover mm_mover.v] \
    [file join $root src kohakuaccel sysnode mover mv_exec.v] \
    [file join $root src kohakuaccel sysnode core mag_stage.v] \
    [file join $root src kohakuaccel sysnode core mag_mem_port.v] \
    [file join $root src kohakuaccel sysnode core mag_dram_port.v] \
    [file join $root src kohakutransmit prim kts_fifo.v] \
    [file join $root src kohakutransmit link kts_tx.v] \
    [file join $root src kohakutransmit link kts_rx.v] \
    [file join $root src kohakutransmit carrier kts_pipe.v] \
    [file join $root src kohakuaccel sysnode interlink il_pkt_arb.v] \
    [file join $root src kohakuaccel sysnode interlink mag_link.v] \
    [file join $root src kohakuaccel sysnode interlink mag_switch.v] \
    [file join $root src kohakuaccel sysnode interlink mag_ilink.v] \
    [file join $root src kohakuaccel sysnode core sn_hub.v] \
    [file join $root src kohakuaccel sysnode core mag.v] \
    [file join $root src kohakuaccel sysnode sysnode.v]]

read_verilog [glob \
        [file join $root src kohakuaccel pe rv64-sys *.v] \
        [file join $root src kohakuaccel pe rv64-sys core *.v]]
read_verilog [list \
    [file join $root src kohakuaccel sysnode cpu rv64_mag_pe.v] \
    [file join $root src kohakuaccel sysnode cpu rv64_load_win.v] \
    [file join $root src kohakuaccel sysnode cpu rv64_load_axi.v] \
    [file join $root src kohakuaccel axi station sb_axi_deconcentrate.v]]

read_xdc [file join $root scripts xdc ooc_sysnode.xdc]
if {$sxdc ne ""} { read_xdc [file join $root $sxdc] }

# STAGE=1 / STAGE_AT_PORT=1 are the node as shipped: one store on the
# converged path. At STAGE_AT_PORT=0 every memory port carries its own 64 URAM
# and none of them is reachable by the mover or the interlink.
synth_design -top sysnode -mode out_of_context -part $part -directive $sdir \
    -generic STAGE=1 -generic STAGE_AT_PORT=1 {*}$generics

if {[llength [get_clocks -quiet]] == 0} {
    error "no clock: ooc_sysnode.xdc did not apply, so every figure would be unconstrained"
}
report_utilization -quiet -file "${out}_util.rpt"
report_utilization -hierarchical -hierarchical_depth 8 -quiet -file "${out}_hier.rpt"
report_timing_summary -quiet -file "${out}_time.rpt"
report_timing -max_paths 2000 -nworst 1 -setup -quiet -file "${out}_paths.rpt"
write_checkpoint -force "${out}.dcp"

set nlut ?; set nff ?; set nbram ?; set ndsp ?; set nuram ?
set u [report_utilization -return_string]
regexp {CLB LUTs\D+(\d+)}          $u -> nlut
regexp {CLB Registers\D+(\d+)}     $u -> nff
regexp {Block RAM Tile\D+([\d.]+)} $u -> nbram
regexp {URAM\D+(\d+)}              $u -> nuram
regexp {DSPs\D+(\d+)}              $u -> ndsp
set slk [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]

puts "@@@ NODE TAG=$tag GEN=$gen_arg DIR=$sdir XDC=$sxdc LUT=$nlut FF=$nff BRAM=$nbram URAM=$nuram DSP=$ndsp WNS=$slk"

# Every failing path grouped by start/end register, with its logic level: a
# level count above 11 is the thing to fix, not the slack.
set bad [get_timing_paths -max_paths 2000 -nworst 1 -setup -slack_lesser_than 0]
puts "@@@FAILN [llength $bad]"

proc base {pin} {
    set c [file dirname $pin]
    regsub {\[[0-9]+\]$} $c "" c
    return $c
}

array set grp {}
foreach p $bad {
    set k "[base [get_property STARTPOINT_PIN $p]] -> [base [get_property ENDPOINT_PIN $p]]"
    set s [get_property SLACK $p]
    set l [get_property LOGIC_LEVELS $p]
    if {[info exists grp($k)]} {
        lassign $grp($k) cnt worst lvl
        set grp($k) [list [expr {$cnt + 1}] [expr {$s < $worst ? $s : $worst}] \
                          [expr {$l > $lvl ? $l : $lvl}]]
    } else {
        set grp($k) [list 1 $s $l]
    }
}
set rows {}
foreach k [array names grp] {
    lassign $grp($k) cnt worst lvl
    lappend rows [list $worst $cnt $lvl $k]
}
foreach r [lsort -real -index 0 $rows] {
    lassign $r worst cnt lvl k
    puts [format "@@@GROUP %4d paths  worst %+7.3f  lvl %3s  %s" $cnt $worst $lvl $k]
}

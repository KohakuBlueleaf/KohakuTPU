# Place and route the system node out of context inside ONE SLR -- the routed
# proof a URAM chain in the staging store needs; synthesis cannot see a cascade.
#   -tclargs <tag> <generics NAME:VALUE+...|-> [slr 0-3] [period_ns]
# Everything to build/impl_sysnode_<tag>/: congestion after place and after
# route, utilization, hierarchy, every timing path, the DCP.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set tag   [lindex $argv 0]
set gspec [lindex $argv 1]
set slr   [lindex $argv 2]
set per   [lindex $argv 3]
if {$tag   eq ""} { set tag   ship }
if {$gspec eq ""} { set gspec - }
if {$slr   eq ""} { set slr   1 }
if {$per   eq ""} { set per   3.333 }
set out $root/build/impl_sysnode_$tag
file mkdir $out

set_param general.maxThreads 8

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

set generics {}
if {$gspec ne "-"} {
    foreach kv [split $gspec "+"] {
        set p [split $kv ":"]
        lappend generics "[lindex $p 0]=[lindex $p 1]"
    }
}
puts "@@@ impl_sysnode $tag slr $slr period $per generics {$generics}"
set cmd [list synth_design -top sysnode -part $part -mode out_of_context \
             -generic STAGE=1 -generic STAGE_AT_PORT=1]
foreach g $generics { lappend cmd -generic $g }
if {[catch {eval $cmd} err]} { puts "SYNTH FAILED: $err"; exit 1 }
report_utilization -file $out/util_synth.rpt
read_xdc [file join $root scripts xdc ooc_sysnode.xdc]
if {[llength [get_clocks -quiet]] == 0} {
    error "no clock: ooc_sysnode.xdc did not apply, so every figure would be unconstrained"
}

set rows {0 {Y0 Y3} 1 {Y4 Y7} 2 {Y8 Y11} 3 {Y12 Y15}}
lassign [dict get $rows $slr] ylo yhi
create_pblock pb_slr
resize_pblock [get_pblocks pb_slr] -add "CLOCKREGION_X0${ylo}:CLOCKREGION_X7${yhi}"
add_cells_to_pblock [get_pblocks pb_slr] [get_cells -hier -filter {PARENT == ""}]
set_property CONTAIN_ROUTING false [get_pblocks pb_slr]

opt_design
place_design
report_design_analysis -congestion -file $out/congestion_place.rpt
foreach line [split [report_design_analysis -congestion -return_string] "\n"] {
    if {[regexp -nocase {level|congest|\| *(North|South|East|West)} $line]} { puts "@@@ place $line" }
}
phys_opt_design
route_design
report_design_analysis -congestion -file $out/congestion_route.rpt
foreach line [split [report_design_analysis -congestion -return_string] "\n"] {
    if {[regexp -nocase {level|congest|\| *(North|South|East|West)} $line]} { puts "@@@ route $line" }
}
# The checkpoint first: a report that errors must not lose the routed design.
write_checkpoint -force $out/routed.dcp
# Whole-design totals BEFORE the other reports: taken afterwards the plain
# report_utilization -return_string read 0 for every row.
set lut "?"; set ff "?"; set uram "?"; set bram "?"
foreach line [split [report_utilization -return_string] "\n"] {
    if {[regexp {CLB LUTs\*?\s+\|\s+(\d+)} $line -> v]} { set lut $v }
    if {[regexp {CLB Registers\s+\|\s+(\d+)} $line -> v]} { set ff $v }
    if {[regexp {\|\s*URAM\s+\|\s+(\d+)} $line -> v]} { set uram $v }
    if {[regexp {Block RAM Tile\s+\|\s+(\d+)} $line -> v]} { set bram $v }
}
foreach {what cmd} [list \
    route_status  [list report_route_status -file $out/route_status.rpt] \
    util          [list report_utilization -file $out/util.rpt] \
    hier          [list report_utilization -hierarchical -hierarchical_depth 4 -file $out/hier.rpt] \
    timing_sum    [list report_timing_summary -file $out/timing_summary.rpt] \
    timing        [list report_timing -delay_type max -max_paths 100 -nworst 1 -sort_by slack -file $out/timing.rpt]] {
    if {[catch {eval $cmd} err]} { puts "@@@ WARNING report $what failed: $err" }
}

set wns [get_property SLACK [get_timing_paths -max_paths 1 -setup]]
set fh [open "$out/result.txt" w]
puts $fh "ROUTED tag=$tag LUT=$lut FF=$ff URAM=$uram BRAM=$bram WNS=[format %.3f $wns] generics={$generics}"
close $fh
puts "ROUTED tag=$tag LUT=$lut FF=$ff URAM=$uram BRAM=$bram WNS=[format %.3f $wns]"
puts "@@@ impl_sysnode done $tag -> $out"

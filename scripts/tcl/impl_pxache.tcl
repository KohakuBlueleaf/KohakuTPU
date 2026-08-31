# Place and route kx_pxache out of context, one home per SLR -- the routed proof
# a URAM cascade needs: synth reported the same Fmax at every chain depth,
# including the 8-deep one that routed at UG949 congestion level 6.
#   -tclargs <tag> <generics NAME:VALUE+...|-> [period_ns]
# Everything to build/impl_pxache_<tag>/: congestion after place and after
# route, per-pblock and hierarchical utilization, every timing path, the DCP.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set tag   [lindex $argv 0]
set gspec [lindex $argv 1]
set per   [lindex $argv 2]
if {$tag   eq ""} { set tag   d4 }
if {$gspec eq ""} { set gspec - }
if {$per   eq ""} { set per   3.333 }
set out $root/build/impl_pxache_$tag
file mkdir $out

set_param general.maxThreads 8

foreach f {
    src/kohakuaccel/common/kohaku_sdpram.v
    src/kohakuaccel/common/kohaku_sdpram_be.v
    src/kohakuaccel/common/async_fifo.v
    src/kohakuaccel/common/sync_fifo.v
    src/kohakuaxi/xache/edge/kx_scdc.v
    src/kohakuaxi/xache/edge/kx_link.v
    src/kohakuaxi/xache/edge/kx_perm.v
    src/kohakuaxi/xache/array/kx_carray.v
    src/kohakuaxi/xache/engine/kx_rd_pipe.v
    src/kohakuaxi/xache/engine/kx_wr_engine.v
    src/kohakuaxi/pxache/lane/kx_hop.v
    src/kohakuaxi/pxache/lane/kx_lane.v
    src/kohakuaxi/pxache/kx_pxache.v
} { read_verilog [file join $root $f] }

set generics {}
if {$gspec ne "-"} {
    foreach kv [split $gspec "+"] {
        set p [split $kv ":"]
        lappend generics "[lindex $p 0]=[lindex $p 1]"
    }
}
puts "@@@ impl_pxache $tag period $per generics {$generics}"
set cmd [list synth_design -top kx_pxache -part $part -mode out_of_context]
foreach g $generics { lappend cmd -generic $g }
if {[catch {eval $cmd} err]} { puts "SYNTH FAILED: $err"; exit 1 }
report_utilization -file $out/util_synth.rpt

# clocks: clk plus every m_clk / h_clk bit, all asynchronous (as ooc_kx.tcl)
set clkports [concat [get_ports -quiet clk] \
                     [get_ports -quiet -filter {DIRECTION == IN} m_clk*] \
                     [get_ports -quiet -filter {DIRECTION == IN} h_clk*]]
set names {}
foreach cp $clkports {
    set nm [get_property NAME $cp]
    create_clock -name $nm -period $per $cp
    lappend names $nm
}
if {[llength $names] > 1} {
    set grp {}
    foreach nm $names { lappend grp -group [get_clocks $nm] }
    set_clock_groups -asynchronous {*}$grp
}

# The BD places partition p on SLR p. Masters, homes and their edges pin to
# their partition; a lane's hop t straddles boundary t -- u_tx in the sending
# partition, u_rx in the receiving one (kx_lane.v:7) -- so every crossing is
# one register-to-register net over one boundary. Pinning only the homes let
# the unpinned lanes collapse to one side and put 1,448 nets across THREE SLRs.
set rows {0 {Y0 Y3} 1 {Y4 Y7} 2 {Y8 Y11} 3 {Y12 Y15}}
set NP 4
for {set p 0} {$p < $NP} {incr p} {
    lassign [dict get $rows $p] ylo yhi
    create_pblock pb_slr$p
    resize_pblock [get_pblocks pb_slr$p] \
        -add "CLOCKREGION_X0${ylo}:CLOCKREGION_X7${yhi}"
    set_property CONTAIN_ROUTING false [get_pblocks pb_slr$p]
}
proc pin {p what cells} {
    if {[llength $cells]} {
        add_cells_to_pblock [get_pblocks pb_slr$p] $cells
        puts "@@@ pin SLR$p [llength $cells] cells: $what"
    } else {
        puts "@@@ WARNING pin SLR$p matched no cells: $what"
    }
}
for {set p 0} {$p < $NP} {incr p} {
    pin $p "partition $p own logic" [get_cells -quiet -hier -filter \
        "(NAME =~ g_home\[$p\].* || NAME =~ g_hedge\[$p\].* || NAME =~ g_m\[$p\].* || NAME =~ g_medge\[$p\].*) && NAME !~ g_home\[$p\].g_up.* && NAME !~ g_home\[$p\].g_dn.* && NAME !~ g_m\[$p\].g_up.* && NAME !~ g_m\[$p\].g_dn.*"]
    foreach {dir sgn} {g_up 1 g_dn -1} {
        for {set t 0} {$t < $NP} {incr t} {
            set src [expr {$p + $sgn * $t}]
            set dst [expr {$p + $sgn * ($t + 1)}]
            if {$dst < 0 || $dst >= $NP} { break }
            # `=~` is a GLOB here: `u_tx.*` never matches `u_tx/...`
            foreach own {g_m g_home} {
                pin $src "${own}\[$p\].$dir tap $t tx" [get_cells -quiet -hier -filter \
                    "NAME =~ ${own}\[$p\].$dir.*/g_t\[$t\].u_h/u_tx*"]
                pin $dst "${own}\[$p\].$dir tap $t rx" [get_cells -quiet -hier -filter \
                    "NAME =~ ${own}\[$p\].$dir.*/g_t\[$t\].u_h/u_rx*"]
            }
        }
    }
}

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
# The checkpoint first: a report that errors must not lose the routed design
# (-pblocks takes ONE pblock; the first run died here after a clean route).
write_checkpoint -force $out/routed.dcp
# Whole-design totals BEFORE the per-pblock reports: after them a plain
# report_utilization -return_string came back scoped to the last pblock.
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
foreach pb [get_pblocks] {
    if {[catch {report_utilization -pblocks $pb -append -file $out/util_pblocks.rpt} err]} {
        puts "@@@ WARNING report pblock $pb failed: $err"
    }
}

set wns [get_property SLACK [get_timing_paths -max_paths 1 -setup]]
set fmax [expr {1000.0 / ($per - $wns)}]
set fh [open "$out/result.txt" w]
puts $fh "ROUTED tag=$tag LUT=$lut FF=$ff URAM=$uram BRAM=$bram WNS=[format %.3f $wns] Fmax=[format %.1f $fmax] generics={$generics}"
close $fh
puts "ROUTED tag=$tag LUT=$lut FF=$ff URAM=$uram BRAM=$bram WNS=[format %.3f $wns] Fmax=[format %.1f $fmax]"
puts "@@@ impl_pxache done $tag -> $out"

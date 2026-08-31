# Full observation of the SYNTHESIZED design, checked against the netlist and
# the plan: clocks, pins, every pblock line re-evaluated, the Xache's leaves
# claimed by exactly one die, the reset audit, the address map read back.
# The goal ends here: nothing is implemented.

open_run synth_1 -name v8t_synth
set out $root/build
file mkdir $out
set top ${design_name}_i
proc v8_slurp {path} {
    if {![file exists $path]} { error "v8t analyze: $path missing" }
    set fh [open $path r] ; set t [read $fh] ; close $fh
    return $t
}
# Every pblock line is `add_cells_to_pblock [get_pblocks PB] [get_cells -quiet
# ARGS]`; ARGS is re-evaluated here, so what is checked is what was applied.
set ::V8_PB {}
foreach ln [split [v8_slurp $out/${design_name}_pblocks.xdc] \n] {
    if {[regexp {^add_cells_to_pblock \[get_pblocks (\S+)\] \[get_cells -quiet (.*)\]\s*$} $ln all pb args]} {
        lappend ::V8_PB [list $pb $args]
    }
}
set ::V8_CLKPINS {}
foreach {all pins} [regexp -all -inline {get_pins \{([^\}]+)\}} \
                    [v8_slurp $out/${design_name}_clocks.xdc]] {
    lappend ::V8_CLKPINS {*}$pins
}
set v8_fail 0
proc v8_bad {msg} { global v8_fail ; incr v8_fail ; puts "@@@ FAIL $msg" }
proc v8_cells {args} { return [eval get_cells -quiet $args] }

# ---- 1. clocks -----------------------------------------------------------
puts "\n=== clocks ==="
set clks [get_clocks -quiet]
puts [format "%-52s %9s %9s" clock period MHz]
foreach c [lsort -dictionary [get_property NAME $clks]] {
    set p [get_property PERIOD [get_clocks $c]]
    puts [format "%-52s %9.3f %9.1f" $c $p [expr {$p > 0 ? 1000.0 / $p : 0}]]
}
puts "@@@ clocks [llength $clks]"
# ctrl, sys, 4 bus, 4 MIG (+ their generated), xdma, pcie, sys_clk
if {[llength $clks] < 12} { v8_bad "only [llength $clks] clocks" }
if {![llength [get_clocks -quiet sys_clk]]} {
    v8_bad "sys_clk does not exist -- create_clock matched no port"
}
puts "\n=== top-level pins ==="
set v8_pins [list system_clk_p system_clk_n pcie_reset user_lnk_up \
                  c0_sys_clk_p c1_sys_clk_p c2_sys_clk_p c3_sys_clk_p]
foreach p $v8_pins {
    set o [get_ports -quiet $p]
    if {![llength $o]} { set o [get_ports -quiet "$p\[0\]"] }
    if {![llength $o]} { v8_bad "port $p matches nothing" ; continue }
    set loc [get_property -quiet PACKAGE_PIN $o]
    puts [format "  %-16s %-18s %s" $p [get_property NAME $o] \
          [expr {$loc eq "" ? "NO PACKAGE_PIN" : $loc}]]
    if {$loc eq ""} { v8_bad "port $p has no PACKAGE_PIN" }
}
# The clocks that load something, at the rate the config names.
foreach {pin mhz} [list clk_wiz_mesh$SYS_WIZ/clk_out4 $SYS_MHZ clk_wiz_ctrl/clk_out1 $CTRL_MHZ] {
    set c [get_clocks -quiet -of_objects [get_pins -quiet $top/$pin]]
    if {![llength $c]} { v8_bad "$pin has no clock" ; continue }
    set got [expr {1000.0 / [get_property PERIOD $c]}]
    if {abs($got - $mhz) > 1.0} { v8_bad [format "%s is %.1f MHz, want %.1f" $pin $got $mhz] }
}
foreach {mid mod} $MESHES {
    set c [get_clocks -quiet -of_objects [get_pins -quiet $top/clk_wiz_bus$mid/clk_out1]]
    if {![llength $c]} { v8_bad "clk_wiz_bus$mid/clk_out1 has no clock" ; continue }
    set got [expr {1000.0 / [get_property PERIOD $c]}]
    if {abs($got - $BUS_MHZ) > 1.0} { v8_bad [format "bus%s is %.1f MHz, want %.1f" $mid $got $BUS_MHZ] }
    if {$mid != $SYS_WIZ && [llength [get_pins -quiet $top/clk_wiz_mesh$mid/clk_out4]]} {
        v8_bad "clk_wiz_mesh$mid has a clk_out4 -- only SYS_WIZ carries the sysnode clock"
    }
}

# ---- 1b. DDR4 <-> SLR, from the part rather than from DDR_OF_SLR ----------
puts "\n=== DDR4 pins per die ==="
foreach {mid mod} $MESHES {
    set ddr [dict get $DDR_OF_SLR $mid]
    set txt [v8_slurp $BOARD_DIR/ddr4_c${ddr}.xdc]
    set slrs {}
    foreach {all pin} [regexp -all -inline {PACKAGE_PIN\s+(\w+)} $txt] {
        set st [get_sites -quiet -of_objects [get_package_pins -quiet $pin]]
        if {![llength $st]} { continue }
        lappend slrs [get_property NAME [get_slrs -of_objects $st]]
    }
    set slrs [lsort -unique $slrs]
    puts [format "  node_%s <- ddr4_%s  pins in %s" $mid $ddr $slrs]
    if {[llength $slrs] != 1 || [lindex $slrs 0] ne "SLR$mid"} {
        v8_bad "ddr4_$ddr pins are in $slrs, but it is home $mid pinned to SLR$mid"
    }
}

# ---- 2. pblock lines -----------------------------------------------------
puts "\n=== pblock lines ==="
foreach e $::V8_PB {
    lassign $e pb args
    set n [llength [v8_cells {*}$args]]
    puts [format "  %-8s %-96s %6d" $pb [string range $args 0 95] $n]
    if {$n == 0} { v8_bad "pblock $pb: $args resolves to NO cells" }
}
foreach {mid mod} $MESHES {
    set pat $top/station_bus/inst/u_line/g_stn\[$mid\].*/*g_link*
    if {[llength [get_cells -quiet -hier -filter "NAME =~ $pat"]] > 0} {
        v8_bad "g_link matched inside g_stn\[$mid\] -- it would be pinned"
    }
}
# Every leaf of the Xache is claimed by exactly one die: a slice left out
# floats, one claimed twice is 12-1433.
puts "\n=== Xache leaf coverage ==="
set kxleaf [get_cells -quiet -hier -filter "NAME =~ $top/xache/inst/u_kx/* && IS_PRIMITIVE && REF_NAME != GND && REF_NAME != VCC"]
array unset claim
foreach e $::V8_PB {
    lassign $e pb args
    if {[string first "xache" $args] < 0} { continue }
    foreach c [v8_cells {*}$args] {
        if {[get_property IS_PRIMITIVE $c]} { lappend claim($c) $pb }
        foreach l [get_cells -quiet -hier -filter "NAME =~ $c/* && IS_PRIMITIVE"] {
            lappend claim($l) $pb
        }
    }
}
set unclaimed 0 ; set twice 0
foreach c $kxleaf {
    if {![info exists claim($c)]} {
        incr unclaimed ; if {$unclaimed <= 8} { puts "  unclaimed $c" }
    } elseif {[llength [lsort -unique $claim($c)]] > 1} {
        incr twice ; if {$twice <= 8} { puts "  twice $c [lsort -unique $claim($c)]" }
    }
}
puts "  Xache leaves [llength $kxleaf], unclaimed $unclaimed, claimed twice $twice"
if {$twice} { v8_bad "$twice Xache leaf cells are pinned to two dies" }
# A leaf synthesis hoists out of its generate scope is pinned to the one die
# every neighbour it touches is in; neighbours on two dies would be a
# combinational die crossing, and that fails.
proc v8_pb_of {c} {
    global claim top
    if {[info exists claim($c)]} { return [lindex $claim($c) 0] }
    foreach e $::V8_PB {
        lassign $e pb args
        if {[string first "-filter" $args] >= 0 || [string first "*" $args] >= 0} { continue }
        set cell [string trim $args "{}"]
        regsub -all {\\\[} $cell {[} cell ; regsub -all {\\\]} $cell {]} cell
        if {[string match "$cell/*" $c] || $c eq $cell} { return $pb }
    }
    return ""
}
set leafxdc $out/${design_name}_pblocks_leaf.xdc
set lfh [open $leafxdc w]
puts $lfh "# GENERATED by scripts/tcl/v8t/70_analyze.tcl: Xache leaves hoisted out of their scope"
set unresolved 0 ; set pinned 0
foreach c $kxleaf {
    if {[info exists claim($c)]} { continue }
    set pbs {}
    foreach n [get_nets -quiet -of_objects [get_pins -quiet -of_objects $c]] {
        foreach lp [get_pins -quiet -leaf -of_objects $n] {
            set pb [v8_pb_of [get_cells -of_objects $lp]]
            if {$pb ne ""} { lappend pbs $pb }
        }
    }
    set pbs [lsort -unique $pbs]
    if {[llength $pbs] == 1} {
        puts $lfh "add_cells_to_pblock \[get_pblocks [lindex $pbs 0]\] \[get_cells -quiet \{$c\}\]"
        if {$pinned < 12} { puts "  pinned $c -> $pbs" }
        incr pinned
    } else {
        puts "  UNRESOLVED $c: neighbours in {$pbs}"
        incr unresolved
    }
}
close $lfh
if {![llength [get_files -quiet -of_objects [get_filesets constrs_1] $leafxdc]]} {
    add_files -fileset constrs_1 -norecurse $leafxdc
}
puts "  hoisted leaves pinned $pinned, unresolved $unresolved"
if {$unresolved} { v8_bad "$unresolved hoisted Xache leaf cell(s) touch two dies or none" }

puts "\n=== clock-group pins ==="
foreach p $::V8_CLKPINS {
    set c [get_clocks -quiet -of_objects [get_pins -quiet $p]]
    set nm [expr {[llength $c] ? [join [get_property NAME $c] " "] : "NONE"}]
    puts [format "  %-56s %s" $p $nm]
    if {![llength $c]} { v8_bad "clock pin $p resolves to no clock" }
}

# ---- 3. nodes and their clocks -------------------------------------------
puts "\n=== nodes ==="
set sysclk_name [get_property NAME [get_clocks -of_objects [get_pins $top/$SYS_CLK]]]
foreach {mid mod} $MESHES {
    set cell [get_cells -quiet $top/node_$mid]
    if {![llength $cell]} { v8_bad "node_$mid absent" ; continue }
    set ref [get_property REF_NAME $cell]
    set inner [get_cells -quiet $top/node_$mid/inst]
    set iref [expr {[llength $inner] ? [get_property REF_NAME $inner] : ""}]
    if {$ref ne $mod && $iref ne $mod && ![string match "*_$mod" $iref]} {
        v8_bad "node_$mid is $ref (inner: $iref), want $mod"
    }
    set bits {}
    foreach p {MESH_ID PORTS L2_MAG_BANKS L2_MAG_ENTRIES DRAM_CDC} {
        lappend bits "$p=[get_property -quiet $p $cell]"
    }
    puts "  node_$mid  $ref  [join $bits {  }]"
    if {[llength [get_cells -quiet -hier -filter "NAME =~ $top/node_$mid/* && REF_NAME =~ *NoCRouter*"]]} {
        v8_bad "node_$mid carries a router"
    }
    foreach pin {axi_aclk dram_aclk} {
        set p [get_pins -quiet $top/node_$mid/$pin]
        if {![llength $p]} { v8_bad "node_$mid/$pin missing" ; continue }
        set c [get_clocks -quiet -of_objects $p]
        set nm [expr {[llength $c] ? [get_property NAME $c] : "NONE"}]
        puts [format "  node_%s %-10s %s" $mid $pin $nm]
        if {$nm ne $sysclk_name} { v8_bad "node_$mid/$pin is $nm, not the sysnode clock $sysclk_name" }
    }
}
if {[llength [get_cells -quiet -hier -filter "REF_NAME =~ *NoCRouter* || REF_NAME =~ *noc_router*"]]} {
    v8_bad "a router exists somewhere: v8t has no mesh"
}
puts "\n=== the Xache's clocks ==="
foreach pin {aclk h_clk0 h_clk1 h_clk2 h_clk3} {
    set c [get_clocks -quiet -of_objects [get_pins -quiet $top/xache/$pin]]
    set nm [expr {[llength $c] ? [get_property NAME $c] : "NONE"}]
    puts [format "  xache/%-8s %s" $pin $nm]
    if {![llength $c]} { v8_bad "xache/$pin has no clock" }
    if {$pin eq "aclk" && $nm ne $sysclk_name} { v8_bad "xache/aclk is not the sysnode clock" }
}
foreach {mid mod} $MESHES {
    set ddr [dict get $DDR_OF_SLR $mid]
    set want [get_property NAME [get_clocks -of_objects [get_pins $top/ddr4_$ddr/c0_ddr4_ui_clk]]]
    set got [get_property NAME [get_clocks -of_objects [get_pins $top/xache/h_clk$mid]]]
    if {$got ne $want} { v8_bad "xache/h_clk$mid is $got, want ddr4_$ddr's $want" }
}
# The lanes cross the sysnode clock's dies, never a clock: every hop half is
# on the sysnode clock.
set hopcells [get_cells -quiet -hier -filter "NAME =~ $top/xache/inst/u_kx/*g_t?*?.u_h/u_?x"]
set hopbad 0
foreach h $hopcells {
    foreach cp [get_pins -quiet -of_objects $h -filter {IS_CLOCK}] {
        set k [get_clocks -quiet -of_objects $cp]
        if {[llength $k] && [get_property NAME [lindex $k 0]] ne $sysclk_name} { incr hopbad }
    }
}
puts "  hop halves [llength $hopcells], on another clock $hopbad"
if {$hopbad} { v8_bad "$hopbad hop half(s) clocked by something other than the sysnode clock" }

# ---- 4. ONE CLOCK = ONE RESET, audited on the netlist ---------------------
puts "\n=== reset audit ==="
proc v8_clk_of_cell {c} {
    set cp [get_pins -quiet -of_objects $c -filter {IS_CLOCK}]
    if {![llength $cp]} { return "" }
    set k [get_clocks -quiet -of_objects [lindex $cp 0]]
    return [expr {[llength $k] ? [get_property NAME [lindex $k 0]] : ""}]
}
proc v8_seq_loads {net depth} {
    set r {}
    foreach lp [get_pins -quiet -leaf -of_objects $net -filter {DIRECTION == IN}] {
        set lc [get_cells -of_objects $lp]
        if {[string match "LUT*" [get_property REF_NAME $lc]] && $depth > 0} {
            foreach on [get_nets -quiet -of_objects [get_pins -quiet -of_objects $lc -filter {DIRECTION == OUT}]] {
                lappend r {*}[v8_seq_loads $on [expr {$depth - 1}]]
            }
        } else {
            lappend r $lc
        }
    }
    return $r
}
set rst_bad 0
set psrs [get_cells -quiet $top/rst_* -filter {REF_NAME !~ *rst_tree*}]
if {[llength $psrs] != 10} { v8_bad "[llength $psrs] proc_sys_reset blocks under $top, want 10: ctrl, sys, 4 bus, 4 MIG" }
foreach psr $psrs {
    set sclk [get_pins -quiet $psr/slowest_sync_clk]
    if {![llength $sclk]} { continue }
    set k [get_clocks -quiet -of_objects $sclk]
    if {![llength $k]} { v8_bad "$psr: slowest_sync_clk carries no clock" ; continue }
    set kn [get_property NAME [lindex $k 0]]
    set loads 0 ; set foreign 0 ; set synced 0
    foreach op {peripheral_aresetn peripheral_reset interconnect_aresetn} {
        set n [get_nets -quiet -of_objects [get_pins -quiet $psr/$op]]
        if {![llength $n]} { continue }
        foreach lc [lsort -unique [v8_seq_loads $n 2]] {
            incr loads
            set lk [v8_clk_of_cell $lc]
            if {$lk eq "" || $lk eq $kn} { continue }
            if {[string match "*u_rs_*" $lc] || [string match "*kh_rst_sync*" [get_property REF_NAME $lc]]} { incr synced ; continue }
            incr foreign
            if {$foreign <= 6} { puts "  FOREIGN [file tail $psr] -> $lc on $lk" }
        }
    }
    puts [format "  %-14s %-40s loads %6d  synced %3d  foreign %d" [file tail $psr] $kn $loads $synced $foreign]
    if {$foreign} { incr rst_bad $foreign }
}
if {$rst_bad} { v8_bad "$rst_bad register(s) take a reset from another clock domain" }
# The sysnode reset's per-die copies: each sending register has ONE load, and
# every load of die i's copy is pinned to die i.
puts "\n=== sysnode reset tree ==="
foreach {mid mod} $MESHES {
    set qn [get_nets -quiet -of_objects [get_pins -quiet $top/rst_tree/inst/q_reg\[$mid\]/Q]]
    set ql [get_pins -quiet -leaf -of_objects $qn -filter {DIRECTION == IN}]
    set cn [get_nets -quiet -of_objects [get_pins -quiet $top/rst_tree/rstn_o$mid]]
    set home 0 ; set away 0 ; set free 0
    foreach lc [lsort -unique [v8_seq_loads $cn 2]] {
        set pb [v8_pb_of $lc]
        if {$pb eq "pb_slr$mid"} { incr home } elseif {$pb eq ""} { incr free } else {
            incr away ; if {$away <= 4} { puts "  AWAY copy $mid -> $lc in $pb" }
        }
    }
    puts [format "  copy %s: q_reg loads %d; copy loads on die %d, unpinned %d, other die %d" \
          $mid [llength $ql] $home $free $away]
    if {[llength $ql] != 1} { v8_bad "rst_tree q_reg\[$mid\] drives [llength $ql] loads, want 1" }
    if {$away} { v8_bad "$away load(s) of reset copy $mid are pinned to another die" }
}

# ---- 5. station bus and the address map, read back from the BD -----------
puts "\n=== station bus ==="
open_bd_design [get_files ${design_name}.bd]
set sbbd [get_bd_cells -quiet /station_bus]
foreach p {FW OST LINK_CDC LINK_FULL CRED PIPE SEG_OVERRIDE} {
    puts "  $p = [get_property -quiet CONFIG.$p $sbbd]"
}
if {[get_property -quiet CONFIG.SEG_OVERRIDE $sbbd] ne "1"} {
    v8_bad "station_bus SEG_OVERRIDE is not 1 -- the uniform 64 KB test map is live"
}
puts "\n=== address map (BD) ==="
source [file dirname [file normalize [info script]]]/50_addr_lit.tcl
set nseg [llength $seg_base]
foreach {nm want} [list SEG_BASE_P [v8_cat $seg_base $AW] SEG_DST_P [v8_cat $seg_dst 2] \
                        SEG_DPORT_P [v8_cat $seg_dprt 2]] {
    set got [get_property -quiet CONFIG.$nm $sbbd]
    if {$got ne $want} { v8_bad "station_bus $nm differs from the intended table" }
}
puts "  station segments: $nseg, literals match the intended table"
foreach {mid mod} $MESHES {
    set ddr [dict get $DDR_OF_SLR $mid]
    set segs [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet node_$mid/M_AXI_DRAM]]
    set hit "" ; foreach s $segs { if {[string match "*xache*" $s]} { set hit $s } }
    if {$hit eq ""} { v8_bad "node_$mid/M_AXI_DRAM is not mapped onto the Xache ($segs)" ; continue }
    if {[llength $segs] != 1} { v8_bad "node_$mid/M_AXI_DRAM has [llength $segs] segments, want the Xache alone" }
    set off [get_property OFFSET $hit] ; set rng [get_property RANGE $hit]
    puts [format "  node_%s/M_AXI_DRAM -> %s  offset %s range %s" $mid [file tail $hit] $off $rng]
    if {[expr {$off}] != 0} { v8_bad "node_$mid's Xache aperture is not at 0" }
    if {[expr {$rng}] < ($DRAM_GB << 30)} { v8_bad "node_$mid's Xache aperture is smaller than $DRAM_GB GB" }
    set dsegs [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet xache/M0${mid}_AXI]]
    set dhit "" ; foreach s $dsegs { if {[string match "*ddr4_$ddr*" $s]} { set dhit $s } }
    if {$dhit eq ""} { v8_bad "xache/M0${mid}_AXI is not mapped onto ddr4_$ddr" ; continue }
    set doff [get_property OFFSET $dhit] ; set drng [get_property RANGE $dhit]
    puts [format "  xache/M0%s_AXI -> ddr4_%s  offset %s range %s" $mid $ddr $doff $drng]
    if {[expr {$doff}] != 0 || [expr {$drng}] != (4 << 30)} { v8_bad "ddr4_$ddr is not the 4 GB at 0 behind home $mid" }
}
current_design v8t_synth

# ---- 6. timing and area, per die -----------------------------------------
puts "\n=== timing (synthesis) ==="
report_timing_summary -file $out/${design_name}_synth_timing.rpt
foreach c [lsort -dictionary [get_property NAME $clks]] {
    set p [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type max \
                            -sort_by slack -to [get_clocks $c]]
    if {![llength $p]} { continue }
    puts [format "  %-52s WNS %+8.3f  levels %s" $c \
          [get_property SLACK $p] [get_property LOGIC_LEVELS $p]]
}
set wp [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type max -sort_by slack]
if {[llength $wp]} {
    puts "@@@ v8t synth WNS [format %.3f [get_property SLACK $wp]] levels [get_property LOGIC_LEVELS $wp]"
    puts "@@@   from [get_property STARTPOINT_PIN $wp]"
    puts "@@@   to   [get_property ENDPOINT_PIN $wp]"
}
puts "\n=== ten worst setup paths ==="
foreach p [get_timing_paths -quiet -max_paths 10 -nworst 1 -delay_type max -sort_by slack] {
    puts [format "  %+8.3f lv %2s  %s -> %s" [get_property SLACK $p] [get_property LOGIC_LEVELS $p] \
          [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
}
report_utilization -file $out/${design_name}_synth_util.rpt
report_utilization -hierarchical -hierarchical_depth 3 \
    -file $out/${design_name}_synth_util_hier.rpt
if {$pinned} { read_xdc -quiet $leafxdc }
puts "\n=== per-SLR (pblock) usage ==="
foreach pb [lsort [get_pblocks]] {
    if {[catch {report_utilization -pblocks $pb -return_string} r]} {
        puts "  $pb: report_utilization failed: $r" ; continue
    }
    foreach row {"CLB LUTs" "CLB Registers" "Block RAM Tile" "URAM" "DSPs"} {
        if {[regexp "\\|\\s*$row\\*?\\s*\\|\\s*(\[0-9.\]+)" $r all n]} { puts [format "  %-8s %-16s %10s" $pb $row $n] }
    }
}
catch {report_utilization -pblocks [get_pblocks] -file $out/${design_name}_synth_util_pblocks.rpt}
report_control_sets -verbose -file $out/${design_name}_synth_ctrlsets.rpt
puts "\n@@@ reports in $out"
if {$v8_fail} { error "v8t analysis: $v8_fail check(s) FAILED" }
puts "@@@ v8t analysis: all checks passed"

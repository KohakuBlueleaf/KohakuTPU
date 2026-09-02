# Full observation of the SYNTHESIZED design against the plan: clocks, pins,
# every pblock line, the Xache's leaves claimed by exactly one die, the pipes,
# the reset audit on both trees, the station bus. V8_MODE ro (v8t2_analyze.tcl)
# runs on a READ-ONLY open of the project beside the implementation; the
# synth_1 checkpoint alone will not do -- the OOC IP and module references are
# black boxes in it, and open_run is what links them in.

if {![info exists V8_MODE]} { set V8_MODE project }
open_run synth_1 -name v8t2_synth
set out $root/build
file mkdir $out
set top ${design_name}_i
proc v8_slurp {path} {
    if {![file exists $path]} { error "v8t2 analyze: $path missing" }
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
# `[` opens a character class in a NAME =~ glob and in Tcl's string match; a
# path with brackets matches itself only with `?` in the filter (as
# 60_constraints writes its scope lines) and `\[` in string match.
proc v8_glob {s} { return [string map [list "\[" "?" "\]" "?"] $s] }
proc v8_smatch {s} { return [string map [list "\[" "\\\[" "\]" "\\\]"] $s] }
proc v8_mhz_of {pin} {
    global top
    set c [get_clocks -quiet -of_objects [get_pins -quiet $top/$pin]]
    if {![llength $c]} { return -1 }
    return [expr {1000.0 / [get_property PERIOD $c]}]
}
proc v8_want_mhz {pin mhz} {
    set got [v8_mhz_of $pin]
    if {$got < 0} { v8_bad "$pin has no clock" ; return }
    if {abs($got - $mhz) > 1.0} { v8_bad [format "%s is %.1f MHz, want %.1f" $pin $got $mhz] }
}
proc v8_t {} { return [clock format [clock seconds] -format %H:%M:%S] }

# ---- 1. clocks -----------------------------------------------------------
puts "\n=== clocks === [v8_t]"
set clks [get_clocks -quiet]
puts [format "%-52s %9s %9s" clock period MHz]
foreach c [lsort -dictionary [get_property NAME $clks]] {
    set p [get_property PERIOD [get_clocks $c]]
    puts [format "%-52s %9.3f %9.1f" $c $p [expr {$p > 0 ? 1000.0 / $p : 0}]]
}
puts "@@@ clocks [llength $clks]"
if {[llength $clks] < 20} { v8_bad "only [llength $clks] clocks" }
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
# Every generator output at the rate the config names.
v8_want_mhz $CTRL_CLK $CTRL_MHZ
v8_want_mhz $BUS_CLK  $BUS_MHZ
v8_want_mhz $SYS_CLK  $SYS_MHZ
foreach {mid mod} $MESHES {
    v8_want_mhz clk_wiz_mesh$mid/clk_out1 $MESH_MHZ
    v8_want_mhz clk_wiz_mesh$mid/clk_out2 $MAT2X_MHZ
    v8_want_mhz clk_wiz_mesh$mid/clk_out3 $VEC_MHZ
    if {$mid != $SYS_WIZ && [llength [get_pins -quiet $top/clk_wiz_mesh$mid/clk_out4]]} {
        v8_bad "clk_wiz_mesh$mid has a clk_out4 -- only SYS_WIZ carries the sysnode clock"
    }
}
if {[llength [get_cells -quiet $top/clk_wiz_bus*]]} { v8_bad "a per-die bus wizard exists: the stations share the system clock" }

# ---- 1b. DDR4 <-> SLR, from the part rather than from DDR_PORT_OF_SLR -----
puts "\n=== DDR4 pins per die === [v8_t]"
foreach {mid mod} $MESHES {
    set port [dict get $DDR_PORT_OF_SLR $mid]
    set txt [v8_slurp $BOARD_DIR/ddr4_c${port}.xdc]
    set slrs {}
    foreach {all pin} [regexp -all -inline {PACKAGE_PIN\s+(\w+)} $txt] {
        set st [get_sites -quiet -of_objects [get_package_pins -quiet $pin]]
        if {![llength $st]} { continue }
        lappend slrs [get_property NAME [get_slrs -of_objects $st]]
    }
    set slrs [lsort -unique $slrs]
    puts [format "  ddr4_%s <- board c%s  pins in %s" $mid $port $slrs]
    if {[llength $slrs] != 1 || [lindex $slrs 0] ne "SLR$mid"} {
        v8_bad "board controller c$port pins are in $slrs, but it is ddr4_$mid pinned to SLR$mid"
    }
    # the netlist agrees: ddr4_$mid's IOBs sit in SLR $mid
    set iob {}
    foreach c [get_cells -quiet -hier -filter "NAME =~ $top/ddr4_$mid/* && (REF_NAME =~ IOB* || REF_NAME =~ *IBUFDS* || REF_NAME =~ *OBUFDS*)"] {
        foreach s [get_slrs -quiet -of_objects [get_sites -quiet -of_objects $c]] { lappend iob [get_property NAME $s] }
    }
    set iob [lsort -unique $iob]
    if {[llength $iob] && $iob ne "SLR$mid"} { v8_bad "ddr4_$mid's placed IOBs are in $iob" }
}

# ---- 2. pblock lines -----------------------------------------------------
puts "\n=== pblock lines === [v8_t]"
foreach e $::V8_PB {
    lassign $e pb args
    set n [llength [v8_cells {*}$args]]
    puts [format "  %-8s %-96s %6d" $pb [string range $args 0 95] $n]
    if {$n == 0} { v8_bad "pblock $pb: $args resolves to NO cells" }
}
foreach {mid mod} $MESHES {
    set pat $top/station_bus/inst/u_line/g_stn?$mid?.*/*g_link*
    if {[llength [get_cells -quiet -hier -filter "NAME =~ $pat"]] > 0} {
        v8_bad "g_link matched inside g_stn\[$mid\] -- it would be pinned"
    }
}
# Every leaf of the Xache is claimed by exactly one die: a slice left out
# floats, one claimed twice is 12-1433. ONE netlist query per pblock line --
# the line's own filter with IS_PRIMITIVE appended -- then a Tcl array; a
# query per matched cell (the earlier form) was hours on this netlist.
puts "\n=== Xache leaf coverage === [v8_t]"
set kxleaf [get_cells -quiet -hier -filter "NAME =~ $top/xache/inst/u_kx/* && IS_PRIMITIVE && REF_NAME != GND && REF_NAME != VCC"]
array unset claim
foreach e $::V8_PB {
    lassign $e pb args
    if {[string first "xache" $args] < 0} { continue }
    if {[regexp {^-hier -filter \{(.*)\}$} $args all flt]} {
        set leaves [get_cells -quiet -hier -filter "($flt) && IS_PRIMITIVE"]
    } else {
        set cell [string trim $args "{}"]
        set leaves [get_cells -quiet -hier -filter "NAME =~ [v8_glob $cell]/* && IS_PRIMITIVE"]
        set self [get_cells -quiet $cell]
        if {[llength $self] && [get_property IS_PRIMITIVE $self]} { lappend leaves {*}$self }
    }
    foreach l $leaves { lappend claim([get_property NAME $l]) $pb }
}
set unclaimed 0 ; set twice 0
set unclaimed_cells {}
foreach c $kxleaf {
    set n [get_property NAME $c]
    if {![info exists claim($n)]} {
        incr unclaimed ; lappend unclaimed_cells $c
        if {$unclaimed <= 8} { puts "  unclaimed $n" }
    } elseif {[llength [lsort -unique $claim($n)]] > 1} {
        incr twice ; if {$twice <= 8} { puts "  twice $n [lsort -unique $claim($n)]" }
    }
}
puts "  Xache leaves [llength $kxleaf], unclaimed $unclaimed, claimed twice $twice  [v8_t]"
if {$twice} { v8_bad "$twice Xache leaf cells are pinned to two dies" }
# A leaf synthesis hoists out of its generate scope is pinned to the one die
# every neighbour it touches is in; neighbours on two dies would be a
# combinational die crossing, and that fails.
# The pblock a leaf name falls under: the Xache claims, then the plain and
# glob cell lines (`g_stn[i].*` is a glob); the Xache scope filters are
# already in `claim`.
proc v8_pb_of {c} {
    global claim top
    if {[info exists claim($c)]} { return [lindex $claim($c) 0] }
    foreach e $::V8_PB {
        lassign $e pb args
        if {[string first "-filter" $args] >= 0} { continue }
        set pat [v8_smatch [string trim $args "{}"]]
        if {[string match $pat $c] || [string match "$pat/*" $c]} { return $pb }
    }
    return ""
}
set leafxdc $out/${design_name}_pblocks_leaf.xdc
set lfh [open $leafxdc w]
puts $lfh "# GENERATED by scripts/tcl/v8t2/70_analyze.tcl: Xache leaves hoisted out of their scope"
set unresolved 0 ; set pinned 0
foreach c $unclaimed_cells {
    set pbs {}
    foreach n [get_nets -quiet -of_objects [get_pins -quiet -of_objects $c]] {
        foreach lp [get_pins -quiet -leaf -of_objects $n] {
            set pb [v8_pb_of [get_property NAME [get_cells -of_objects $lp]]]
            if {$pb ne ""} { lappend pbs $pb }
        }
    }
    set pbs [lsort -unique $pbs]
    if {[llength $pbs] == 1} {
        puts $lfh "add_cells_to_pblock \[get_pblocks [lindex $pbs 0]\] \[get_cells -quiet \{[get_property NAME $c]\}\]"
        if {$pinned < 12} { puts "  pinned [get_property NAME $c] -> $pbs" }
        incr pinned
    } else {
        puts "  UNRESOLVED [get_property NAME $c]: neighbours in {$pbs}"
        incr unresolved
    }
}
close $lfh
if {$V8_MODE eq "project" && $pinned} {
    if {![llength [get_files -quiet -of_objects [get_filesets constrs_1] $leafxdc]]} {
        add_files -fileset constrs_1 -norecurse $leafxdc
    }
}
puts "  hoisted leaves pinned $pinned (in $leafxdc), unresolved $unresolved"
if {$unresolved} { v8_bad "$unresolved hoisted Xache leaf cell(s) touch two dies or none" }

puts "\n=== clock-group pins ==="
foreach p $::V8_CLKPINS {
    set c [get_clocks -quiet -of_objects [get_pins -quiet $p]]
    set nm [expr {[llength $c] ? [join [get_property NAME $c] " "] : "NONE"}]
    puts [format "  %-56s %s" $p $nm]
    if {![llength $c]} { v8_bad "clock pin $p resolves to no clock" }
}

# ---- 3. meshes and their clocks ------------------------------------------
puts "\n=== meshes === [v8_t]"
set sysclk_name [get_property NAME [get_clocks -of_objects [get_pins $top/$SYS_CLK]]]
foreach {mid mod} $MESHES {
    set cell [get_cells -quiet $top/mesh_$mid]
    if {![llength $cell]} { v8_bad "mesh_$mid absent" ; continue }
    set ref [get_property REF_NAME $cell]
    set inner [get_cells -quiet $top/mesh_$mid/inst]
    set iref [expr {[llength $inner] ? [get_property REF_NAME $inner] : ""}]
    if {$ref ne $mod && $iref ne $mod && ![string match "*_$mod" $iref]} {
        v8_bad "mesh_$mid is $ref (inner: $iref), want $mod"
    }
    set bits {}
    foreach p {MESH_ID L2_MAG_BANKS L2_MAG_ENTRIES TILE_PRIM MAG_CDC UNIT_CDC DRAM_CDC} {
        lappend bits "$p=[get_property -quiet $p $cell]"
    }
    puts "  mesh_$mid  $ref  [join $bits {  }]"
    if {[llength [get_cells -quiet -hier -filter "NAME =~ $top/mesh_$mid/* && REF_NAME =~ *noc_l2_adapter*"]]} {
        v8_bad "mesh_$mid carries a noc_l2_adapter"
    }
    foreach pin {axi_aclk noc_clk vec_clk mat_clk mat_clk2x dram_aclk} {
        set p [get_pins -quiet $top/mesh_$mid/$pin]
        if {![llength $p]} { v8_bad "mesh_$mid/$pin missing" ; continue }
        set c [get_clocks -quiet -of_objects $p]
        set nm [expr {[llength $c] ? [get_property NAME $c] : "NONE"}]
        set mhz [expr {[llength $c] ? 1000.0 / [get_property PERIOD $c] : 0}]
        puts [format "  mesh_%s %-10s %-44s %7.1f MHz" $mid $pin $nm $mhz]
        if {![llength $c]} { v8_bad "mesh_$mid/$pin has no clock" }
        if {($pin eq "axi_aclk" || $pin eq "dram_aclk") && $nm ne $sysclk_name} {
            v8_bad "mesh_$mid/$pin is $nm, not the sysnode clock $sysclk_name"
        }
    }
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
    set want [get_property NAME [get_clocks -of_objects [get_pins $top/ddr4_$mid/c0_ddr4_ui_clk]]]
    set got [get_property NAME [get_clocks -of_objects [get_pins $top/xache/h_clk$mid]]]
    if {$got ne $want} { v8_bad "xache/h_clk$mid is $got, want ddr4_$mid's $want" }
}
# The lanes cross the sysnode clock's dies, never a clock: every hop half is
# on the sysnode clock. So is every interlink pipe half.
proc v8_count_offclock {cells want} {
    set bad 0
    foreach h $cells {
        foreach cp [get_pins -quiet -of_objects $h -filter {IS_CLOCK}] {
            set k [get_clocks -quiet -of_objects $cp]
            if {[llength $k] && [get_property NAME [lindex $k 0]] ne $want} { incr bad }
        }
    }
    return $bad
}
set hopcells [get_cells -quiet -hier -filter "NAME =~ $top/xache/inst/u_kx/*g_t?*?.u_h/u_?x"]
set hopbad [v8_count_offclock $hopcells $sysclk_name]
puts "  hop halves [llength $hopcells], on another clock $hopbad"
if {$hopbad} { v8_bad "$hopbad hop half(s) clocked by something other than the sysnode clock" }
puts "\n=== interlink pipes === [v8_t]"
set pipecells [get_cells -quiet -hier -filter "NAME =~ $top/pipe_*/inst/u_?x"]
set pipebad [v8_count_offclock $pipecells $sysclk_name]
puts "  pipe halves [llength $pipecells], on another clock $pipebad"
if {[llength $pipecells] != 12} { v8_bad "[llength $pipecells] interlink pipe halves, want 12 (3 hops x 2 directions x tx/rx)" }
if {$pipebad} { v8_bad "$pipebad pipe half(s) clocked by something other than the sysnode clock" }
# Every register whose load is in the OTHER half drives exactly that one
# load: a Laguna pair per crossing bit. A half's other registers face its own
# die's mesh (u_tx's credit return, u_rx's flit out) and fan out there.
foreach h $pipecells {
    set peer [expr {[string match "*/u_tx" $h] ? [string map {u_tx u_rx} $h] : [string map {u_rx u_tx} $h]}]
    set multi 0 ; set xbits 0
    foreach q [get_pins -quiet -of_objects [get_cells -quiet -hier -filter "NAME =~ [v8_glob $h]/* && IS_SEQUENTIAL"] -filter {DIRECTION == OUT}] {
        set loads [get_pins -quiet -leaf -of_objects [get_nets -quiet -of_objects $q] -filter {DIRECTION == IN}]
        set crossing 0
        foreach lp $loads { if {[string match "[v8_smatch $peer]/*" [get_property NAME $lp]]} { set crossing 1 } }
        if {!$crossing} { continue }
        incr xbits
        if {[llength $loads] != 1} { incr multi }
    }
    if {$multi} { v8_bad "$h: $multi crossing register(s) drive more than one load -- not a Laguna pair" }
    if {!$xbits} { v8_bad "$h: no register reaches its peer half" }
}

# ---- 4. ONE CLOCK = ONE RESET, audited on the netlist ---------------------
puts "\n=== reset audit === [v8_t]"
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
if {[llength $psrs] != 7} { v8_bad "[llength $psrs] proc_sys_reset blocks under $top, want 7: ctrl, bus, sys, 4 MIG" }
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
# Both reset trees: each sending register has ONE load, and every load of die
# i's copy is pinned to die i.
foreach t {rst_tree rst_tree_bus} {
    puts "\n=== $t === [v8_t]"
    foreach {mid mod} $MESHES {
        set qn [get_nets -quiet -of_objects [get_pins -quiet $top/$t/inst/q_reg\[$mid\]/Q]]
        set ql [get_pins -quiet -leaf -of_objects $qn -filter {DIRECTION == IN}]
        set cn [get_nets -quiet -of_objects [get_pins -quiet $top/$t/rstn_o$mid]]
        set home 0 ; set away 0 ; set free 0
        foreach lc [lsort -unique [v8_seq_loads $cn 2]] {
            set pb [v8_pb_of [get_property NAME $lc]]
            if {$pb eq "pb_slr$mid"} { incr home } elseif {$pb eq ""} { incr free } else {
                incr away ; if {$away <= 4} { puts "  AWAY copy $mid -> $lc in $pb" }
            }
        }
        puts [format "  copy %s: q_reg loads %d; copy loads on die %d, unpinned %d, other die %d" \
              $mid [llength $ql] $home $free $away]
        if {[llength $ql] != 1} { v8_bad "$t q_reg\[$mid\] drives [llength $ql] loads, want 1" }
        if {$away} { v8_bad "$away load(s) of $t copy $mid are pinned to another die" }
    }
}

# ---- 5. station bus, from the netlist -------------------------------------
puts "\n=== station bus === [v8_t]"
# `?` for the brackets: `\[` in a NAME =~ glob matches nothing, and a check
# on an empty match cannot fail -- so the cell count is asserted first.
set nmu0 $top/station_bus/inst/u_line/g_stn?1?.g_mgr.g_nmu?0?.*
set nmu0_all [get_cells -quiet -hier -filter "NAME =~ $nmu0"]
set nmu0_async [get_cells -quiet -hier -filter "NAME =~ $nmu0 && (REF_NAME =~ *async_fifo* || REF_NAME =~ xpm_fifo_async*)"]
puts "  JTAG NMU cells: [llength $nmu0_all], async FIFO cells: [llength $nmu0_async]"
if {![llength $nmu0_all]} { v8_bad "the JTAG NMU (g_stn\[1\].g_mgr.g_nmu\[0\]) matched no cell" }
if {[llength $nmu0_async]} { v8_bad "the JTAG NMU still carries [llength $nmu0_async] async FIFO cell(s): MGR0_DOM did not take" }
set jclk [get_clocks -quiet -of_objects [get_pins -quiet $top/jtag_ctrl/aclk]]
set bclk [get_clocks -quiet -of_objects [get_pins -quiet $top/$BUS_CLK]]
if {![llength $jclk] || ![llength $bclk] || [get_property NAME $jclk] ne [get_property NAME $bclk]} {
    v8_bad "jtag_ctrl/aclk is not the system clock"
}
# A module reference's inner ref names carry the reference's prefix
# (`multimesh_v8t2_station_bus_0_sb_link__xdcDup__1`), so match `*sb_link*`.
set glink [get_cells -quiet -hier -filter "NAME =~ $top/station_bus/inst/u_line/g_link*"]
set links [get_cells -quiet -hier -filter "NAME =~ $top/station_bus/inst/u_line/g_link* && REF_NAME =~ *sb_link*"]
set nlcdc [llength [get_cells -quiet -hier -filter "NAME =~ $top/station_bus/inst/u_line/g_link* && REF_NAME =~ *sb_link_cdc*"]]
set lrefs [expr {[llength $links] ? [lsort -unique [get_property REF_NAME $links]] : "none"}]
puts "  g_link cells [llength $glink]; link instances [llength $links] ($lrefs), of which clock-crossing $nlcdc"
foreach c [lrange $glink 0 5] { puts "    [get_property NAME $c]  [get_property REF_NAME $c]" }
if {![llength $glink]} { v8_bad "nothing under station_bus/inst/u_line/g_link*: the links are not where the pblock rules expect them" }
if {[llength $links] != 6} { v8_bad "[llength $links] station link instances, want 6 (3 boundaries x REQ + RSP)" }
if {$nlcdc} { v8_bad "$nlcdc sb_link_cdc instance(s): LINK_CDC did not take" }
if {[catch {open_bd_design [get_files ${design_name}.bd]} bdmsg]} {
    puts "  (block design not readable here: $bdmsg -- its parameters and address map are 75_verify_bd's)"
} else {
    set sbbd [get_bd_cells -quiet /station_bus]
    foreach p {FW OST LINK_CDC LINK_FULL LINK_KTS MGR0_DOM CRED PIPE SEG_OVERRIDE} {
        puts "  $p = [get_property -quiet CONFIG.$p $sbbd]"
    }
    if {[get_property -quiet CONFIG.SEG_OVERRIDE $sbbd] ne "1"} { v8_bad "station_bus SEG_OVERRIDE is not 1" }
    if {[get_property -quiet CONFIG.MGR0_DOM $sbbd] ne "1"} { v8_bad "station_bus MGR0_DOM is not 1" }
    if {[get_property -quiet CONFIG.LINK_CDC $sbbd] ne "0"} { v8_bad "station_bus LINK_CDC is not 0" }
    current_design v8t2_synth
}

# ---- 6. timing and area, per die -----------------------------------------
puts "\n=== timing (synthesis) === [v8_t]"
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
    puts "@@@ v8t2 synth WNS [format %.3f [get_property SLACK $wp]] levels [get_property LOGIC_LEVELS $wp]"
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
puts "\n=== per-SLR (pblock) usage === [v8_t]"
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
puts "\n@@@ reports in $out  [v8_t]"
if {$v8_fail} { error "v8t2 analysis: $v8_fail check(s) FAILED" }
puts "@@@ v8t2 analysis: all checks passed"

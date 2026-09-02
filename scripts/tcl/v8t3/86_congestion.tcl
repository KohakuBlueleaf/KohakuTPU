# Congestion attribution on a routed run: where every module's cells actually
# sit, which columns the SLR crossings use, and per congestion window who sits
# inside, whose nets end inside, and whose nets only pass through.
#
# A window is a pair of tile names -- (CLEM_X64Y570,CLEL_R_X78Y601) -- so the
# grid is resolved through get_tiles rather than assumed. Nets are attributed by
# NET NAME, which is the scope that declares them: one bulk get_property instead
# of two get_pins per net, which on the 113k-net L6 window did not finish in an
# hour. The driver -> load pass survives for the pass-through set alone, capped.

set here [file dirname [file normalize [info script]]]
if {![info exists design_name]} { source $here/00_config.tcl }

if {![llength [current_project -quiet]]} {
    open_project -read_only $proj_dir/${design_name}.xpr
}
if {![llength [current_design -quiet]]} { open_run impl_1 -name ${design_name}_cong }

set out $root/build
file mkdir $out
set top ${design_name}_i
set PAIR_CAP 2000

# The owner of a leaf or a net: the top's child, then the next two levels that
# are not the BD's "inst" wrapper -- deep enough to name a partition or a stage.
proc v8_owner {name} {
    global top
    set p [split $name /]
    if {[lindex $p 0] eq $top} { set p [lrange $p 1 end] }
    set keep {}
    foreach c $p {
        if {$c eq "inst"} { continue }
        lappend keep $c
        if {[llength $keep] >= 3} { break }
    }
    if {![llength $keep]} { return "-" }
    return [join $keep /]
}
proc v8_top1 {name} { return [lindex [split [v8_owner $name] /] 0] }
# A PIN name ends in the pin, which is not part of the owner.
proc v8_pin_owner {name} {
    set p [split $name /]
    return [v8_owner [join [lrange $p 0 end-1] /]]
}

# ------------------------------------------------- footprint per module ------
# Cells per owner per clock region, one bulk pass over every placed primitive.
if {[catch {
    set pl [get_cells -quiet -hierarchical -filter {IS_PRIMITIVE && LOC != ""}]
    puts "@@@ placed primitives: [llength $pl]"
    set names [get_property NAME $pl]
    set locs  [get_property LOC  $pl]
    set refs  [get_property REF_NAME $pl]
    array unset CR
    foreach loc [lsort -unique $locs] {
        set CR($loc) [get_property CLOCK_REGION [get_sites -quiet $loc]]
    }
    puts "@@@ distinct sites: [array size CR]"
    array unset F
    array unset T
    array unset R
    foreach nm $names loc $locs rf $refs {
        set cr $CR($loc)
        if {$cr eq ""} { set cr "-" }
        incr F([v8_owner $nm]\t$cr)
        incr T([v8_top1 $nm]\t$cr)
        # A RAMB18 is half a block-RAM tile, which is how report_utilization
        # counts it; everything is scaled by 2 and halved on the way out.
        set kind "" ; set n 2
        if {[string match "RAMB36*" $rf]} { set kind BRAM } \
        elseif {[string match "RAMB18*" $rf]} { set kind BRAM ; set n 1 } \
        elseif {[string match "URAM288*" $rf]} { set kind URAM } \
        elseif {[string match "DSP_*" $rf] || $rf eq "DSP48E2"} { set kind DSPSUB } \
        elseif {[string match "LUT*" $rf] || [string match "RAM?32*" $rf] \
                || [string match "RAM?64*" $rf] || [string match "SRL*" $rf]} { set kind LUT } \
        elseif {[string match "FD*" $rf]} { set kind FF }
        if {$kind ne ""} {
            set slr [expr {[regexp {Y(\d+)$} $cr -> y] ? $y / 4 : "-"}]
            incr R($kind\t[v8_top1 $nm]\t$slr) $n
            incr D($kind\t[v8_owner $nm]\t$slr) $n
        }
    }
    set rf2 [open $out/${design_name}_census_slr.tsv w]
    puts $rf2 [join {KIND MODULE SLR COUNT} \t]
    foreach k [lsort [array names R]] {
        lassign [split $k \t] kind mod slr
        puts $rf2 "$kind\t$mod\t$slr\t[expr {$R($k) / 2.0}]"
    }
    close $rf2
    set rf3 [open $out/${design_name}_census_slr_deep.tsv w]
    puts $rf3 [join {KIND OWNER SLR COUNT} \t]
    foreach k [lsort [array names D]] {
        lassign [split $k \t] kind own slr
        puts $rf3 "$kind\t$own\t$slr\t[expr {$D($k) / 2.0}]"
    }
    close $rf3
    puts "@@@ per-SLR resource census written ([array size R] rows, [array size D] deep)"
    set ff [open $out/${design_name}_cell_regions.tsv w]
    puts $ff [join {OWNER CLOCK_REGION CELLS} \t]
    foreach k [lsort [array names F]] { puts $ff "$k\t$F($k)" }
    close $ff
    set tf [open $out/${design_name}_cell_regions_top.tsv w]
    puts $tf [join {MODULE CLOCK_REGION CELLS} \t]
    foreach k [lsort [array names T]] { puts $tf "$k\t$T($k)" }
    close $tf
    puts "@@@ footprint written"
} emsg]} { puts "@@@ footprint failed: $emsg" }

# SLL usage per boundary is report_utilization -slr's "SLR Connectivity" table
# and per owner is report_design_analysis's "SLR Net Crossing Reporting"; both
# are in 80_report's output. A Laguna-tile census is NOT the way to get it --
# get_nodes on a LAG tile returns the clock routing and none of the SLL data.

# ------------------------------------------------------------- clocks --------
report_clock_utilization -file $out/${design_name}_clock_util.rpt
# One row per BUFG output: a global clock appears once per hierarchical alias
# of its net, and the aliases are the same physical clock.
if {[catch {
    set cf2 [open $out/${design_name}_clock_roots.tsv w]
    puts $cf2 [join {DRIVER CLOCK_ROOT LOADS NET} \t]
    array unset K
    foreach n [get_nets -quiet -hierarchical -filter {TYPE == GLOBAL_CLOCK}] {
        set d [get_pins -quiet -leaf -of_objects $n -filter {DIRECTION == OUT}]
        if {![llength $d]} { continue }
        set dn [get_property NAME [lindex $d 0]]
        if {[info exists K($dn)]} { continue }
        set K($dn) 1
        puts $cf2 "$dn\t[get_property -quiet CLOCK_ROOT $n]\
\t[llength [get_pins -quiet -leaf -of_objects $n]]\t[get_property NAME $n]"
    }
    close $cf2
    puts "@@@ clock roots written: [array size K] global clocks"
} emsg]} { puts "@@@ clock roots failed: $emsg" }

# ---------------------------------------------------------------- windows ----
set txt [report_design_analysis -congestion -return_string]
set wins {}
set sec "?"
foreach line [split $txt "\n"] {
    if {[regexp {^\d+\.\s+(.*?)\s*$} $line -> h]} { set sec $h }
    if {[regexp {^\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\d+)\s*\|\s*\((\S+),(\S+)\)\s*\|} \
             $line -> dir type lvl t0 t1]} {
        lappend wins [list $sec $dir $type $lvl $t0 $t1]
    }
}
puts "@@@ congestion windows parsed: [llength $wins]"

set wf [open $out/${design_name}_cong_windows.tsv w]
puts $wf [join {SECTION DIRECTION TYPE LEVEL TILE0 TILE1 COL0 COL1 ROW0 ROW1 \
                SITES CELLS NETS_ENDPOINT NETS_ROUTED NETS_THROUGH} \t]
set cf [open $out/${design_name}_cong_cells.tsv w]
puts $cf [join {WINDOW LEVEL OWNER CELLS} \t]
set nf [open $out/${design_name}_cong_nets.tsv w]
puts $nf [join {WINDOW LEVEL KIND OWNER NETS} \t]
set pf [open $out/${design_name}_cong_pairs.tsv w]
puts $pf [join {WINDOW LEVEL SRC_OWNER DST_OWNER NETS} \t]

foreach w $wins {
    lassign $w sec dir type lvl t0 t1
    set win "$t0..$t1"
    set a [get_tiles -quiet $t0]
    set b [get_tiles -quiet $t1]
    if {![llength $a] || ![llength $b]} { puts "@@@ window $win: tiles not found" ; continue }
    set c0 [get_property COLUMN $a] ; set c1 [get_property COLUMN $b]
    set r0 [get_property ROW    $a] ; set r1 [get_property ROW    $b]
    if {$c0 > $c1} { set t $c0 ; set c0 $c1 ; set c1 $t }
    if {$r0 > $r1} { set t $r0 ; set r0 $r1 ; set r1 $t }
    set tiles [get_tiles -quiet -filter \
        "COLUMN >= $c0 && COLUMN <= $c1 && ROW >= $r0 && ROW <= $r1"]
    set sites [get_sites -quiet -of_objects $tiles]
    set cells [get_cells -quiet -of_objects $sites]
    puts "@@@ window $win level $lvl: [llength $tiles] tiles, [llength $sites] sites,\
 [llength $cells] cells"

    array unset C
    if {[llength $cells]} {
        foreach nm [get_property NAME $cells] { incr C([v8_owner $nm]) }
    }
    foreach k [lsort [array names C]] { puts $cf [join [list $win $lvl $k $C($k)] \t] }
    flush $cf

    # every net with a pin on a cell inside, and every net routed through the
    # window's tiles: the difference is what the window pays for and gets nothing from
    set en {}
    if {[llength $cells]} {
        set en [get_property NAME [get_nets -quiet -of_objects \
                    [get_pins -quiet -of_objects $cells]]]
    }
    set inside [dict create]
    array unset N
    foreach nm $en { dict set inside $nm 1 ; incr N(ENDPOINT|[v8_owner $nm]) }
    set rn {}
    if {[catch {
        set rn [get_property NAME [get_nets -quiet -of_objects \
                    [get_nodes -quiet -of_objects $tiles]]]
    } emsg]} { puts "@@@ window $win: routed-net pass failed: $emsg" }
    set thru {}
    foreach nm $rn {
        if {[dict exists $inside $nm]} { continue }
        lappend thru $nm
        incr N(PASS_THROUGH|[v8_owner $nm])
    }
    puts "@@@ window $win: [llength $en] endpoint nets, [llength $rn] routed,\
 [llength $thru] pass-through"
    foreach k [lsort [array names N]] {
        lassign [split $k |] kind owner
        puts $nf "$win\t$lvl\t$kind\t$owner\t$N($k)"
    }
    flush $nf
    puts $wf [join [list $sec $dir $type $lvl $t0 $t1 $c0 $c1 $r0 $r1 \
                         [llength $sites] [llength $cells] \
                         [llength $en] [llength $rn] [llength $thru]] \t]
    flush $wf

    # from who to who, for the pass-through set alone: two pin queries a net, so
    # capped -- the owner histogram above already covers every one of them. ONE
    # row a net (driver, first load), or a clock counts once per load scope and
    # a single net reads as thousands.
    array unset P
    set n 0
    set paired 0
    foreach nm $thru {
        if {[incr n] > $PAIR_CAP} { break }
        set net [get_nets -quiet $nm]
        if {[llength $net] != 1} { continue }
        set dp [get_pins -quiet -leaf -of_objects $net -filter {DIRECTION == OUT}]
        if {![llength $dp]} { continue }
        set so [v8_pin_owner [get_property NAME [lindex $dp 0]]]
        set lp [get_pins -quiet -leaf -of_objects $net -filter {DIRECTION == IN}]
        set do_ "-"
        if {[llength $lp]} { set do_ [v8_pin_owner [get_property NAME [lindex $lp 0]]] }
        incr P($so|$do_)
        incr paired
    }
    foreach k [lsort [array names P]] {
        lassign [split $k |] src dst
        puts $pf "$win\t$lvl\t$src\t$dst\t$P($k)"
    }
    flush $pf
    puts "@@@ window $win: $paired of [expr {$n > $PAIR_CAP ? $PAIR_CAP : $n}] pass-through nets paired"
}
close $wf ; close $cf ; close $nf ; close $pf
puts "@@@ congestion attribution complete"

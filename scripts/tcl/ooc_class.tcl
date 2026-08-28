# Fmax for EVERY clock, queried per clock. A single sampled sweep only reports
# the clocks that appear in the worst N, so the other domain silently vanished.

# Count by REF_NAME. `PRIMITIVE_GROUP == CLB_LUT` matches nothing here and
# returns zero SILENTLY, which reads as "that instance is empty".

# `prefix` scopes by NAME. `get_cells -hier -of_objects <cell>` also returns
# nothing silently -- -of_objects wants nets or pins, not a parent.
proc ooc_count {label {prefix {}}} {
    set out [format "%-18s" $label]
    # `[` opens a CHARACTER CLASS in a Vivado glob, so a g_stn[1] prefix matched
    # the single char `1` and every bracketed instance counted ZERO in silence.
    set pfx [string map [list "\[" "\\\[" "\]" "\\\]"] $prefix]
    foreach {nm pats} {LUT {LUT?} FF {FD*} LUTRAM {RAMD* RAMS*} SRL {SRL*} \
                       BRAM {RAMB*} MUXF {MUXF*} CARRY {CARRY*}} {
        set n 0
        foreach p $pats {
            set f "REF_NAME =~ $p"
            if {$prefix ne ""} { append f " && NAME =~ $pfx/*" }
            incr n [llength [get_cells -quiet -hier -filter $f]]
        }
        append out [format "  %s %6d" $nm $n]
    }
    puts "@@@ $out"
}

# A flat module's LUT total is a LUMP -- `report_utilization -hierarchical`
# cannot look inside one, so a 3,672-LUT control block names no signal to aim
# at. Synthesis derives a LUT's name from the signal it drives, so bucketing by
# that name turns the lump back into a list. Strips `_i_N`, `[k]`, `_reg` and
# `__N` so every LUT of one signal lands in one bucket.
proc ooc_lut_census {label prefix {top 24}} {
    set pfx [string map [list "\[" "\\\[" "\]" "\\\]"] $prefix]
    # Empty prefix = whole design; a NAME filter of "/*" matches nothing.
    if {$prefix eq ""} {
        set cells [get_cells -quiet -hier -filter "REF_NAME =~ LUT?"]
    } else {
        set cells [get_cells -quiet -hier -filter "REF_NAME =~ LUT? && NAME =~ $pfx/*"]
    }
    array set bucket {}
    foreach c $cells {
        set n [get_property NAME $c]
        # Drop the scoping prefix, then everything is one flat name.
        set drop [expr {$prefix eq "" ? 0 : [string length $prefix] + 1}]
        set n [string range $n $drop end]
        regsub {_i_[0-9]+$} $n "" n
        regsub {__[0-9]+$} $n "" n
        regsub -all {\[[0-9]+\]} $n "" n
        regsub {_reg$} $n "" n
        if {$n eq ""} { set n "(unnamed)" }
        incr bucket($n)
    }
    set rows {}
    foreach {k v} [array get bucket] { lappend rows [list $v $k] }
    set rows [lsort -integer -decreasing -index 0 $rows]
    puts "@@@CENSUS $label total [llength $cells] LUT in [llength $rows] signal(s)"
    set i 0
    foreach r $rows {
        if {[incr i] > $top} break
        puts [format "@@@CENSUS %s %6d  %s" $label [lindex $r 0] [lindex $r 1]]
    }
}

# Which FFs actually carry a reset, grouped by parent. Reading the RTL does not
# settle it: a reset-free source still routes as one if synthesis folds one in.
proc ooc_resets {} {
    array set hit {}
    array set tot {}
    foreach c [get_cells -quiet -hier -filter {REF_NAME =~ FD*}] {
        set m [get_property REF_NAME [get_cells -quiet $c]]
        # FDRE/FDSE use R/S, FDCE/FDPE use CLR/PRE.
        set pin [expr {[string match FDC* $m] ? "CLR" :
                      ([string match FDP* $m] ? "PRE" :
                      ([string match FDS* $m] ? "S" : "R"))}]
        set n [get_nets -quiet -of_objects [get_pins -quiet $c/$pin]]
        set k [file dirname $c]
        if {![info exists tot($k)]} { set tot($k) 0; set hit($k) 0 }
        incr tot($k)
        if {[llength $n] && [get_property TYPE $n] ne "GROUND"} { incr hit($k) }
    }
    set rows {}
    set th 0; set tt 0
    foreach k [array names tot] {
        incr th $hit($k); incr tt $tot($k)
        if {$hit($k)} { lappend rows [list $hit($k) $tot($k) $k] }
    }
    puts [format "@@@ reset FF %d of %d" $th $tt]
    foreach r [lsort -integer -decreasing -index 0 $rows] {
        lassign $r h t k
        puts [format "@@@   %6d / %6d  %s" $h $t $k]
    }
}

# Control sets, not reset count, are what a datapath reset actually costs: eight
# FFs share one CLK/CE/SR triple per slice, so a stray SR splits the packing.
proc ooc_ctrlsets {} {
    foreach l [split [report_control_sets -verbose -return_string] "\n"] {
        if {[string match -nocase "*control set*" $l] ||
            [string match -nocase "*Number of unique*" $l]} { puts "@@@ $l" }
    }
}

# Vivado's own SITE accounting, not ooc_count's primitive counts: a LUT6 holds
# two RAMD32, so a distributed-RAM primitive count can exceed the sites charged.
proc ooc_util {} {
    # Sub-rows are INDENTED and the total is "CLB LUTs*", so an exact
    # "| <key> " match silently drops every row that matters.
    foreach l [split [report_utilization -return_string] "\n"] {
        if {![string match "|*" [string trim $l]]} { continue }
        foreach k {"CLB LUTs" "LUT as Logic" "LUT as Memory" \
                   "LUT as Distributed RAM" "LUT as Shift Register" \
                   "CLB Registers" "Block RAM Tile"} {
            if {[string first $k $l] >= 0} { puts "@@@ util [string trim $l]" }
        }
    }
}

# Per-hierarchy LUT SITES, which is the only unit comparable to a vendor
# utilization report. ooc_count's per-instance numbers are primitives.
proc ooc_hier {{depth 3}} {
    foreach l [split [report_utilization -hierarchical \
                          -hierarchical_depth $depth -return_string] "\n"] {
        if {[string match "|*" [string trim $l]]} { puts "@@@ hier [string trim $l]" }
    }
}

# One synth, every number: `@@@REC` key=value totals, `@@@FMAX` per clock,
# `@@@HIER` per instance. `cfg` is the configuration as key=value pairs.
proc ooc_record {tag cfg {npaths 2000} {depth 2}} {
    array set u {}
    foreach l [split [report_utilization -return_string] "\n"] {
        if {![string match "|*" [string trim $l]]} { continue }
        set f [split $l "|"]
        if {[llength $f] < 4} { continue }
        set k [string trim [lindex $f 1]]
        set v [string trim [lindex $f 2]]
        # double: a lone RAMB18 makes "Block RAM Tile" 26.5, and an integer
        # test drops the row -- reporting 0 BRAM for a design that uses it.
        if {![string is double -strict $v]} { continue }
        switch -glob -- $k {
            "CLB LUTs*"                 { set u(lut)      $v }
            "LUT as Logic"              { set u(lut_log)  $v }
            "LUT as Memory"             { set u(lut_mem)  $v }
            "LUT as Distributed RAM"    { set u(lut_dram) $v }
            "LUT as Shift Register"     { set u(lut_srl)  $v }
            "CLB Registers"             { set u(ff)       $v }
            "Block RAM Tile"            { set u(bram)     $v }
            "URAM"                      { set u(uram)     $v }
            "DSPs"                      { set u(dsp)      $v }
        }
    }
    foreach k {lut lut_log lut_mem lut_dram lut_srl ff bram uram dsp} {
        if {![info exists u($k)]} { set u($k) 0 }
    }

    set cs 0
    foreach l [split [report_control_sets -verbose -return_string] "\n"] {
        if {[regexp {Total control sets\s*\|\s*(\d+)} $l -> n]} { set cs $n }
    }

    set out "@@@REC tag=$tag $cfg"
    foreach k {lut lut_log lut_mem lut_dram lut_srl ff bram uram dsp} {
        append out " $k=$u($k)"
    }
    append out " ctrlsets=$cs"
    puts $out

    foreach c [get_clocks -quiet] {
        set nm [get_property NAME $c]
        set ps [get_timing_paths -to $c -max_paths $npaths -nworst $npaths -setup]
        if {[llength $ps] == 0} { puts "@@@FMAX $tag $nm none"; continue }
        set wsl 1e9; set req 0
        foreach p $ps {
            set sl [get_property SLACK $p]
            if {$sl < $wsl} { set wsl $sl; set req [get_property REQUIREMENT $p] }
        }
        set d [expr {$req - $wsl}]
        set f [expr {$d > 0 ? 1000.0 / $d : 0.0}]
        puts [format "@@@FMAX %s %s %.1f slack %+.3f req %.3f" $tag $nm $f $wsl $req]
    }

    foreach l [split [report_utilization -hierarchical \
                          -hierarchical_depth $depth -return_string] "\n"] {
        if {[string match "|*" [string trim $l]]} {
            puts "@@@HIER $tag [string trim $l]"
        }
    }
}

# EVERY failing endpoint, grouped into cones, then one representative path per
# cone cell by cell. Listing the worst N is useless on a real design: they come
# back as N bits of the same register, one problem wearing many names. Strip the
# bit indices and the tool's _iNN suffixes and count -- THAT is the number of
# distinct things wrong. The SIMT PE read 1,633 failing endpoints and five
# problems; fixing one of the five took it to 70.
proc ooc_cones {{ndetail 6} {maxp 200000}} {
    puts "@@@ ============================ ALL failing endpoints, by cone"
    set fp [get_timing_paths -max_paths $maxp -nworst 1 -setup -slack_lesser_than 0]
    puts "@@@G total failing endpoints: [llength $fp]"
    array unset CONE
    foreach p $fp {
        set ep [get_property ENDPOINT_PIN $p]
        set sp [get_property STARTPOINT_PIN $p]
        foreach v {ep sp} {
            upvar 0 $v s
            regsub -all {\[[0-9]+\]} $s {[]} s
            regsub -all {_[0-9]+(_[0-9]+)+} $s {_N} s
            regsub -all {_i_[0-9]+} $s {_iN} s
        }
        set k "$sp -> $ep"
        if {![info exists CONE($k)]} { set CONE($k) [list 0 99.0 0 {}] }
        lassign $CONE($k) n w l b
        incr n
        set s [get_property SLACK $p]
        set v [get_property LOGIC_LEVELS $p]
        if {$s < $w} { set w $s ; set b $p }
        if {$v > $l} { set l $v }
        set CONE($k) [list $n $w $l $b]
    }
    set rows {}
    foreach k [array names CONE] { lappend rows [concat $CONE($k) [list $k]] }
    set rows [lsort -real -index 1 $rows]
    foreach r $rows {
        puts [format "@@@G n=%-5d worst=%7.3f lvl=%-3d %s" \
                  [lindex $r 0] [lindex $r 1] [lindex $r 2] [lindex $r 4]]
    }

    puts "@@@ ============================ top cones, cell by cell"
    foreach r [lrange $rows 0 [expr {$ndetail - 1}]] {
        puts "@@@D === [lindex $r 4]"
        set on 0
        # `-of_objects` REFUSES -input_pins and silently drops it, and Vivado
        # prints "Delay type" in lower case -- matching "Delay Type" emitted six
        # empty dumps that read exactly like six clean cones.
        foreach l [split [report_timing -of_objects [lindex $r 3] \
                              -return_string] "\n"] {
            if {[string match "*Delay type*" $l]} { set on 1 }
            if {$on && [string trim $l] ne ""} { puts "@@@D $l" }
            if {$on && [string match "*required time*" $l]} { break }
        }
    }

    # Route is the majority of these paths and fanout is why: a driver feeding
    # hundreds of loads cannot be placed near all of them.
    puts "@@@ ============================ high-fanout nets"
    # NOT lmap with a bracketed body: `[list $x ...]` is substituted BEFORE lmap
    # runs, so the loop variable is unset and the block errors out.
    set fan {}
    foreach x [get_nets -hierarchical -filter \
                   {FLAT_PIN_COUNT > 60 && TYPE == SIGNAL}] {
        lappend fan [list $x [get_property FLAT_PIN_COUNT $x]]
    }
    foreach n [lrange [lsort -decreasing -integer -index 1 $fan] 0 24] {
        puts [format "@@@F %6d  %s" [lindex $n 1] [lindex $n 0]]
    }
}

proc ooc_classify {{npaths 4000}} {
    foreach c [get_clocks] {
        set nm [get_property NAME $c]
        set ps [get_timing_paths -to $c -max_paths $npaths -nworst $npaths -setup]
        if {[llength $ps] == 0} { puts [format "@@@ %-8s no paths" $nm]; continue }

        set wsl 1e9
        set rep {}
        foreach p $ps {
            set sl [get_property SLACK $p]
            if {$sl < $wsl} {
                set wsl $sl
                set rep [list [get_property REQUIREMENT $p] \
                              [get_property LOGIC_LEVELS $p] \
                              [get_property STARTPOINT_PIN $p] \
                              [get_property ENDPOINT_PIN $p]]
            }
        }
        lassign $rep req lvl sp ep
        set d [expr {$req - $wsl}]
        set f [expr {$d > 0 ? 1000.0 / $d : 0.0}]
        puts [format "@@@ %-8s %6.1f MHz  slack %+.3f  req %.3f  lvl %-3s %s -> %s" \
                  $nm $f $wsl $req $lvl $sp $ep]
    }
}

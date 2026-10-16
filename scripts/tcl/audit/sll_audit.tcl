# sll_audit.tcl -- on an open routed design: every die (SLR) boundary crossing,
# and every clock that reaches a die other than its source's.
#
#   source scripts/tcl/audit/sll_audit.tcl; sll_audit <out.txt>
#
# Part 1, foreign clocks: for each global clock net, the SLR of its buffer and
# the clocked pins it reaches per SLR; every leaf in another SLR is listed by
# owner (three levels below the block design) and cell type, LAGUNA sites
# counted apart. A die-local clock with foreign leaves outside Laguna is a
# rule break: only a Laguna RX flop may take another die's clock.
# Part 2, crossings: every net whose driver and a load sit in different SLRs,
# grouped by owner and kind -- a register hop on one clock (Laguna-registered
# or fabric), a clock-domain crossing, or a combinational net -- with the
# group's worst setup and hold slack through it.

proc _owner {name} {
    set p [split $name /]
    return [join [lrange $p 1 3] /]
}

proc _slr {c} {
    set s [get_slrs -quiet -of $c]
    return [expr {$s eq "" ? "-" : [get_property NAME $s]}]
}

proc _clock_of {cell} {
    set cp [get_pins -quiet -of $cell -filter {IS_CLOCK}]
    if {$cp eq ""} { return "" }
    return [get_clocks -quiet -of [lindex $cp 0]]
}

proc sll_audit {out} {
    set fh [open $out w]
    puts $fh "== Part 1: global clocks reaching another die"
    foreach buf [get_cells -hier -filter {PRIMITIVE_SUBGROUP == BUFG || REF_NAME =~ BUFG*}] {
        set n [get_nets -quiet -of [get_pins -quiet -of $buf -filter {DIRECTION == OUT}]]
        if {$n eq ""} { continue }
        set home [_slr $buf]
        set leaves [get_pins -quiet -leaf -of [get_nets -segments $n] -filter {DIRECTION == IN}]
        set per [dict create]
        set foreign [dict create]
        foreach p $leaves {
            set c [get_cells -of $p]
            set s [_slr $c]
            dict incr per $s
            if {$s ne $home} {
                set lag [string match LAGUNA* [get_property -quiet SITE_TYPE [get_sites -quiet -of $c]]]
                dict incr foreign "[expr {$lag ? {LAGUNA} : {fabric}}] [get_property REF_NAME $c] [_owner [get_property NAME $c]]"
            }
        }
        if {[dict size $foreign] == 0} { continue }
        puts $fh "\n-- [get_property NAME $n]  (buffer in $home)  leaves per SLR: $per"
        foreach k [lsort [dict keys $foreign]] { puts $fh [format "   %6d  %s" [dict get $foreign $k] $k] }
    }

    puts $fh "\n== Part 2: nets crossing a die boundary"
    set groups [dict create]
    # CROSSING_SLRS narrows ~1.9M nets to the few thousand that leave a die
    foreach n [get_nets -hier -top_net_of_hierarchical_group -filter {TYPE == SIGNAL && CROSSING_SLRS != ""}] {
        set d [get_pins -quiet -leaf -of [get_nets -segments $n] -filter {DIRECTION == OUT}]
        if {[llength $d] != 1} { continue }
        set dc [get_cells -of $d]
        set ds [_slr $dc]
        set far {}
        foreach p [get_pins -quiet -leaf -of [get_nets -segments $n] -filter {DIRECTION == IN}] {
            set lc [get_cells -of $p]
            if {[_slr $lc] ne $ds} { lappend far $lc }
        }
        if {$far eq ""} { continue }
        set dclk [_clock_of $dc]
        set seq [get_property IS_SEQUENTIAL $dc]
        set lag [string match LAGUNA* [get_property -quiet SITE_TYPE [get_sites -quiet -of $dc]]]
        foreach lc $far {
            set lclk [_clock_of $lc]
            if {!$seq} {
                set kind comb
            } elseif {$lclk eq ""} {
                set kind "reg->comb"
            } elseif {$dclk eq $lclk} {
                set llag [string match LAGUNA* [get_property -quiet SITE_TYPE [get_sites -quiet -of $lc]]]
                set kind [expr {$lag && $llag ? "hop laguna" : "hop fabric"}]
            } else {
                set kind cdc
            }
            set key "$kind | $ds->[_slr $lc] | [_owner [get_property NAME $dc]] -> [_owner [get_property NAME $lc]]"
            dict lappend groups $key $n
        }
    }
    foreach k [lsort [dict keys $groups]] {
        set nets [lsort -unique [dict get $groups $k]]
        set line [format "%6d  %s" [llength $nets] $k]
        if {[string match "hop*" $k]} {
            foreach dly {max min} {
                set p [get_timing_paths -quiet -delay_type $dly -through $nets -max_paths 1]
                append line "  $dly [expr {$p eq {} ? {-} : [get_property SLACK $p]}]"
            }
        }
        puts $fh $line
    }
    close $fh
    return "sll_audit: [dict size $groups] crossing groups -> $out"
}

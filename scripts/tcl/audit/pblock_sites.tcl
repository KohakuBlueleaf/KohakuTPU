# Per site type, every pblock an XDC shapes on the bare part: count, sibling overlap, escape, cover.
#   vivado -mode batch -source scripts/tcl/audit/pblock_sites.tcl -tclargs <part> <pblocks.xdc> <out.tsv>
lassign $argv part xdc out
set_param tcl.collectionResultDisplayLimit 0
create_project -in_memory -part $part
link_design -part $part
set fh [open $xdc r] ; set txt [read $fh] ; close $fh
set nshape 0
foreach ln [split $txt \n] {
    if {[regexp {^(create_pblock|resize_pblock) } $ln] || [regexp {^set_property \S+ \S+ \[get_pblocks } $ln]} {
        eval $ln
        incr nshape
    }
}
set types {SLICE DSP48E2 RAMB36 RAMB18 URAM288}
set fo [open $out w]
puts $fo "kind\tpblock\tother\ttype\tcount"
foreach pb [lsort [get_pblocks]] {
    puts $fo "ranges\t$pb\t[get_property PARENT $pb]\t-\t[get_property GRID_RANGES $pb]"
    foreach t $types {
        set ss($pb,$t) [get_sites -quiet -of_objects $pb -filter "NAME =~ ${t}_X*"]
        puts $fo "sites\t$pb\t[get_property PARENT $pb]\t$t\t[llength $ss($pb,$t)]"
    }
}
proc v8_minus {a b} {
    foreach s $b { set in($s) 1 }
    set n 0
    foreach s $a { if {![info exists in($s)]} { incr n } }
    return $n
}
foreach pb [lsort [get_pblocks]] {
    set par [get_property PARENT $pb]
    set kids {}
    foreach c [get_pblocks] { if {[get_property PARENT $c] eq $pb} { lappend kids $c } }
    foreach t $types {
        if {$par ne "ROOT"} {
            puts $fo "outside_parent\t$pb\t$par\t$t\t[v8_minus $ss($pb,$t) $ss($par,$t)]"
        }
        if {[llength $kids]} {
            set u {}
            foreach c $kids { lappend u {*}$ss($c,$t) }
            puts $fo "uncovered\t$pb\t[join [lsort $kids] ,]\t$t\t[v8_minus $ss($pb,$t) $u]"
            foreach a $kids {
                foreach b $kids {
                    if {[string compare $a $b] >= 0} { continue }
                    set shared [expr {[llength $ss($a,$t)] - [v8_minus $ss($a,$t) $ss($b,$t)]}]
                    puts $fo "shared\t$a\t$b\t$t\t$shared"
                }
            }
        }
    }
}
close $fo
puts "@@@ pblock sites: $nshape shape lines, [llength [get_pblocks]] pblocks -> $out"

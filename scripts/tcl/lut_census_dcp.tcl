# LUT census of named hierarchies in a synthesised checkpoint: every LUT under
# each prefix bucketed by the register/signal its name derives from, so a
# 700-LUT "skid" says which cone the tool absorbed into it. No design change.
#   vivado -mode batch -source lut_census_dcp.tcl -tclargs <dcp> <prefix>[+<prefix>...] [top_n]

set dcp   [lindex $argv 0]
set pfxs  [split [lindex $argv 1] "+"]
set topn  [lindex $argv 2]
if {$topn eq ""} { set topn 15 }

open_checkpoint $dcp

foreach pfx $pfxs {
    set cells [get_cells -quiet -hier -filter "NAME =~ ${pfx}* && REF_NAME =~ LUT?"]
    puts "@@@ ===== $pfx : [llength $cells] LUT"
    array unset bucket
    array set bucket {}
    foreach c $cells {
        set n [get_property NAME $c]
        set n [string range $n [string length $pfx] end]
        regsub {_i_[0-9]+$} $n "" n
        regsub {__[0-9]+$} $n "" n
        regsub -all {\[[0-9]+\]} $n "" n
        regsub {_reg$} $n "" n
        if {$n eq ""} { set n "(self)" }
        incr bucket($n)
    }
    set rows {}
    foreach {k v} [array get bucket] { lappend rows [list $v $k] }
    set rows [lsort -integer -decreasing -index 0 $rows]
    set i 0
    foreach r $rows {
        if {[incr i] > $topn} { break }
        puts [format "@@@ %6d  %s" [lindex $r 0] [lindex $r 1]]
    }
    # what drives the biggest bucket's LUT inputs, one example cell
    if {[llength $rows]} {
        set ex [lindex [get_cells -quiet -hier -filter "NAME =~ ${pfx}*[lindex [lindex $rows 0] 1]* && REF_NAME =~ LUT?"] 0]
        if {$ex ne ""} {
            puts "@@@ example $ex"
            foreach p [get_pins -quiet -of_objects $ex -filter {DIRECTION == IN}] {
                set net [get_nets -quiet -of_objects $p]
                if {$net ne ""} {
                    set drv [get_pins -quiet -leaf -of_objects $net -filter {DIRECTION == OUT}]
                    puts "@@@    [get_property NAME $p] <- [get_property NAME $net]  drv [lindex $drv 0]"
                }
            }
        }
    }
}
puts "@@@ lut_census done"

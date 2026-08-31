# UG573: a cascade is bottom-up within ONE column and cannot cross an SLR, so
# column height per SLR caps cascade depth and column count caps row width.

create_project -in_memory -part xcvu13p-fhgb2104-2L-e
link_design -part xcvu13p-fhgb2104-2L-e

set us [get_sites -quiet -filter {SITE_TYPE =~ URAM*}]
if {[llength $us] == 0} {
    set us [get_sites -quiet URAM*]
}
puts "@@@ part xcvu13p-fhgb2104-2L-e"
puts "@@@ total URAM288 sites: [llength $us]"

# Column (X) and row (Y) from the site name URAM288_X<c>Y<r>.
array set colrows {}
foreach s $us {
    if {[regexp {X(\d+)Y(\d+)$} [get_property NAME $s] -> x y]} {
        lappend colrows($x) $y
    }
}
set cols [lsort -integer [array names colrows]]
puts "@@@ URAM columns on the die: [llength $cols]  (X = $cols)"

foreach c $cols {
    set ys [lsort -integer $colrows($c)]
    puts "@@@ column X$c : [llength $ys] sites, Y[lindex $ys 0]..Y[lindex $ys end]"
}

puts "@@@ ---------------- per SLR"
foreach slr [get_slrs] {
    set ss [get_sites -quiet -of_objects $slr -filter {SITE_TYPE =~ URAM288*}]
    array unset sc
    array set sc {}
    foreach s $ss {
        if {[regexp {X(\d+)Y(\d+)$} [get_property NAME $s] -> x y]} {
            lappend sc($x) $y
        }
    }
    set sco [lsort -integer [array names sc]]
    set h 0
    if {[llength $sco]} { set h [llength $sc([lindex $sco 0])] }
    puts "@@@ [get_property NAME $slr] : [llength $ss] URAM, [llength $sco] columns, [expr {$h}] tall per column"
    puts "@@@     -> hard cascade cap here = $h (UG573: unlimited within one column of one SLR); clock region = 16 tall"
}
puts "@@@ uram_geometry done"

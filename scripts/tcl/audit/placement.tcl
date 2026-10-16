# Placement census of an implementation checkpoint, read-only, no timing:
# every placed primitive with its site (for footprints and spread), every used
# site's clock region and RPM grid position, and every connection that leaves a
# block (driver cell and site, each load cell and site) for the blocks under
# the mesh, the sysnode, the Xache, the station line and the block design.
#   vivado -mode batch -source scripts/tcl/audit/placement.tcl ?-tclargs dcp <checkpoint> tag placed cfg v9t3?
set here [file dirname [file dirname [file normalize [info script]]]]
set V9_CFG v9t3
foreach {k v} $argv { if {$k eq "cfg"} { set V9_CFG $v } }
source $here/$V9_CFG/00_config.tcl
foreach {k v} $argv {
    switch -- $k { dcp { set V8_DCP $v } tag { set V8_TAG $v } only { set PL_ONLY $v } }
}
if {![info exists PL_ONLY]} { set PL_ONLY all }
if {![info exists V8_DCP]} {
    set V8_DCP $proj_dir/${design_name}.runs/impl_1/${design_name}_wrapper_physopt.dcp
}
if {![info exists V8_TAG]} { set V8_TAG placed }
open_checkpoint $V8_DCP
set top ${design_name}_i
set tag $root/build/${design_name}_${V8_TAG}_place
proc pl_t {} { return [clock format [clock seconds] -format %H:%M:%S] }

# ---- 1. every placed primitive, and every used site ------------------------
if {$PL_ONLY ne "conns"} {
set cells [get_cells -quiet -hierarchical -filter {IS_PRIMITIVE && LOC != ""}]
set fh [open ${tag}_cells.tsv w]
puts $fh [join {CELL REF LOC} \t]
foreach n [get_property NAME $cells] r [get_property REF_NAME $cells] l [get_property LOC $cells] {
    puts $fh "$n\t$r\t$l"
}
close $fh
puts "@@@ [llength $cells] placed primitives  [pl_t]"
set sites [get_sites -quiet -of_objects $cells]
set fh [open ${tag}_sites.tsv w]
puts $fh [join {SITE TYPE CLOCK_REGION RPM_X RPM_Y SLR} \t]
foreach s [get_property NAME $sites] t [get_property SITE_TYPE $sites] \
        c [get_property CLOCK_REGION $sites] x [get_property RPM_X $sites] \
        y [get_property RPM_Y $sites] {
    puts $fh "$s\t$t\t$c\t$x\t$y\t-"
}
close $fh
puts "@@@ [llength $sites] used sites  [pl_t]"
# The grid of every clock region (RPM bounds), from all its sites.
set fh [open ${tag}_regions.tsv w]
puts $fh [join {CLOCK_REGION RPM_X0 RPM_X1 RPM_Y0 RPM_Y1} \t]
foreach cr [get_clock_regions] {
    set ss [get_sites -quiet -of_objects $cr -filter {SITE_TYPE =~ SLICE*}]
    if {![llength $ss]} { continue }
    set xs [get_property RPM_X $ss] ; set ys [get_property RPM_Y $ss]
    puts $fh [join [list [get_property NAME $cr] [tcl::mathfunc::min {*}$xs] \
        [tcl::mathfunc::max {*}$xs] [tcl::mathfunc::min {*}$ys] [tcl::mathfunc::max {*}$ys]] \t]
}
close $fh
puts "@@@ clock-region grid  [pl_t]"
}

# ---- 2. every connection leaving a block ------------------------------------
# A block is a direct hierarchical child of one of these parents; each of its
# pins' flat nets is written once (keyed by the driver pin) with every load.
set parents {}
foreach s {0 1 2 3} {
    lappend parents $top/mesh_$s/inst $top/mesh_$s/inst/u_mag \
        $top/mesh_$s/inst/u_mag/u_pe $top/mesh_$s/inst/u_mag/u_mag
}
lappend parents $top $top/xache/inst/u_kx $top/station_bus/inst/u_line
set blocks {}
foreach p $parents {
    foreach c [get_cells -quiet -filter {!IS_PRIMITIVE} "$p/*"] { lappend blocks $c }
}
puts "@@@ [llength $blocks] blocks  [pl_t]"
set fh [open ${tag}_conns.tsv w]
puts $fh [join {DRIVER DRV_LOC FANOUT LOAD LOAD_LOC} \t]
array set SEEN {}
set nn 0
foreach b $blocks {
    foreach p [get_pins -quiet -of_objects [get_cells $b]] {
        set net [get_nets -quiet -of_objects $p]
        if {[llength $net] != 1} { continue }
        set lp [get_pins -quiet -leaf -of_objects $net]
        set drv [get_pins -quiet $lp -filter {DIRECTION == OUT}]
        if {[llength $drv] != 1} { continue }
        set dn [get_property NAME $drv]
        if {[info exists SEEN($dn)]} { continue }
        set SEEN($dn) 1
        set dc [get_cells -quiet -of_objects $drv]
        if {![llength $dc] || [string match BUFG* [get_property REF_NAME $dc]]} { continue }
        set dl [get_property -quiet LOC $dc]
        set loads [get_pins -quiet $lp -filter {DIRECTION == IN}]
        set fo [llength $loads]
        if {!$fo} { continue }
        set lc [get_cells -quiet -of_objects $loads]
        foreach ln [get_property -quiet NAME $lc] ll [get_property -quiet LOC $lc] {
            puts $fh "[get_property NAME $dc]\t$dl\t$fo\t$ln\t$ll"
        }
        incr nn
    }
    flush $fh
}
close $fh
puts "@@@ $nn block-leaving nets in ${tag}_conns.tsv  [pl_t]"
puts "@@@ placement census done  [pl_t]"

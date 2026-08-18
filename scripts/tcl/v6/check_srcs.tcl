# tclsh scripts/tcl/v6/check_srcs.tcl -- close the instantiation graph from the
# v6 tops. A module no file provides synthesises as a BLACK BOX, not an error.

set here [file dirname [file normalize [info script]]]
source $here/00_config.tcl
source $here/rtl_index.tcl

# add_files is stubbed: 10_sources.tcl only has to yield its list.
proc add_files args {}
proc update_compile_order args {}
source $here/10_sources.tcl

set listed {}
foreach f $V6_SOURCES { lappend listed [string tolower [file normalize $root/$f]] }

set have [dict create] ; set shadow {}
dict for {m paths} $SRC {
    foreach p $paths {
        if {[lsearch -exact $listed [string tolower [file normalize $p]]] >= 0} {
            dict set have $m $p
        }
    }
    if {[llength $paths] > 1} { lappend shadow $m }
}

# Xilinx macros and primitives come from the tool, not from src/.
set PRIM {xpm_* BUFG* MMCM* PLL* IBUF* OBUF* IOBUF* DSP48* RAMB* URAM* FDRE FDSE
          FDCE FDPE LUT? SRL* CARRY* MUXF? IDDR* ODDR* BITSLICE* ISERDES* OSERDES*}
proc is_prim {m} {
    global PRIM
    foreach p $PRIM { if {[string match $p $m]} { return 1 } }
    return 0
}

set TOPS [list sb_bd_line4 ktpu_div2 mag_link_cdc]
foreach {mid mod} $MESHES { lappend TOPS $mod }

set queue [lsort -unique $TOPS] ; set reach {} ; set bad 0
while {[llength $queue]} {
    set m [lindex $queue 0] ; set queue [lrange $queue 1 end]
    if {[lsearch -exact $reach $m] >= 0} { continue }
    lappend reach $m
    if {![dict exists $have $m]} {
        if {[dict exists $SRC $m]} {
            puts "  $m -- defined in [dict get $SRC $m] but NOT in V6_SOURCES"
        } else {
            puts "  $m -- no file under src/ defines it"
        }
        incr bad ; continue
    }
    foreach r [dict get $INS $m] {
        if {[is_prim $r]} { continue }
        lappend queue $r
    }
}

set unused {}
foreach f $V6_SOURCES {
    set used 0
    foreach m $reach {
        foreach p [dict get $SRC $m] {
            if {[string equal -nocase [file normalize $p] [file normalize $root/$f]]} {
                set used 1
            }
        }
    }
    if {!$used} { lappend unused $f }
}
foreach f $unused { puts "  $f: listed but no top reaches it" }

# Two files defining one module: Vivado picks one, and never says which.
foreach m $shadow {
    if {[lsearch -exact $reach $m] >= 0} { puts "  $m defined twice: [dict get $SRC $m]" }
}

puts "[llength $TOPS] tops reach [llength $reach] modules from\
      [llength $V6_SOURCES] listed files"
if {$bad} { error "$bad module(s) would synthesise as a black box" }
puts "@@@ srcs ok: every module the v6 tops instantiate is in the project"

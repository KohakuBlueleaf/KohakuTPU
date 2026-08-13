# Out-of-context synthesis of one module, to answer "does it close at F?".
#
#   vivado -mode batch -source scripts/synth_check.tcl \
#       -tclargs <top> <period_ns> <part> <generics> <file>...
#
# <generics> is "-" or a +-separated NAME:VALUE list, e.g. FIFO_DEPTH:8. It is
# NAME:VALUE joined by '+', not '=' and not ',': Vivado's .bat wrappers split
# arguments on '=', and powershell -File splits a string argument on ','. Both
# forms are rebuilt here in Tcl, where nothing is splitting anything.
#
# Out-of-context because the question is about the module's own logic depth, not
# about placement in a full design. The number it produces is therefore optimistic
# for a large mesh -- 196 routers plus their interconnect will not hold the same
# WNS as one router alone. Treat it as an upper bound and a way to catch a design
# that cannot possibly make the target, not as a sign-off.

if {[llength $argv] < 5} {
    puts "usage: synth_check.tcl <top> <period_ns> <part> <generics|-> <file>..."
    exit 2
}

set top      [lindex $argv 0]
set period   [lindex $argv 1]
set part     [lindex $argv 2]
set gspec    [lindex $argv 3]
set files    [lrange $argv 4 end]

set generics {}
if {$gspec ne "-"} {
    foreach kv [split $gspec "+"] {
        set parts [split $kv ":"]
        lappend generics "[lindex $parts 0]=[lindex $parts 1]"
    }
}

puts "=== synth_check: $top @ ${period}ns on $part generics {$generics} ==="

# A GENERIC THAT DOES NOT EXIST IS NOT AN ERROR TO VIVADO, IT IS A DEFAULT --
# and this has now produced a false result three times: a five-point DEPTH sweep
# that returned five identical numbers (see the -generic note below), and an A/B
# whose two arms agreed to the digit because the snapshot predated the parameter.
# The failure is always a PLAUSIBLE number, never a crash, so it is only caught
# by someone noticing that two things that should differ do not.
#
# Textual rather than elaborated, because this has to run BEFORE synth_design:
# by the time the netlist exists the wrong number has already been produced.
#
# THE TOP'S OWN LIST: -generic binds only to the top, so a name declared in a
# submodule alone is ignored as silently as a typo. Falls back to the
# whole-file search if the top's header cannot be located.
set plist ""
foreach f $files {
    if {[catch {set fh [open $f r]}]} { continue }
    set txt [read $fh]
    close $fh
    if {[regexp "module\\s+\\m${top}\\M\\s*#\\s*\\((.*?)\n\\)\\(" $txt -> hdr]} {
        set plist $hdr
        break
    }
}

foreach g $generics {
    set name [lindex [split $g "="] 0]
    set seen 0
    set where "no source declares"
    if {$plist ne ""} {
        if {[regexp "parameter\[^\n;\]*\\m${name}\\M" $plist]} { set seen 1 }
        set where "top module $top does not declare"
    } else {
        foreach f $files {
            if {[catch {set fh [open $f r]}]} { continue }
            set txt [read $fh]
            close $fh
            if {[regexp "parameter\[^\n;\]*\\m${name}\\M" $txt]} { set seen 1 ; break }
        }
    }
    if {!$seen} {
        puts "SYNTH FAILED: -generic $g names a parameter $where."
        puts "  Vivado would ignore it silently and synthesise the default,"
        puts "  which reads as a real measurement. Check the spelling, and"
        puts "  check the sources are the ones you think -- a snapshot taken"
        puts "  before the parameter was added looks exactly like this."
        puts "  A parameter declared only in a SUBMODULE fails here too: it"
        puts "  must be threaded up to the top before it can be swept."
        exit 1
    }
}

# No auto_detect_xpm here: it is project-mode only and errors with "No open
# project". Non-project synth_design resolves xpm_fifo_sync on its own.
foreach f $files {
    puts "  read: $f"
    read_verilog $f
}

set cmd [list synth_design -top $top -part $part -mode out_of_context]
# Each generic needs its OWN -generic flag. `lappend cmd -generic $generics`
# appends the whole list as one argument, and eval then flattens it to
# `-generic A=1 B=2` -- Vivado takes A and silently drops B. With a single
# generic it happens to work, which is why a five-point DEPTH sweep returned
# five byte-identical results before this was noticed.
foreach g $generics { lappend cmd -generic $g }
if {[catch {eval $cmd} err]} {
    puts "SYNTH FAILED: $err"
    exit 1
}

# A port literally called `clk` where there is one, every `*clk*` input where
# there is not: a two-domain module has neither, and an unconstrained domain
# reports no paths at all rather than failing.
set clkports [get_ports -quiet clk]
if {[llength $clkports] == 0} {
    set clkports [get_ports -quiet -filter {DIRECTION == IN} *clk*]
}
if {[llength $clkports] == 0} {
    puts "SYNTH FAILED: no clock port found on $top"
    exit 1
}
set clknames {}
foreach cp $clkports {
    set nm [get_property NAME $cp]
    create_clock -name $nm -period $period $cp
    lappend clknames $nm
}
# Without this the worst path is a crossing THROUGH the async FIFO, which is
# asynchronous by construction and would make the reported Fmax meaningless.
if {[llength $clknames] > 1} {
    set grp {}
    foreach nm $clknames { lappend grp -group [get_clocks $nm] }
    set_clock_groups -asynchronous {*}$grp
    puts "  NOTE constrained [llength $clknames] clocks as asynchronous groups"
}

# TOP TEN, NOT THE WORST ALONE: mm_mover's ten span 0.046 ns over THREE
# structures, and the one owning 6 of 10 was not the one owning the worst.
# -nworst 1 is one path per endpoint, so these are ten distinct cones.
set paths [get_timing_paths -delay_type max -max_paths 10 -nworst 1]
set worst [lindex $paths 0]
set wns [get_property SLACK $worst]

# Where the worst path actually starts and ends. Without this a number like
# "410 MHz" is unattributable -- in out-of-context mode a port-to-register path is
# constrained against the whole period, so the worst path may be an artefact of the
# measurement boundary rather than real logic.
set rank 0
foreach p $paths {
    incr rank
    puts [format "  PATH %2d %7.3f  %s -> %s" $rank [get_property SLACK $p] \
              [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
}
# A FAILURE, NOT A PASS. Zero paths means the clock reached nothing, or the
# design has no sequential logic -- either way nothing was measured, and
# exiting 0 here reported "no paths" in a column the eye reads as a result.
if {$wns eq "" || $wns eq "undef"} {
    puts "SYNTH FAILED: $top has no timing paths -- nothing was measured."
    puts "  The clock was created on: $clknames"
    exit 1
}

# Fmax is derived from the achieved period, not from the target: a positive slack
# means the target was met with room to spare, a negative one means it was not.
set achieved [expr {$period - $wns}]
set fmax     [expr {1000.0 / $achieved}]

puts "RESULT $top  target [format %.3f $period] ns  WNS [format %.3f $wns] ns\
 -> achieved [format %.3f $achieved] ns = [format %.1f $fmax] MHz"

# Per-instance breakdown, written beside the log. Composition is not additive --
# a submodule synthesised alone optimises differently from the same submodule
# inside a parent -- so "what does one router cost here" has to be read out of the
# hierarchy rather than inferred by subtracting standalone runs.
report_utilization -hierarchical -hierarchical_depth 3 -file "${top}_hier.rpt"
puts "  HIER written to ${top}_hier.rpt"

# Utilisation, so the LUT-budget arithmetic can be checked against something
# measured. Scraped out of the report text -- report_utilization
# returns a string, not an object, so get_property cannot be used on it.
set rpt [report_utilization -return_string]
foreach line [split $rpt "\n"] {
    if {[regexp {CLB LUTs|CLB Registers|Block RAM Tile|URAM|DSPs} $line]} {
        puts "  UTIL $line"
    }
}

if {$wns < 0} {
    puts "VERDICT $top: MISSES ${period}ns"
} else {
    puts "VERDICT $top: meets ${period}ns"
}

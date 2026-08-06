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

# No auto_detect_xpm here: it is project-mode only and errors with "No open
# project". Non-project synth_design resolves xpm_fifo_sync on its own.
foreach f $files {
    puts "  read: $f"
    read_verilog $f
}

set cmd [list synth_design -top $top -part $part -mode out_of_context]
if {[llength $generics]} { lappend cmd -generic $generics }
if {[catch {eval $cmd} err]} {
    puts "SYNTH FAILED: $err"
    exit 1
}

create_clock -name clk -period $period [get_ports clk]

set worst [lindex [get_timing_paths -delay_type max -max_paths 1] 0]
set wns [get_property SLACK $worst]

# Where the worst path actually starts and ends. Without this a number like
# "410 MHz" is unattributable -- in out-of-context mode a port-to-register path is
# constrained against the whole period, so the worst path may be an artefact of the
# measurement boundary rather than real logic.
if {$worst ne ""} {
    puts "  PATH [get_property STARTPOINT_PIN $worst] -> [get_property ENDPOINT_PIN $worst]"
}
if {$wns eq "" || $wns eq "undef"} {
    puts "RESULT $top: no timing paths found"
    exit 0
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

# Utilisation, so the LUT-budget arithmetic in .plan/decisions.md can be checked
# against something measured. Scraped out of the report text -- report_utilization
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

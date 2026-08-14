# Post-synthesis hook: report what the timer actually sees.

# A pattern matching nothing constrains nothing and warns at a severity that
# scrolls past; route_design then times crossings that do not exist.

# Run with `vivado -mode batch -source ...` on an open synthesised design, or as
# a TCL.POST hook on synth_1.

# NAMES, not pins: `get_clocks -of_objects [get_pins -hier -filter ...]` resolves
# to nothing here -- all six read missing on 2026-08-14 with synthesis clean.
set clocks [get_clocks -quiet]
puts "@@@ clocks: [llength $clocks]"
foreach c $clocks {
    puts "@@@ CLK [get_property NAME $c] period=[get_property PERIOD $c]"
}

# NOT `get_clock_groups` -- no such command, and it aborted the whole hook.
# Interaction answers the real question: grouped reports async, ungrouped timed.
if {[catch {report_clock_interaction -return_string} inter]} {
    puts "@@@ WARN report_clock_interaction unavailable: $inter"
} else {
    puts $inter
}

# The only hard failure: a design with no clocks at all is not timed, and every
# report downstream would read as passing.
if {[llength $clocks] == 0} {
    error "no clocks in the design -- nothing is being timed"
}
puts "@@@ CLOCKS OK"

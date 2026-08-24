# OOC constraints for a KohakuSIMT block.
#
# ONE CLOCK. The SIMT PE lives entirely on noc_clk, exactly as the controller PE
# does: its requestor speaks the port contract in that domain, so the unit joins
# it and no CDC exists anywhere in it. A second clock here would mean something
# had gone wrong.
#
# UNCONDITIONAL, and that is not a style choice. Guarding this with
# `if {[llength [get_ports -quiet clk]]}` reads as defensive and is a silent
# failure: the guard evaluates when the file is read, before the design exists,
# so it is always false and no clock is ever created. Synthesis then has no
# target, every LUT figure is the unconstrained one, and `ooc_record` prints no
# Fmax line rather than an error -- the measurement reports nothing wrong while
# measuring the wrong thing. A block with no `clk` port belongs in its own
# script, not behind a guard here.

# NO `if` HERE. Vivado's XDC reader rejects it -- "Command 'if' is not supported
# in the xdc constraint file" -- as a CRITICAL WARNING in every single run, and a
# default set in a file that cannot execute it is not a default. The caller sets
# `::ooc_period` before read_xdc; a caller that forgets should fail loudly here
# rather than be silently constrained at some other period.

create_clock -name noc_clk -period $::ooc_period [get_ports clk]

# OOC constraints for the RV32 controller PE.
#
# ONE CLOCK, AND THAT IS THE DESIGN. The PE lives entirely on noc_clk -- the
# requestor speaks the port contract in that domain, so the core joins it and no
# CDC exists anywhere in the unit. There is therefore no clock group to declare:
# a second clock here would mean something had gone wrong.
#
# THE PERIOD IS THE CALLER'S, AND THE CALLER MUST SET IT. Area is not
# independent of the constraint -- synthesis spends LUT chasing timing -- so a
# resource figure only means something next to the target it was asked for.
#
# NO `if` HERE. Vivado's XDC parser rejects it outright ("Command 'if' is not
# supported in the xdc constraint file", Designutils 20-1307), so the guard that
# used to stand here was dead: it threw a CRITICAL WARNING on every run and
# never supplied its default. Both callers -- ooc_rv_pe.tcl and ooc_simd_pe.tcl
# -- set ::ooc_period unconditionally, so an unset variable now fails loudly on
# the next line instead of silently constraining to the wrong period.
# ooc_khs.xdc and ooc_khg.xdc carry the same note.

create_clock -name noc_clk -period $::ooc_period [get_ports clk]

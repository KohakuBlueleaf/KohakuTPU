# OOC constraints for the RV32 controller PE.
#
# ONE CLOCK, AND THAT IS THE DESIGN. The PE lives entirely on noc_clk -- the
# requestor speaks the port contract in that domain, so the core joins it and no
# CDC exists anywhere in the unit. There is therefore no clock group to declare:
# a second clock here would mean something had gone wrong.
#
# THE PERIOD IS A CALLER'S VARIABLE, defaulting to the 2.5 ns design target.
# Area is not independent of the constraint -- synthesis spends LUT chasing
# timing -- so a resource figure only means something next to the target it was
# asked for, and one script has to serve every target.

if {![info exists ::ooc_period]} { set ::ooc_period 2.500 }

create_clock -name noc_clk -period $::ooc_period [get_ports clk]

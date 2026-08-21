# OOC constraints for the KohakuDSP vector unit.
#
# ONE CLOCK. The unit lives on the PE's clock, which lives on noc_clk, and a
# second clock here would mean something had gone wrong.
#
# THE PERIOD IS A CALLER'S VARIABLE, defaulting to the 3.333 ns the base core's
# measurement settled on: that is where the PE reaches its ceiling, and asking
# for less buys LUT rather than megahertz.

if {![info exists ::ooc_period]} { set ::ooc_period 3.333 }

create_clock -name noc_clk -period $::ooc_period [get_ports clk]

# OOC constraints for sb_bd_leaf: the bus clock and one local clock.

create_clock -name bus_clk -period 2.500 [get_ports bus_clk]
create_clock -name clk_loc -period 3.333 [get_ports clk_loc]

set_clock_groups -asynchronous \
    -group [get_clocks bus_clk] -group [get_clocks clk_loc]

# OOC constraints for sb_bd_root. NO Tcl control flow here: the XDC reader does
# not evaluate it, and every create_clock after the branch is silently skipped.

create_clock -name bus_clk  -period 2.500  [get_ports bus_clk]
create_clock -name clk_ctrl -period 10.000 [get_ports clk_ctrl]
create_clock -name clk_xdma -period 4.000  [get_ports clk_xdma]
create_clock -name clk_loc  -period 3.333  [get_ports clk_loc]

set_clock_groups -asynchronous \
    -group [get_clocks bus_clk]  -group [get_clocks clk_ctrl] \
    -group [get_clocks clk_xdma] -group [get_clocks clk_loc]

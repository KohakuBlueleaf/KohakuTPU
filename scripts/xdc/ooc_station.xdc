# OOC constraints for the station tops. The four port clocks are v5's; bus_clk
# is the knob the whole design trades against, so it is deliberately aggressive.

create_clock -name bus_clk  -period 2.500  [get_ports bus_clk]
create_clock -name clk_ctrl -period 10.000 [get_ports clk_ctrl]
create_clock -name clk_xdma -period 4.000  [get_ports clk_xdma]
create_clock -name clk_mesh -period 4.219  [get_ports clk_mesh]
create_clock -name clk_ddr  -period 3.333  [get_ports clk_ddr]

# Every crossing in this design is an async FIFO by construction, so the tool
# must not try to time between domains.
set_clock_groups -asynchronous \
    -group [get_clocks bus_clk]  -group [get_clocks clk_ctrl] \
    -group [get_clocks clk_xdma] -group [get_clocks clk_mesh] \
    -group [get_clocks clk_ddr]

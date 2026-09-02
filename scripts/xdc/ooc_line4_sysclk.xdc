# OOC constraints for sb_line4 in the one-system-clock shape (v8t2): the four
# bus clocks are ONE net (one group, so the links between stations are timed),
# the four port-0/1 clocks are the one sysnode clock, ctrl and xdma stand alone,
# and each station's port 2 is its own MIG ui_clk.

# NO Tcl control flow: the XDC reader does not evaluate it and every
# create_clock after the branch is silently skipped.

create_clock -name bus_clk0 -period 5.000 [get_ports bus_clk0]
create_clock -name bus_clk1 -period 5.000 [get_ports bus_clk1]
create_clock -name bus_clk2 -period 5.000 [get_ports bus_clk2]
create_clock -name bus_clk3 -period 5.000 [get_ports bus_clk3]

create_clock -name clk_ctrl -period 10.000 [get_ports clk_ctrl]
create_clock -name clk_xdma -period 4.000  [get_ports clk_xdma]

create_clock -name clk_s0   -period 3.333  [get_ports clk_s0]
create_clock -name clk_s1   -period 3.333  [get_ports clk_s1]
create_clock -name clk_s2   -period 3.333  [get_ports clk_s2]
create_clock -name clk_s3   -period 3.333  [get_ports clk_s3]
create_clock -name clk_ddr0 -period 3.333  [get_ports clk_ddr0]
create_clock -name clk_ddr1 -period 3.333  [get_ports clk_ddr1]
create_clock -name clk_ddr2 -period 3.333  [get_ports clk_ddr2]
create_clock -name clk_ddr3 -period 3.333  [get_ports clk_ddr3]

set_clock_groups -asynchronous \
    -group [get_clocks {bus_clk0 bus_clk1 bus_clk2 bus_clk3}] \
    -group [get_clocks clk_ctrl] -group [get_clocks clk_xdma] \
    -group [get_clocks {clk_s0 clk_s1 clk_s2 clk_s3}] \
    -group [get_clocks clk_ddr0] -group [get_clocks clk_ddr1] \
    -group [get_clocks clk_ddr2] -group [get_clocks clk_ddr3]

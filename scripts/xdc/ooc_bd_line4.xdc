# OOC constraints for sb_bd_line4: production v6.7/v7 rates -- bus 200,
# ctrl 100, xdma 250, mesh MAG 300 per SLR, DDR ui_clk 300 per SLR.

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

# Every crossing is an async FIFO by construction, including the links when
# LINK_CDC is 1, so the tool must not time between any of these.
set_clock_groups -asynchronous \
    -group [get_clocks bus_clk0] -group [get_clocks bus_clk1] \
    -group [get_clocks bus_clk2] -group [get_clocks bus_clk3] \
    -group [get_clocks clk_ctrl] -group [get_clocks clk_xdma] \
    -group [get_clocks clk_s0]   -group [get_clocks clk_s1] \
    -group [get_clocks clk_s2]   -group [get_clocks clk_s3] \
    -group [get_clocks clk_ddr0] -group [get_clocks clk_ddr1] \
    -group [get_clocks clk_ddr2] -group [get_clocks clk_ddr3]

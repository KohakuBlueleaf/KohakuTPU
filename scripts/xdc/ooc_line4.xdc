# OOC constraints for sb_line4: a fabric clock per station, two manager clocks,
# a mesh clock per SLR and one DDR clock -- v5's domain count, on a line.

# NO Tcl control flow: the XDC reader does not evaluate it and every
# create_clock after the branch is silently skipped.

create_clock -name bus_clk0 -period 5.000 [get_ports bus_clk0]
create_clock -name bus_clk1 -period 5.000 [get_ports bus_clk1]
create_clock -name bus_clk2 -period 5.000 [get_ports bus_clk2]
create_clock -name bus_clk3 -period 5.000 [get_ports bus_clk3]

create_clock -name clk_ctrl -period 10.000 [get_ports clk_ctrl]
create_clock -name clk_xdma -period 4.000  [get_ports clk_xdma]

create_clock -name clk_s0   -period 4.219  [get_ports clk_s0]
create_clock -name clk_s1   -period 3.333  [get_ports clk_s1]
create_clock -name clk_s2   -period 5.545  [get_ports clk_s2]
create_clock -name clk_s3   -period 4.746  [get_ports clk_s3]
create_clock -name clk_ddr  -period 3.333  [get_ports clk_ddr]

# Every crossing is an async FIFO by construction, including the links when
# LINK_CDC is 1, so the tool must not time between any of these.
set_clock_groups -asynchronous \
    -group [get_clocks bus_clk0] -group [get_clocks bus_clk1] \
    -group [get_clocks bus_clk2] -group [get_clocks bus_clk3] \
    -group [get_clocks clk_ctrl] -group [get_clocks clk_xdma] \
    -group [get_clocks clk_s0]   -group [get_clocks clk_s1] \
    -group [get_clocks clk_s2]   -group [get_clocks clk_s3] \
    -group [get_clocks clk_ddr]

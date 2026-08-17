# OOC constraints for sb_quad: two manager clocks plus one local clock per SLR,
# matching the clk_wiz-per-SLR shape the validation BD builds.

create_clock -name bus_clk  -period 2.500  [get_ports bus_clk]
create_clock -name clk_ctrl -period 10.000 [get_ports clk_ctrl]
create_clock -name clk_xdma -period 4.000  [get_ports clk_xdma]
create_clock -name clk_s0   -period 4.219  [get_ports clk_s0]
create_clock -name clk_s1   -period 3.333  [get_ports clk_s1]
create_clock -name clk_s2   -period 5.545  [get_ports clk_s2]
create_clock -name clk_s3   -period 4.746  [get_ports clk_s3]

# Every crossing in this design is an async FIFO by construction, so the tool
# must not try to time between domains.
set_clock_groups -asynchronous \
    -group [get_clocks bus_clk]  -group [get_clocks clk_ctrl] \
    -group [get_clocks clk_xdma] -group [get_clocks clk_s0] \
    -group [get_clocks clk_s1]   -group [get_clocks clk_s2] \
    -group [get_clocks clk_s3]

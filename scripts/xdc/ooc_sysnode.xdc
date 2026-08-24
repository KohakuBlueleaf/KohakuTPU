# Gate 0's clocks. 3.333 ns is the project's OOC reference period, and the two
# domains are asynchronous by construction -- mag_dram_port is the crossing.
create_clock -period 3.333 -name clk  [get_ports clk]
create_clock -period 3.333 -name dclk [get_ports dram_aclk]
set_clock_groups -asynchronous -group [get_clocks clk] -group [get_clocks dclk]

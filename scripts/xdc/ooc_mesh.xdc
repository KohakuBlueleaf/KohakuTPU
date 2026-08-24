# A whole mesh out of context. 3.333 ns is the project's OOC reference period.
# The five domains are asynchronous by construction: noc_local_cdc and
# mag_dram_port are the crossings, so grouping them is what stops the tool
# timing paths that never exist.
create_clock -period 3.333 -name aclk [get_ports axi_aclk]
create_clock -period 3.333 -name nclk [get_ports noc_clk]
create_clock -period 3.333 -name tclk [get_ports mat_clk]
create_clock -period 3.333 -name vclk [get_ports vec_clk]
create_clock -period 3.333 -name dclk [get_ports dram_aclk]
set_clock_groups -asynchronous \
    -group [get_clocks aclk] -group [get_clocks nclk] \
    -group [get_clocks tclk] -group [get_clocks vclk] \
    -group [get_clocks dclk]

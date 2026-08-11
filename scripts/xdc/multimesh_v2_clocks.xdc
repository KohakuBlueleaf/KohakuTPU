# Asynchronous clock groups for multimesh v2.
#
# NO proc / foreach / if. Vivado parses XDC in a restricted mode and rejects
# control flow with "Command 'proc' is not supported in the xdc constraint
# file" -- a CRITICAL WARNING, not an error, so the block is SILENTLY SKIPPED
# and every crossing gets timed. That cost hours of routing on 2026-08-11.
# Command substitution IS allowed, which is what makes the flat form below work.
#
# Three board references -- system 100 MHz, four DDR refclks, PCIe -- so these
# domains are mutually asynchronous. mag_dram_port crosses mesh <-> DDR through
# xpm_fifo_async, which carries its own scoped CDC constraints; this only stops
# the tool timing a crossing that is not meant to be timed.
#
# THE MESH CLOCK IS RETUNABLE AND TIMING DOES NOT KNOW. Vivado constrains
# clk_wiz_mesh/clk_out1 from the MMCM's build-time M/D/k, so that frequency is
# the VERIFIED ceiling: at or below it the design is analysed, above it is the
# deliberately unmeasured sweep (docs/clocking.md s6).

set_clock_groups -asynchronous \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *clk_wiz_ctrl*clk_out1}]] \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *clk_wiz_mesh*clk_out1}]] \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *ddr4_0*c0_ddr4_ui_clk}]] \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *ddr4_1*c0_ddr4_ui_clk}]] \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *ddr4_2*c0_ddr4_ui_clk}]] \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *ddr4_3*c0_ddr4_ui_clk}]] \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *xdma_0*axi_aclk}]]

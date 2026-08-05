# XDC constraints for the Xilinx F3E board
# part: xcvu13p-fhgb2104-2-e

# General configuration
set_property CFGBVS GND [current_design]
set_property CONFIG_VOLTAGE 1.8 [current_design]
set_property BITSTREAM.GENERAL.COMPRESS true [current_design]
set_property BITSTREAM.CONFIG.CONFIGFALLBACK ENABLE [current_design]
set_property BITSTREAM.CONFIG.EXTMASTERCCLK_EN DISABLE [current_design]
set_property BITSTREAM.CONFIG.CONFIGRATE 85.0 [current_design]
set_property BITSTREAM.CONFIG.SPI_32BIT_ADDR YES [current_design]
set_property BITSTREAM.CONFIG.SPI_BUSWIDTH 4 [current_design]
set_property BITSTREAM.CONFIG.SPI_FALL_EDGE YES [current_design]
set_property BITSTREAM.CONFIG.UNUSEDPIN PULLUP [current_design]

# 100 MHz clock
set_property IOSTANDARD DIFF_SSTL12 [get_ports system_clk_n]
set_property IOSTANDARD DIFF_SSTL12 [get_ports system_clk_p]
set_property PACKAGE_PIN AY23 [get_ports system_clk_p]
# create_clock -add -name clk -period 10.00 [get_ports {clk}];

set_property IOSTANDARD LVCMOS12 [get_ports {led_tri_io[*]}]
set_property PACKAGE_PIN BA20 [get_ports {led_tri_io[0]}]
set_property PACKAGE_PIN BB20 [get_ports {led_tri_io[1]}]
set_property PACKAGE_PIN BB21 [get_ports {led_tri_io[2]}]
set_property PACKAGE_PIN BC21 [get_ports {led_tri_io[3]}]
set_property PACKAGE_PIN BB22 [get_ports {led_tri_io[4]}]
set_property PACKAGE_PIN BC22 [get_ports {led_tri_io[5]}]
set_property PACKAGE_PIN BA24 [get_ports {led_tri_io[6]}]
set_property PACKAGE_PIN BB24 [get_ports {led_tri_io[7]}]

# set_property IOSTANDARD LVCMOS12 [get_ports user_lnk_up]
# set_property PACKAGE_PIN BD20 [get_ports user_lnk_up]

# set_property IOSTANDARD LVCMOS12 [get_ports pcie_reset]
# set_property PACKAGE_PIN AR26 [get_ports pcie_reset]
set_property IOSTANDARD LVCMOS12 [get_ports rst]
set_property PACKAGE_PIN AR26 [get_ports rst]

set_property DRIVE 8 [get_ports {led_tri_io[7]}]
set_property DRIVE 8 [get_ports {led_tri_io[6]}]
set_property DRIVE 8 [get_ports {led_tri_io[5]}]
set_property DRIVE 8 [get_ports {led_tri_io[4]}]
set_property DRIVE 8 [get_ports {led_tri_io[3]}]
set_property DRIVE 8 [get_ports {led_tri_io[2]}]
set_property DRIVE 8 [get_ports {led_tri_io[1]}]
set_property DRIVE 8 [get_ports {led_tri_io[0]}]

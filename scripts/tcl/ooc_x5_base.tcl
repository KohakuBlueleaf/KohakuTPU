# DEV single baseline: kaxi_xbar5 4x4 full SAMD, 512b. The one config to iterate the
# LUT shrink on. Target: half SMC (~4,235) / near axi-ic. Census to see the overhead.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
read_verilog [list [file join $root src kohakuaxi kaxi_wr.v] \
                   [file join $root src kohakuaxi kaxi_rd.v] \
                   [file join $root src kohakuaxi kaxi_xbar5.v]]
synth_design -top kaxi_xbar5 -part $part -mode out_of_context \
    -generic M=4 -generic N_HOME=4 -generic DATA_W=512 \
    -generic WR_MODE=1 -generic RD_MODE=1 -flatten_hierarchy rebuilt
create_clock -name clk -period 3.333 [get_ports clk]
ooc_record "x5-4x4-SAMD-300" "M=4 N=4 wr=SAMD rd=SAMD 300MHz" 2000 1
ooc_lut_census x5 "" 10
puts "X5BASE_DONE"

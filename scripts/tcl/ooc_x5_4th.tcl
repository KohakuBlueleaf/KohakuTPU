set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
read_verilog [list [file join $root src kohakuaxi kaxi_wr.v] \
                   [file join $root src kohakuaxi kaxi_rd.v] \
                   [file join $root src kohakuaxi kaxi_xbar5.v]]
synth_design -top kaxi_xbar5 -part $part -mode out_of_context \
    -generic M=4 -generic N_HOME=4 -generic DATA_W=512 \
    -generic WR_MODE=1 -generic RD_MODE=0
create_clock -name clk -period 2.0 [get_ports clk]
ooc_record "x5-4x4-wSAMD-rSASD" "wr=SAMD rd=SASD" 2000 1
puts "X54_DONE"
